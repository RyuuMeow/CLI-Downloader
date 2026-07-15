from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from threading import Event, Thread, local
from time import monotonic
from typing import TextIO
from urllib.parse import urlsplit

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.suggester import Suggester
from textual.containers import Horizontal, Vertical
from textual.geometry import Size
from textual.css.query import NoMatches
from textual.widgets import (
    Button,
    DataTable,
    Input,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)
from cli_downloader.about import PROJECT_NAME, REPOSITORY_URL, __version__
from cli_downloader.application.release_update_service import (
    ReleaseUpdateResult,
    ReleaseUpdateService,
    ReleaseUpdateStatus,
)
from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.config.loader import ConfigLoader
from cli_downloader.config.schema import ConfigField, SettingsRegistry
from cli_downloader.core.browser_auth import browser_session_has_cookies
from cli_downloader.core.crash_log import write_crash_log
from cli_downloader.core.errors import ConfigValidationError, TaskNotFoundError
from cli_downloader.plugins.runtime import ApplicationRuntime
from cli_downloader.plugins.activation import PluginActivationStatus
from cli_downloader.plugins.loader import PluginScanStatus
from cli_downloader.plugins.catalog import (
    CATALOG_REPOSITORY,
    PluginCatalog,
    PluginCatalogEntry,
    PluginCatalogService,
)
from cli_downloader.core.operations import (
    ListItemData,
    ListPageData,
    OperationResult,
    ResultKind,
)
from cli_downloader.core.task_tree import TaskFileNode, TaskKind, TaskNode, TaskState
from cli_downloader.core.view_session import ViewSession
from cli_downloader.core.widgets import ActionKind
from cli_downloader.sites.manifest import BrowserAuthSchema
from cli_downloader.cli.textual_renderer import (
    AsciiSelectionList,
    CLI_DOWNLOADER_THEME,
    CorePageView,
)
from cli_downloader.core.progress import byte_percentage, normalise_byte_progress
from cli_downloader.cli.i18n import TranslationService, translation_context
from cli_downloader.cli.presentation import SHELL_CSS
from cli_downloader.cli.table import TableColumn, TableSpec, render_table_text
from cli_downloader.plugins.manager import (
    _read_active_marker,
    fetch_remote_plugin_metadata,
    install_plugin,
    remove_plugin,
    update_plugin,
    check_plugin_update,
)
from cli_downloader.plugins.registry import load_registry
from textual import work

CommandRunner = Callable[[Sequence[str]], int]
ShellProgress = Callable[[str, Mapping[str, object]], None]
BackgroundCommandRunner = Callable[[Sequence[str], Event, ShellProgress], int]
TaskServiceLoader = Callable[[], TaskHierarchyService]
OperationRunner = Callable[[str, Mapping[str, object]], tuple[str, OperationResult]]
BatchSelectionRunner = Callable[
    [Sequence[ListItemData], str | None, str | None, Event, ShellProgress], int
]
ExternalOpener = Callable[[str], None]
BrowserAuthRunner = Callable[[str, BrowserAuthSchema], str]
BrowserAuthValidator = Callable[[str, str], bool]
ShutdownWarning = Callable[[], str | None]

_AUTO_REFRESH_TASK_STATES = frozenset({TaskState.PREPARING, TaskState.QUEUED, TaskState.RUNNING})
_PLUGIN_STATUS_MIN_WIDTH = 18


class _ThreadLocalOutput:
    def __init__(self, fallback: TextIO) -> None:
        self._fallback = fallback
        self._local = local()

    @contextmanager
    def capture(self, stream: TextIO):
        previous = getattr(self._local, "stream", None)
        self._local.stream = stream
        try:
            yield
        finally:
            self._local.stream = previous

    def write(self, value: str) -> int:
        return self._stream.write(value)

    def flush(self) -> None:
        return self._stream.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)

    @property
    def _stream(self) -> TextIO:
        return getattr(self._local, "stream", None) or self._fallback


@contextmanager
def _capture_output(stream: TextIO):
    if isinstance(sys.stdout, _ThreadLocalOutput):
        with sys.stdout.capture(stream):
            yield
        return
    with redirect_stdout(stream):
        yield


@dataclass
class ShellCommandBlock:
    command: str
    status: str = "running"
    output: str = ""
    detail: str = "Running"
    started_at: float = field(default_factory=monotonic)
    elapsed_seconds: float | None = None

    @property
    def elapsed(self) -> float:
        return (
            monotonic() - self.started_at if self.elapsed_seconds is None else self.elapsed_seconds
        )


class CommandSuggester(Suggester):
    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None

        commands = [
            "download ",
            "sites",
            "tasks ",
            "history ",
            "batches ",
            "config ",
            "plugins ",
            "clear",
            "quit",
        ]

        for cmd in commands:
            if cmd.startswith(value):
                return cmd

        if value.startswith("history "):
            for arg in ["show ", "delete ", "--page ", "--query "]:
                if arg.startswith(value[8:]):
                    return "history " + arg

        return None


ASCII_BANNER = r"""
   ____ _     ___       ____                      _                 _           
  / ___| |   |_ _|     |  _ \  _____      ___ __ | | ___   __ _  __| | ___ _ __ 
 | |   | |    | |_____ | | | |/ _ \ \ /\ / / '_ \| |/ _ \ / _` |/ _` |/ _ \ '__|
 | |___| |___ | |_____|| |_| | (_) \ V  V /| | | | | (_) | (_| | (_| |  __/ |   
  \____|_____|___|     |____/ \___/ \_/\_/ |_| |_|_|\___/ \__,_|\__,_|\___|_|   
                                                                                
 Welcome to CLI-Downloader! Type 'help' for available commands.
"""


class InteractiveShell:
    def __init__(
        self,
        *,
        prompt: str = "cli-downloader> ",
        runner: CommandRunner | None = None,
        background_runner: BackgroundCommandRunner | None = None,
        task_service_loader: TaskServiceLoader | None = None,
        operation_runner: OperationRunner | None = None,
        batch_selection_runner: BatchSelectionRunner | None = None,
        settings_registry: SettingsRegistry | None = None,
        config_loader: ConfigLoader | None = None,
        url_opener: ExternalOpener | None = None,
        folder_opener: ExternalOpener | None = None,
        file_opener: ExternalOpener | None = None,
        browser_auth_runner: BrowserAuthRunner | None = None,
        browser_auth_validator: BrowserAuthValidator | None = None,
        startup_commands: Sequence[Sequence[str]] = (),
        runtime: ApplicationRuntime | None = None,
        plugins_dir: Path | None = None,
        cache_dir: Path | None = None,
        plugin_catalog_service: PluginCatalogService | None = None,
        shutdown_warning: ShutdownWarning | None = None,
    ) -> None:
        self._prompt = prompt
        self._runner = runner
        self._background_runner = background_runner
        self._task_service_loader = task_service_loader
        self._operation_runner = operation_runner
        self._batch_selection_runner = batch_selection_runner
        self._external_refresh_callback: Callable[[], None] | None = None
        self._settings_registry = settings_registry or SettingsRegistry()
        self._config_loader = config_loader
        self._translator = TranslationService()
        self._url_opener = url_opener or _open_url
        self._folder_opener = folder_opener or _open_folder
        self._file_opener = file_opener or _open_file
        self._browser_auth_runner = browser_auth_runner
        self._browser_auth_validator = browser_auth_validator
        self._startup_commands = tuple(tuple(command) for command in startup_commands)
        self._runtime = runtime
        self._plugins_dir = plugins_dir or Path("plugins")
        catalog_cache = cache_dir or Path("~/.cli-downloader/cache").expanduser()
        self._plugin_catalog_service = plugin_catalog_service or PluginCatalogService(
            catalog_cache
        )
        self._shutdown_warning_callback = shutdown_warning

    @property
    def translator(self) -> TranslationService:
        return self._translator

    def set_language(self, language: str) -> None:
        self._translator = TranslationService(language)

    def set_external_refresh_callback(self, callback: Callable[[], None] | None) -> None:
        self._external_refresh_callback = callback

    def notify_external_tasks(self) -> None:
        callback = self._external_refresh_callback
        if callback is not None:
            callback()

    def shutdown_warning(self) -> str | None:
        """Return a safe, user-facing warning before the Shell releases ownership."""

        callback = self._shutdown_warning_callback
        if callback is None:
            return None
        try:
            warning = callback()
        except Exception:
            # Shutdown must never be prevented by a best-effort status probe.
            return None
        return warning if isinstance(warning, str) and warning.strip() else None

    def _write_shutdown_warning(self, output_stream: TextIO) -> None:
        warning = self.shutdown_warning()
        if warning is not None:
            output_stream.write(f"Warning: {warning}\n")

    def run_batch_selection(
        self,
        items: Sequence[ListItemData],
        source_url: str | None,
        title: str | None,
        output_stream: TextIO,
        cancellation: Event,
        progress: ShellProgress,
    ) -> int:
        if self._batch_selection_runner is None:
            output_stream.write("Batch selection runner is unavailable.\n")
            return 1
        try:
            with _capture_output(output_stream):
                return self._batch_selection_runner(
                    items, source_url, title, cancellation, progress
                )
        except SystemExit as error:
            return int(error.code) if isinstance(error.code, int) else 1

    def run(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> int:
        if input_stream is None and output_stream is None:
            if sys.stdin.isatty() and sys.stdout.isatty():
                return TextualShellApp(self).run() or 0

        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stdout
        output_stream.write(self._translator("shell.welcome") + "\n")

        while True:
            output_stream.write(self._prompt)
            output_stream.flush()
            line = input_stream.readline()
            if line == "":
                self._write_shutdown_warning(output_stream)
                output_stream.write("\n")
                return 0

            if not self.execute(line, output_stream):
                return 0

    def execute(self, command_line: str, output_stream: TextIO) -> bool:
        keep_running, _ = self.execute_with_status(command_line, output_stream)
        return keep_running

    def execute_with_status(
        self,
        command_line: str,
        output_stream: TextIO,
    ) -> tuple[bool, int]:
        plugins = set(self._runtime.plugins_by_id.keys()) if self._runtime else set()
        command_line = _normalize_shell_command(command_line.strip(), plugins)
        if not command_line:
            return True, 0
        try:
            argv = shlex.split(command_line)
        except ValueError as error:
            self._print_error(
                output_stream,
                input_text=command_line,
                error_type="ShellParseError",
                error=str(error),
            )
            return True, 2

        command = argv[0].lower()
        if command == "help":
            self._print_help(output_stream)
            return True, 0
        if command in {"clear", "cls"}:
            if getattr(output_stream, "isatty", lambda: False)():
                output_stream.write("\x1b[2J\x1b[H")
            return True, 0
        if command in {"exit", "quit"}:
            self._write_shutdown_warning(output_stream)
            return False, 0
        if not _is_allowed_shell_command(argv):
            self._print_error(
                output_stream,
                input_text=command_line,
                error_type="ShellCommandNotAllowed",
                error="The shell currently allows read-only commands and download --dry-run.",
            )
            return True, 2

        return True, self._run_cli(argv, output_stream)

    def _run_cli(self, argv: Sequence[str], output_stream: TextIO) -> int:
        runner = self._runner
        if runner is None:
            from cli_downloader.cli.app import main

            runner = main
        try:
            with translation_context(self._translator):
                with _capture_output(output_stream):
                    return runner(argv)
        except SystemExit as error:
            return int(error.code) if isinstance(error.code, int) else 1

    def run_background(
        self,
        argv: Sequence[str],
        output_stream: TextIO,
        cancellation: Event,
        progress: ShellProgress,
    ) -> int:
        runner = self._background_runner
        if runner is None:
            return self._run_cli(argv, output_stream)
        try:
            with translation_context(self._translator):
                with _capture_output(output_stream):
                    return runner(argv, cancellation, progress)
        except SystemExit as error:
            return int(error.code) if isinstance(error.code, int) else 1

    def _print_help(self, output_stream: TextIO) -> None:
        translator = self._translator
        rows = (
            (translator("table.command"), translator("table.use"), translator("table.example")),
            ("download", translator("help.download"), "download <url>"),
            ("download --dry-run", translator("help.download_dry_run"), "download <url> --dry-run"),
            ("tasks", translator("help.tasks"), "tasks --status failed"),
            ("tasks show", translator("help.tasks_show"), "tasks show <id>"),
            ("tasks cancel", translator("help.tasks_cancel"), "tasks cancel <id>"),
            ("tasks retry", translator("help.tasks_retry"), "tasks retry <id>"),
            ("tasks resume", translator("help.tasks_resume"), "tasks resume <id>"),
            ("tasks delete", translator("help.tasks_delete"), "tasks delete <id> --dry-run"),
            ("tasks hard-retry", translator("help.tasks_hard_retry"), "tasks hard-retry <id>"),
            ("history", translator("help.history"), "history show <id>"),
            ("sites", translator("help.sites"), "sites"),
            ("config", translator("help.config"), "config show"),
            ("clear / cls", translator("help.clear"), "clear"),
            ("exit / quit", translator("help.exit"), "exit"),
        )
        spec = TableSpec(
            title=None,
            columns=tuple(TableColumn(label) for label in rows[0]),
            rows=tuple(tuple(row) for row in rows[1:]),
        )
        output_stream.write(f"{translator('help.title')}\n{render_table_text(spec)}\n")

    def _print_error(
        self,
        output_stream: TextIO,
        *,
        input_text: str,
        error_type: str,
        error: str,
    ) -> None:
        translator = self._translator
        output_stream.write(
            f"{translator('shell.error')}: {error}\n"
            f"{translator('shell.command')}: {input_text}\n"
            f"{translator('shell.help_hint')}\n"
        )


class TextualShellApp(App[int]):
    CSS = SHELL_CSS

    BINDINGS = [
        Binding("up", "previous_command", show=False),
        Binding("down", "next_command", show=False),
        Binding("escape", "show_console", show=False),
        Binding("f5", "refresh_activity", show=False),
        Binding("ctrl+shift+c", "copy_current_block", show=False),
        Binding("ctrl+q", "quit_shell", show=False),
    ]

    def __init__(
        self,
        shell: InteractiveShell,
        *,
        release_update_service: ReleaseUpdateService | None = None,
    ) -> None:
        super().__init__()
        self.register_theme(CLI_DOWNLOADER_THEME)
        self.theme = CLI_DOWNLOADER_THEME.name
        self._shell = shell
        shell.set_external_refresh_callback(lambda: self.call_from_thread(self._refresh_tasks))
        self._history: list[str] = []
        self._history_index = 0
        self._command_blocks: list[ShellCommandBlock] = []
        self._running_block: ShellCommandBlock | None = None
        self._operation_block: ShellCommandBlock | None = None
        self._task_service: TaskHierarchyService | None = None
        self._task_level = "roots"
        self._task_nodes: dict[str, TaskNode] = {}
        self._task_files: dict[str, TaskFileNode] = {}
        self._task_page_size = 100
        self._task_page_offset = 0
        self._task_page_total = 0
        self._task_refresh_thread: Thread | None = None
        self._task_refresh_generation = 0
        self._task_refresh_pending = False
        self._task_focus_pending = True
        self._task_layout_signature: tuple[object, ...] | None = None
        self._task_detail_signature: tuple[object, ...] | None = None
        self._task_action_signature: tuple[object, ...] | None = None
        self._task_tab_request_pending = False
        self._active_task_id: str | None = None
        self._active_batch_id: str | None = None
        self._active_batch_name: str | None = None
        self._task_source_url: str | None = None
        self._task_folder: str | None = None
        self._task_file_path: str | None = None
        self._pending_task_delete: str | None = None
        self._operation_thread: Thread | None = None
        self._operation_cancel: Event | None = None
        self._background_jobs: dict[
            int, tuple[Thread, Event, ShellCommandBlock, str | None, bool]
        ] = {}
        self._operation_source_url: str | None = None
        self._operation_plugin_id: str | None = None
        self._operation_result: OperationResult | None = None
        self._operation_session: ViewSession | None = None
        self._operation_epoch = 0
        self._selected_items: dict[tuple[str, str], ListItemData] = {}
        self._settings_path: tuple[str, ...] = ()
        self._settings_values: dict[str, object] = {}
        self._settings_secrets: dict[str, object] = {}
        self._settings_selected_key: str | None = None
        self._release_update_service = release_update_service or ReleaseUpdateService(
            repository_url=REPOSITORY_URL,
            current_version=__version__,
        )
        self._release_update_result = ReleaseUpdateResult.not_checked(__version__)
        self._release_update_checking = False
        self._browser_auth_thread: Thread | None = None
        self._original_stdout: TextIO | None = None
        self._plugin_catalog = self._shell._plugin_catalog_service.load(refresh=False)
        self._catalog_entries: dict[str, PluginCatalogEntry] = {
            entry.plugin_id: entry for entry in self._plugin_catalog.entries
        }
        self._fetched_remote_plugins: dict[str, dict[str, object]] = {}
        self._plugins_fetched = False
        self._installed_plugins_update_available: set[str] = set()
        self._plugin_remove_confirm = False
        self._selected_plugin_source = "local"

    def compose(self) -> ComposeResult:
        yield Static("CLI-Downloader", id="shell-title")
        with TabbedContent(initial="console", id="shell-tabs"):
            with TabPane("Console", id="console"):
                yield RichLog(id="shell-output", min_width=1, wrap=False, markup=False)
                yield Static("Ready", id="operation-status")
                yield Horizontal(
                    Input(placeholder="Command", id="shell-input"),
                    Button("Copy latest", id="copy-command", disabled=True),
                    Button(
                        "Cancel",
                        id="cancel-operation",
                        variant="error",
                        disabled=True,
                    ),
                    id="command-row",
                )
            with TabPane("Tasks", id="tasks"):
                yield Horizontal(
                    Static("Tasks", id="task-path"),
                    Static("", id="task-page-status"),
                    Button("←", id="task-back", disabled=True),
                    Button("◀", id="task-page-prev", disabled=True),
                    Button("▶", id="task-page-next", disabled=True),
                    Button("‖", id="task-pause", disabled=True),
                    Button("▶", id="task-resume", disabled=True),
                    Button("■", id="task-cancel", disabled=True),
                    Button("↺", id="task-retry", disabled=True),
                    Button("✕", id="task-delete", disabled=True),
                    Button("↻", id="refresh-tasks"),
                    id="task-toolbar",
                )
                yield DataTable(id="task-table", classes="activity-table", cursor_type="row")
                yield DataTable(
                    id="task-file-table",
                    classes="activity-table",
                    cursor_type="row",
                )
                with Horizontal(id="task-detail-row", classes="activity-detail"):
                    yield Static(id="task-detail")
                    with Vertical(id="task-detail-actions"):
                        yield Button("Open URL", id="task-source", disabled=True)
                        yield Button("Open Folder", id="task-folder", disabled=True)
            with TabPane("Settings", id="settings"):
                yield Horizontal(
                    Static("Settings", id="settings-path"),
                    Button("←", id="settings-back", disabled=True),
                    Button("Save", id="settings-save", disabled=True),
                    Button("Reload", id="settings-reload"),
                    Button("Check updates", id="settings-update-check-btn", disabled=True),
                    Button("Open release", id="settings-release-btn", disabled=True),
                    Button("GitHub", id="settings-github-btn", disabled=True),
                    id="settings-toolbar",
                )
                yield DataTable(id="settings-table", cursor_type="row")
                yield Static("Select a category.", id="settings-detail")
                yield Horizontal(
                    Input(placeholder="Select a setting", id="settings-editor", disabled=True),
                    Select([], prompt="Select a value", id="settings-select", disabled=True),
                    Button("Apply", id="settings-apply", disabled=True),
                    Button("Clear", id="settings-clear", disabled=True),
                    id="settings-editor-row",
                )
            with TabPane("Plugins", id="plugins"):
                yield Static("Plugin catalog: loading...", id="plugin-catalog-status")
                yield Static(
                    "Enter a Git URL or select a catalog plugin from the list to install.",
                    id="plugin-info",
                )
                yield Horizontal(
                    Input(placeholder="Git URL to install...", id="plugin-install-url"),
                    Button("Install", id="plugin-install-btn"),
                    Button("Update", id="plugin-update-btn", disabled=True),
                    Button("Remove", id="plugin-remove-btn", disabled=True),
                    Button("GitHub", id="plugin-github-btn", disabled=True),
                    id="plugin-toolbar",
                )
                yield DataTable(id="plugins-table", cursor_type="row")
                yield Static(
                    "Select a plugin to inspect its details.",
                    id="plugin-detail",
                    classes="activity-detail",
                )
            with TabPane("Operation", id="operation"):
                yield Horizontal(
                    Static("No active operation.", id="operation-view-status"),
                    Button("Select page", id="select-page", disabled=True),
                    Button("Select all", id="select-all-pages", disabled=True),
                    Button("Deselect all", id="deselect-all", disabled=True),
                    Button(
                        "Cancel",
                        id="cancel-operation-view",
                        variant="error",
                        disabled=True,
                    ),
                    id="operation-toolbar",
                )
                yield Vertical(id="operation-host")

    def on_mount(self) -> None:
        if not isinstance(sys.stdout, _ThreadLocalOutput):
            self._original_stdout = sys.stdout
            sys.stdout = _ThreadLocalOutput(sys.stdout)
        task_table = self.query_one("#task-table", DataTable)
        self._task_column_keys = {
            "number": task_table.add_column("#", key="number"),
            "type": task_table.add_column("Type", key="type"),
            "name": task_table.add_column("Name", key="name"),
            "site": task_table.add_column("Site", key="site"),
            "progress": task_table.add_column("Progress", key="progress"),
            "status": task_table.add_column("Status", key="status"),
            "created": task_table.add_column("Created", key="created"),
        }
        file_table = self.query_one("#task-file-table", DataTable)
        file_table.add_column("#", key="index")
        file_table.add_column("File", key="name")
        file_table.add_column("Progress", key="progress")
        file_table.add_column("Status", key="status")
        file_table.add_column("Attempts", key="attempts")
        file_table.add_column("Location", key="location")
        self.query_one("#task-file-table", DataTable).display = False
        self.query_one("#settings-select", Select).display = False
        self.query_one("#settings-table", DataTable).add_columns("#", "Setting", "Value", "Access")
        plugins_table = self.query_one("#plugins-table", DataTable)
        self._plugin_column_keys = {
            "id": plugins_table.add_column("ID", key="id"),
            "name": plugins_table.add_column("Name", key="name"),
            "version": plugins_table.add_column("Version", key="version"),
            "source": plugins_table.add_column("Source", key="source"),
            "author": plugins_table.add_column("Author", key="author"),
            "description": plugins_table.add_column("Description", key="description"),
            "status": plugins_table.add_column("Status", key="status"),
        }
        self._refresh_tasks()
        self._load_settings()
        self._load_plugins()
        self._apply_translations(reset_neutral=True)
        self._show_settings_level()
        self._resize_task_columns()
        self.set_interval(2.0, self._refresh_tasks_if_active)
        shell_input = self.query_one("#shell-input", Input)
        shell_input.suggester = CommandSuggester()
        if self.query_one("#shell-tabs", TabbedContent).active == "console":
            shell_input.focus()
        self.query_one("#shell-output", RichLog).write(ASCII_BANNER)
        for argv in self._shell._startup_commands:
            self._start_background(shlex.join(argv), argv)

    def _apply_translations(self, *, reset_neutral: bool = False) -> None:
        """Refresh static Shell chrome after the configured language is loaded."""
        translator = self._shell.translator
        tabs = self.query_one("#shell-tabs", TabbedContent)
        for pane_id, key in (
            ("console", "tab.console"),
            ("tasks", "tab.tasks"),
            ("settings", "tab.settings"),
            ("plugins", "tab.plugins"),
            ("operation", "tab.operation"),
        ):
            tabs.get_tab(pane_id).label = translator(key)

        for button_id, key in (
            ("copy-command", "common.copy_latest"),
            ("cancel-operation", "common.cancel"),
            ("settings-back", "common.back"),
            ("settings-save", "common.save"),
            ("settings-reload", "common.reload"),
            ("settings-update-check-btn", "settings.action.check_updates"),
            ("settings-release-btn", "settings.action.open_release"),
            ("settings-github-btn", "common.github"),
            ("settings-apply", "common.apply"),
            ("settings-clear", "common.clear"),
            ("plugin-install-btn", "common.install"),
            ("plugin-update-btn", "common.update"),
            ("plugin-remove-btn", "common.remove"),
            ("plugin-github-btn", "common.github"),
            ("select-page", "common.select_page"),
            ("select-all-pages", "common.select_all"),
            ("deselect-all", "common.deselect_all"),
            ("cancel-operation-view", "common.cancel"),
        ):
            self.query_one(f"#{button_id}", Button).label = translator(key)

        for table_id, columns in (
            (
                "#task-table",
                {
                    "number": "table.id",
                    "type": "table.type",
                    "name": "table.name",
                    "site": "table.site",
                    "progress": "table.progress",
                    "status": "table.status",
                    "created": "table.created",
                },
            ),
            (
                "#task-file-table",
                {
                    "0": "table.id",
                    "1": "table.file",
                    "2": "table.progress",
                    "3": "table.status",
                    "4": "table.attempts",
                    "5": "table.location",
                },
            ),
            (
                "#settings-table",
                {
                    "0": "table.id",
                    "1": "table.setting",
                    "2": "table.value",
                    "3": "table.access",
                },
            ),
            (
                "#plugins-table",
                {
                    "id": "table.id",
                    "name": "table.name",
                    "version": "table.version",
                    "source": "table.source",
                    "author": "table.author",
                    "description": "table.description",
                    "status": "table.status",
                },
            ),
        ):
            table = self.query_one(table_id, DataTable)
            for column_key, translation_key in columns.items():
                if table_id in {"#settings-table", "#task-file-table"}:
                    column = list(table.columns.values())[int(column_key)]
                else:
                    keys = {
                        "#task-table": self._task_column_keys,
                        "#plugins-table": self._plugin_column_keys,
                    }[table_id]
                    column = table.columns[keys[column_key]]
                column.label = Text(translator(translation_key))
            table.refresh(layout=True)

        task_primary_is_file = self._task_level == "files"
        for button_id, translation_key in (
            (
                "task-source",
                "tooltip.open_file" if task_primary_is_file else "tooltip.open_source",
            ),
            ("task-folder", "tooltip.open_folder"),
            ("task-back", "tooltip.back"),
            ("task-pause", "tooltip.pause"),
            ("task-resume", "tooltip.resume"),
            ("task-cancel", "tooltip.cancel_keep_files"),
            ("task-retry", "tooltip.retry"),
            ("task-delete", "tooltip.delete_keep_files"),
            ("refresh-tasks", "tooltip.refresh_tasks"),
            ("settings-save", "tooltip.save_new_tasks"),
            ("settings-reload", "tooltip.reload_settings"),
            ("settings-back", "tooltip.back"),
            ("settings-clear", "tooltip.clear_secret"),
            ("copy-command", "tooltip.copy_latest"),
        ):
            self.query_one(f"#{button_id}", Button).tooltip = translator(translation_key)
        self.query_one("#task-source", Button).label = translator(
            "task.action.open_file" if task_primary_is_file else "task.action.open_url"
        )
        self.query_one("#task-folder", Button).label = translator("task.action.open_folder")

        self.query_one("#shell-input", Input).placeholder = translator("common.command")
        self.query_one("#settings-editor", Input).placeholder = translator("common.select_setting")
        self.query_one("#settings-select", Select).prompt = translator("common.select_value")
        self._task_layout_signature = None
        self._task_detail_signature = None
        self._task_action_signature = None
        if reset_neutral:
            self.query_one("#operation-status", Static).update(translator("common.ready"))
            self.query_one("#operation-view-status", Static).update(
                translator("operation.no_active")
            )
            self.query_one("#plugin-info", Static).update(translator("plugin.info.prompt"))
            self.query_one("#plugin-detail", Static).update(translator("plugin.detail.prompt"))
            self.query_one("#plugin-catalog-status", Static).update(
                translator("plugin.catalog.loading")
            )
        if self._task_service is not None and not reset_neutral:
            self._refresh_tasks()
        if hasattr(self, "_plugin_rows"):
            self._load_plugins()

    def on_unmount(self) -> None:
        if self._original_stdout is not None and isinstance(sys.stdout, _ThreadLocalOutput):
            sys.stdout = self._original_stdout

    def on_resize(self, _event: events.Resize) -> None:
        self._resize_task_columns()
        self._resize_plugin_columns()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "plugin-install-url":
            self._install_plugin()
            return

        plugins = set(self._shell._runtime.plugins_by_id.keys()) if self._shell._runtime else set()
        command_line = _normalize_shell_command(event.value.strip(), plugins)
        event.input.clear()
        if not command_line:
            return

        self._history.append(command_line)
        self._history_index = len(self._history)
        try:
            argv = shlex.split(command_line)
        except ValueError as error:
            block = self._new_command_block(command_line)
            self._finish_command_block(
                block,
                "failed",
                f"Could not parse this command: {error}. Check the quotes and try again.",
            )
            return
        if argv and argv[0].lower() in {"clear", "cls"}:
            self._clear_console()
            return
        if self._operation_thread is not None:
            if argv and argv[0].lower() in {"exit", "quit"}:
                self._cancel_operation()
            else:
                block = self._new_command_block(command_line)
                self._finish_command_block(
                    block,
                    "failed",
                    "Another operation is running. Wait for it or press Cancel.",
                )
            return
        if argv and argv[0].lower() not in {"help", "exit", "quit"}:
            if not _is_allowed_shell_command(argv):
                block = self._new_command_block(command_line)
                self._finish_command_block(
                    block,
                    "failed",
                    "This command is not available in the Shell. "
                    "Run help to see supported commands.",
                )
                return
        if _is_embedded_operation_command(argv) and self._shell._operation_runner:
            if argv[0] == "download":
                self._start_operation_load(command_line, argv[1], {})
            elif argv[0] == "operation":
                plugin_id = argv[1]
                operation_id = argv[2] if len(argv) >= 3 else "help"
                params = {}
                for arg in argv[3:]:
                    if "=" in arg:
                        k, v = arg.split("=", 1)
                        params[k] = v
                from urllib.parse import urlencode

                fake_url = f"plugin://{plugin_id}/{operation_id}"
                if params:
                    fake_url += "?" + urlencode(params)
                self._start_operation_load(command_line, fake_url, {})
            return
        if argv and argv[0].lower() not in {"help", "exit", "quit"}:
            self._start_background(command_line, argv)
            return

        block = self._new_command_block(command_line)
        output = StringIO()
        keep_running, exit_code = self._shell.execute_with_status(
            command_line,
            output,
        )
        self._finish_command_block(
            block,
            "completed" if exit_code == 0 else "failed",
            output.getvalue(),
        )
        if not keep_running:
            self.exit(0)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh-tasks":
            self._refresh_tasks()
        elif event.button.id == "task-back":
            self._task_back()
        elif event.button.id == "task-page-prev":
            self._task_previous_page()
        elif event.button.id == "task-page-next":
            self._task_next_page()
        elif event.button.id == "task-source":
            self._open_task_source()
        elif event.button.id == "task-folder":
            self._open_task_folder()
        elif event.button.id in {"task-pause", "task-resume", "task-cancel"}:
            self._control_task(event.button.id.removeprefix("task-"))
        elif event.button.id == "task-retry":
            self._retry_task()
        elif event.button.id == "task-delete":
            self._delete_task_record()
        elif event.button.id == "settings-back":
            self._settings_back()
        elif event.button.id == "settings-apply":
            self._apply_setting()
        elif event.button.id == "settings-clear":
            self._clear_setting()
        elif event.button.id == "settings-save":
            self._save_settings()
        elif event.button.id == "settings-reload":
            self._load_settings()
            self._apply_translations()
            self._show_settings_level()
        elif event.button.id == "settings-github-btn":
            self._open_settings_github()
        elif event.button.id == "settings-update-check-btn":
            self._start_release_update_check()
        elif event.button.id == "settings-release-btn":
            self._open_settings_release()
        elif event.button.id == "copy-command":
            self.action_copy_current_block()
        elif event.button.id == "cancel-operation":
            self._cancel_operation()
        elif event.button.id == "cancel-operation-view":
            self._cancel_operation()
        elif event.button.id == "select-page":
            self._select_page()
        elif event.button.id == "select-all-pages":
            self._select_all_pages()
        elif event.button.id == "deselect-all":
            self._deselect_all()
        elif event.button.id == "plugin-install-btn":
            self._install_plugin()
        elif event.button.id == "plugin-update-btn":
            self._update_plugin()
        elif event.button.id == "plugin-remove-btn":
            if not self._plugin_remove_confirm:
                self._plugin_remove_confirm = True
                btn = self.query_one("#plugin-remove-btn", Button)
                btn.label = self._shell.translator("plugin.action.confirm_remove")
                btn.variant = "error"
            else:
                self._remove_plugin()
                self._plugin_remove_confirm = False
                btn = self.query_one("#plugin-remove-btn", Button)
                btn.label = self._shell.translator("common.remove")
                btn.variant = "default"
        elif event.button.id == "plugin-github-btn":
            self._open_plugin_github()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if not self.query("#task-detail").nodes:
            return
        row_id = str(event.row_key.value)
        if event.data_table.id == "task-table":
            self._select_task(row_id)
        elif event.data_table.id == "task-file-table":
            self._show_task_file(row_id)
        elif event.data_table.id == "settings-table":
            self._select_setting(row_id)
        elif event.data_table.id == "plugins-table":
            self._select_plugin(row_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_id = str(event.row_key.value)
        if event.data_table.id == "task-table":
            self._open_task(row_id)
        elif event.data_table.id == "settings-table":
            self._open_setting_row(row_id)
        elif event.data_table.id == "plugins-table":
            # Action on double click or enter
            pass

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        try:
            pane = getattr(event, "pane", None)
            active_tab = getattr(pane, "id", None) or self.query_one(
                "#shell-tabs", TabbedContent
            ).active
        except Exception:
            return
        if active_tab == "tasks":
            self._task_tab_request_pending = True
            tabs = self.query_one("#shell-tabs", TabbedContent)
            if tabs.active != "tasks":
                tabs.active = "tasks"
            self._prepare_tasks_tab(clear_request=False)
            # TabbedContent may emit the pane event before its internal active
            # id settles (notably during test mounts and fast startup).  Finish
            # one layout turn after the explicit request.
            self.call_after_refresh(self._prepare_tasks_tab)
        elif active_tab == "console" and self._task_tab_request_pending:
            # A TabbedContent mount can emit a stale Console event after the
            # caller already requested Tasks.  Keep the explicit request until
            # the Tasks pane has completed its first refresh turn.
            self.query_one("#shell-tabs", TabbedContent).active = "tasks"
            self._prepare_tasks_tab()
        elif active_tab == "plugins":
            self.call_after_refresh(self.call_after_refresh, self._resize_plugin_columns)
            if not self._plugins_fetched:
                self._plugins_fetched = True
                self._fetch_remote_plugins()
        elif active_tab != "tasks":
            # Invalidate a presentation worker when the user leaves Tasks. The
            # download itself continues; only its eventual UI snapshot is
            # discarded until the tab is opened again.
            self._task_refresh_generation += 1

    def _prepare_tasks_tab(self, *, clear_request: bool = True) -> None:
        tabs = self.query_one("#shell-tabs", TabbedContent)
        if tabs.active != "tasks":
            tabs.active = "tasks"
        if clear_request:
            self._task_tab_request_pending = False
        self.refresh(layout=True)
        self._resize_task_columns()
        if self._task_nodes:
            self._task_focus_pending = True
            self.query_one("#task-table", DataTable).focus()
            if self._active_task_id is None:
                self._select_task(next(iter(self._task_nodes)))
        if callable(getattr(self._task_service, "list_root_summaries", None)) or not self._task_nodes:
            self._refresh_tasks()

    @work(thread=True)
    def _fetch_remote_plugins(self, *, force: bool = False) -> None:
        changed = False
        try:
            # ── Step 0: validate registry (remove stale entries) ──
            registry = load_registry(self._shell._plugins_dir)
            stale: list[str] = []
            for plugin_id in registry:
                if (self._shell._plugins_dir / plugin_id).is_dir():
                    continue
                else:
                    stale.append(plugin_id)
            if stale:
                changed = True
                for plugin_id in stale:
                    registry.pop(plugin_id, None)
                from cli_downloader.plugins.registry import save_registry

                save_registry(self._shell._plugins_dir, registry)

            # ── Step 1: refresh and merge Official + Community discovery data ──
            catalog = self._shell._plugin_catalog_service.load(refresh=True, force=force)
            if catalog != self._plugin_catalog:
                changed = True
            self._plugin_catalog = catalog
            self._catalog_entries = {
                entry.plugin_id: entry for entry in self._plugin_catalog.entries
            }

            # ── Step 2: fetch plugin.json metadata for catalog plugins ──
            for catalog_entry in self._plugin_catalog.entries:
                url = catalog_entry.repository_url
                metadata = fetch_remote_plugin_metadata(url)
                if not metadata.get("fetch_error") and metadata.get("id") != catalog_entry.plugin_id:
                    metadata["catalog_error"] = (
                        f"plugin.json ID {metadata.get('id')!r} does not match "
                        f"catalog ID {catalog_entry.plugin_id!r}"
                    )
                if self._fetched_remote_plugins.get(url) != metadata:
                    changed = True
                self._fetched_remote_plugins[url] = metadata

            # ── Step 3: check updates for every installed plugin that has a git_url ──
            for plugin_id, info in registry.items():
                git_url = info.get("git_url")
                if not git_url:
                    continue
                if check_plugin_update(self._shell._plugins_dir, plugin_id):
                    if plugin_id not in self._installed_plugins_update_available:
                        changed = True
                    self._installed_plugins_update_available.add(plugin_id)
                else:
                    if plugin_id in self._installed_plugins_update_available:
                        changed = True
                    self._installed_plugins_update_available.discard(plugin_id)
        except Exception:
            pass

        if changed:
            self.call_from_thread(self._load_plugins)
        self.call_from_thread(self._show_catalog_status)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "settings-select":
            self.query_one("#settings-apply", Button).disabled = self._settings_selected_key is None

    def on_core_page_view_action_selected(self, event: CorePageView.ActionSelected) -> None:
        self._operation_session = event.session
        self._merge_visible_selection(event.session)
        source_url = self._operation_source_url
        if source_url is None:
            return
        if event.action.kind is ActionKind.INVOKE_OPERATION:
            self._start_operation_load(
                f"download {source_url}",
                source_url,
                dict(event.action.params),
                session=event.session,
            )
        elif event.action.kind is ActionKind.DOWNLOAD:
            items = tuple(self._selected_items.values())
            if not items:
                self._update_selection_status()
                return
            self.query_one("#shell-tabs", TabbedContent).active = "console"
            self._start_background(
                f"download {source_url}",
                (),
                selected_items=items,
                block=self._operation_block,
                operation_submission=True,
            )

    def on_core_page_view_selection_changed(self, event: CorePageView.SelectionChanged) -> None:
        self._operation_session = event.session
        self._merge_visible_selection(
            event.session,
            single_selection=event.single_selection,
        )

    def on_core_page_view_cancelled(self, event: CorePageView.Cancelled) -> None:
        self._operation_session = event.session
        self.action_show_console()

    def on_core_page_view_back_requested(self, event: CorePageView.BackRequested) -> None:
        self._operation_session = event.session
        self.action_show_console()

    def action_previous_command(self) -> None:
        if self.query_one("#shell-input", Input).has_focus and self._history:
            self._history_index = max(0, self._history_index - 1)
            self._set_input(self._history[self._history_index])

    def action_next_command(self) -> None:
        if not self.query_one("#shell-input", Input).has_focus:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._set_input(self._history[self._history_index])
        else:
            self._history_index = len(self._history)
            self._set_input("")

    def action_quit_shell(self) -> None:
        if self._operation_cancel is not None:
            self._cancel_operation()
            return
        warning = self._shell.shutdown_warning()
        if warning is not None:
            self.notify(f"Warning: {warning}", severity="warning")
        self.exit(0)

    def action_show_console(self) -> None:
        if (
            self.query_one("#shell-tabs", TabbedContent).active == "tasks"
            and self._task_level != "roots"
        ):
            self._task_back()
            return
        self.query_one("#shell-tabs", TabbedContent).active = "console"
        self.query_one("#shell-input", Input).focus()

    def action_refresh_activity(self) -> None:
        active = self.query_one("#shell-tabs", TabbedContent).active
        if active == "tasks":
            self._refresh_tasks()
        elif active == "plugins":
            self.query_one("#plugin-info", Static).update(
                self._shell.translator("plugin.catalog.refreshing")
            )
            self._fetch_remote_plugins(force=True)

    def action_copy_current_block(self) -> None:
        if not self._command_blocks:
            return
        self.copy_to_clipboard(self._block_plain_text(self._command_blocks[-1]))
        self.query_one("#operation-status", Static).update("Copied latest command block")

    def _set_input(self, value: str) -> None:
        control = self.query_one("#shell-input", Input)
        control.value = value
        control.cursor_position = len(value)

    def _new_command_block(self, command_line: str) -> ShellCommandBlock:
        block = ShellCommandBlock(command=command_line)
        self._command_blocks.append(block)
        self._render_console()
        return block

    def _finish_command_block(
        self,
        block: ShellCommandBlock,
        status: str,
        output: str = "",
        *,
        detail: str | None = None,
    ) -> None:
        block.status = status
        block.output = _format_shell_output(output)
        block.detail = detail or {
            "completed": self._shell.translator("status.completed"),
            "failed": self._shell.translator("status.failed"),
            "cancelled": self._shell.translator("status.cancelled"),
            "ready": self._shell.translator("operation.ready_selection"),
        }.get(status, status.title())
        block.elapsed_seconds = monotonic() - block.started_at
        self._render_console()

    def _render_console(self) -> None:
        log = self.query_one("#shell-output", RichLog)
        log.clear()
        log.write(ASCII_BANNER)
        width = max(40, log.content_size.width)
        for index, block in enumerate(self._command_blocks):
            if index:
                log.write("")
            log.write(self._render_command_block(block, width=width))
        self.query_one("#copy-command", Button).disabled = not self._command_blocks

    def _render_command_block(
        self,
        block: ShellCommandBlock,
        *,
        width: int | None = None,
    ) -> Text:
        status = {
            "running": ("...", self._shell.translator("status.in_progress"), "#ffaa55"),
            "ready": ("+", self._shell.translator("status.ready"), "#67b279"),
            "completed": ("+", self._shell.translator("status.completed"), "#67b279"),
            "failed": ("x", self._shell.translator("status.failed"), "#d96c6c"),
            "cancelled": ("!", self._shell.translator("status.cancelled"), "#999999"),
        }.get(block.status, ("!", block.status.title(), "#999999"))
        symbol, label, color = status
        rendered = Text()
        lines: list[Text] = [Text(f"> {block.command}", style="bold #e58a32")]
        if block.output:
            for line in block.output.splitlines():
                output_line = Text("  ")
                match = re.match(r"(\s*\d+\.\s+)(.*)", line)
                if match:
                    output_line.append(match.group(1), style="#777777")
                    output_line.append(match.group(2), style="#e8e8e8")
                else:
                    output_line.append(line, style="#e8e8e8")
                lines.append(output_line)
        footer = Text(f"  {symbol} {label}", style=f"bold {color}")
        if block.detail and block.detail != label:
            footer.append(f"  {block.detail}", style="#999999")
        footer.append(f"  {block.elapsed:.2f}s", style="#777777")
        lines.append(footer)
        for index, line in enumerate(lines):
            if width is not None and len(line.plain) < width:
                line.append(" " * (width - len(line.plain)))
            line.stylize("on #222222")
            rendered.append_text(line)
            if index < len(lines) - 1:
                rendered.append("\n")
        return rendered

    def _block_plain_text(self, block: ShellCommandBlock) -> str:
        return self._render_command_block(block).plain

    def _clear_console(self) -> None:
        self._command_blocks.clear()
        log = self.query_one("#shell-output", RichLog)
        log.clear()
        self.query_one("#copy-command", Button).disabled = True
        self.query_one("#operation-status", Static).update(
            self._shell.translator("console.cleared")
        )

    def _start_background(
        self,
        command_line: str,
        argv: Sequence[str],
        *,
        selected_items: Sequence[ListItemData] = (),
        block: ShellCommandBlock | None = None,
        operation_submission: bool = False,
    ) -> None:
        command_argv = list(argv)
        task_id = _shell_task_id(command_argv)
        if command_argv and command_argv[0] == "download" and "--json" not in command_argv:
            command_argv.append("--json")
        operation_name = command_argv[0] if command_argv else "command"
        concurrent = bool(selected_items) or operation_name in ("download", "batches")
        cancellation = Event()
        job_id = id(cancellation)
        block = block or self._new_command_block(command_line)
        block.status = "running"
        block.detail = f"Running {_display_name(operation_name)}"
        block.elapsed_seconds = None
        self._running_block = block
        self._render_console()
        self.query_one("#cancel-operation", Button).disabled = False
        self.query_one("#operation-status", Static).update(f"Running: {operation_name}")
        operation_epoch = self._operation_epoch if operation_submission else None
        operation_source_url = self._operation_source_url if operation_submission else None
        operation_title = (
            self._operation_result.data.title
            if operation_submission
            and self._operation_result is not None
            and isinstance(self._operation_result.data, ListPageData)
            else None
        )
        thread = Thread(
            target=self._run_background,
            args=(
                command_line,
                tuple(command_argv),
                tuple(selected_items),
                block,
                cancellation,
                job_id,
                concurrent,
                operation_submission,
                operation_epoch,
                operation_source_url,
                operation_title,
            ),
            daemon=True,
        )
        if concurrent:
            self._background_jobs[job_id] = (
                thread,
                cancellation,
                block,
                task_id,
                concurrent,
            )
        else:
            self._operation_thread = thread
            self._operation_cancel = cancellation
        thread.start()

    def _start_operation_load(
        self,
        command_line: str,
        source_url: str,
        params: Mapping[str, object],
        *,
        session: ViewSession | None = None,
    ) -> None:
        runner = self._shell._operation_runner
        if runner is None:
            return
        if session is None:
            self._operation_epoch += 1
            self._selected_items.clear()
            self._operation_plugin_id = None
            self._operation_block = self._new_command_block(command_line)
        block = self._operation_block
        if block is not None:
            block.status = "running"
            block.detail = self._shell.translator("operation.loading")
            block.elapsed_seconds = None
            self._running_block = block
            self._render_console()
        cancellation = Event()
        self._operation_cancel = cancellation
        self._operation_source_url = source_url
        self._operation_session = session
        self.query_one("#operation-status", Static).update(
            self._shell.translator("operation.loading")
        )
        self.query_one("#operation-view-status", Static).update(
            self._shell.translator("operation.loading_short")
        )
        self.query_one("#cancel-operation", Button).disabled = False
        self.query_one("#cancel-operation-view", Button).disabled = False
        self._enable_selection_buttons(False)
        self.query_one("#shell-tabs", TabbedContent).active = "operation"
        thread = Thread(
            target=self._run_operation_load,
            args=(command_line, source_url, dict(params), cancellation),
            daemon=True,
        )
        self._operation_thread = thread
        thread.start()

    def _run_operation_load(
        self,
        command_line: str,
        source_url: str,
        params: Mapping[str, object],
        cancellation: Event,
    ) -> None:
        try:
            plugin_id, result = self._shell._operation_runner(source_url, params)
            error = None
        except Exception as caught:
            plugin_id = "unknown"
            result = None
            error = f"{type(caught).__name__}: {caught}"
        self.call_from_thread(
            self._finish_operation_load,
            command_line,
            plugin_id,
            result,
            error,
            cancellation.is_set(),
        )

    async def _finish_operation_load(
        self,
        command_line: str,
        plugin_id: str,
        result: OperationResult | None,
        error: str | None,
        cancelled: bool,
    ) -> None:
        self._operation_thread = None
        self._operation_cancel = None
        self._running_block = None
        self.query_one("#cancel-operation", Button).disabled = True
        self.query_one("#cancel-operation-view", Button).disabled = True
        if cancelled:
            if self._operation_block is not None:
                self._finish_command_block(
                    self._operation_block,
                    "cancelled",
                )
            self.query_one("#operation-view-status", Static).update(
                self._shell.translator("operation.cancelled")
            )
            self.query_one("#operation-status", Static).update(
                self._shell.translator("operation.cancelled")
            )
            return
        if error is not None or result is None:
            message = error or "Operation returned no result"
            if self._operation_block is not None:
                self._finish_command_block(
                    self._operation_block,
                    "failed",
                    message,
                )
            self.query_one("#operation-view-status", Static).update(message)
            self.query_one("#operation-status", Static).update(
                self._shell.translator("operation.failed")
            )
            return
        if result.result_kind is ResultKind.GALLERY:
            self.query_one("#shell-tabs", TabbedContent).active = "console"
            self._start_background(
                command_line,
                shlex.split(command_line),
                block=self._operation_block,
            )
            return
        if result.page is None:
            output = json.dumps(result.to_dict(), indent=2)
            if self._operation_block is not None:
                self._finish_command_block(
                    self._operation_block,
                    ("failed" if result.result_kind is ResultKind.ERROR else "completed"),
                    output,
                )
            self.query_one("#operation-view-status", Static).update(output)
            self.query_one("#operation-status", Static).update(
                f"Finished: {result.result_kind.value}"
            )
            return

        session = self._operation_session or ViewSession(
            session_id=result.request_id,
            plugin_id=plugin_id,
            request_id=result.request_id,
        )
        self._operation_plugin_id = plugin_id
        if isinstance(result.data, ListPageData):
            visible_selected = tuple(
                item.item_id
                for item in result.data.items
                if (plugin_id, item.source_url) in self._selected_items
            )
            session = session.with_selected_items(visible_selected)
            self._enable_selection_buttons(True)
        else:
            self._enable_selection_buttons(False)
        self._operation_result = result
        self._operation_session = session
        host = self.query_one("#operation-host", Vertical)
        existing = next(iter(host.query(CorePageView)), None)
        if existing is None:
            await host.mount(CorePageView(result.page, session, id="operation-page"))
        else:
            await existing.show_page(result.page, session)
            self.app.refresh(layout=True)
        self._update_selection_status()
        if self._operation_block is not None:
            self._finish_command_block(
                self._operation_block,
                "ready",
            )
        self.query_one("#operation-status", Static).update(
            self._shell.translator("operation.ready")
        )
        self.query_one("#shell-tabs", TabbedContent).active = "operation"

    def _load_plugins(self) -> None:
        """Rebuild the plugin table from local state (no remote calls)."""
        try:
            table = self.query_one("#plugins-table", DataTable)
        except NoMatches:
            return
        previous_selection = getattr(self, "_selected_plugin_id", None)
        table.clear()

        registry = load_registry(self._shell._plugins_dir)
        plugins: dict[str, dict[str, object]] = {}
        installed_urls: set[str] = set()
        local_meta: dict[str, object] = {}
        load_errors: dict[str, str] = {}

        # ── Phase 1: installed plugins (local metadata only) ──
        if self._shell._runtime is not None:
            if hasattr(self._shell._runtime, "scan_report"):
                for res in self._shell._runtime.scan_report.results:
                    if res.package is not None:
                        local_meta[res.package.metadata.plugin_id] = res.package.metadata
                    elif res.status is PluginScanStatus.INVALID:
                        for plugin_id in registry:
                            plugin_dir = (self._shell._plugins_dir / plugin_id).resolve()
                            result_folder = res.folder.resolve()
                            if result_folder == plugin_dir or result_folder.parent == plugin_dir:
                                load_errors[plugin_id] = res.error or "Plugin scan failed"

            packages_by_folder = {
                package.folder.resolve(): package
                for package in self._shell._runtime.scan_report.valid_packages
            }
            for result in self._shell._runtime.activation_report.results:
                if result.status is not PluginActivationStatus.FAILED:
                    continue
                package = packages_by_folder.get(result.folder.resolve())
                if package is not None:
                    load_errors[package.metadata.plugin_id] = (
                        result.error or "Plugin activation failed"
                    )

            for plugin_id in self._shell._runtime.plugins_by_id:
                meta = local_meta.get(plugin_id)
                local_ver = meta.version if meta else "Unknown"  # type: ignore[union-attr]
                local_desc = (
                    meta.description  # type: ignore[union-attr]
                    if meta and meta.description
                    else "Installed external plugin."
                )

                git_url = registry.get(plugin_id, {}).get("git_url", "")
                if git_url:
                    installed_urls.add(git_url)

                catalog_entry = self._catalog_entries.get(plugin_id)
                remote_meta = self._fetched_remote_plugins.get(git_url) if git_url else None
                available_version = (
                    remote_meta.get("version", "-") if remote_meta else "-"
                )
                source = str(
                    registry.get(plugin_id, {}).get(
                        "source",
                        catalog_entry.source if catalog_entry else "local",
                    )
                )
                author = catalog_entry.author if catalog_entry else None

                status = (
                    "Update Available"
                    if plugin_id in self._installed_plugins_update_available
                    else "Installed"
                )

                plugins[plugin_id] = {
                    "name": meta.display_name if meta else plugin_id.title(),  # type: ignore[union-attr]
                    "version": local_ver,
                    "available_version": available_version,
                    "source": source,
                    "author": author or "-",
                    "description": local_desc,
                    "git_url": git_url,
                    "status": status,
                    "installed": True,
                    "requires_core_api": meta.requires_core_api if meta else None,  # type: ignore[union-attr]
                }

        # ── Phase 1.5: registry entries not (yet) in the runtime (e.g. installed, pending restart) ──
        for plugin_id, info in registry.items():
            if plugin_id in plugins:
                continue
            plugin_dir = self._shell._plugins_dir / plugin_id
            if not plugin_dir.is_dir():
                continue
            git_url = info.get("git_url", "")
            if git_url:
                installed_urls.add(git_url)
            version = _read_active_marker(plugin_dir) or "Unknown"
            meta = local_meta.get(plugin_id)
            catalog_entry = self._catalog_entries.get(plugin_id)
            remote_meta = self._fetched_remote_plugins.get(git_url) if git_url else None
            load_error = load_errors.get(plugin_id)
            plugins[plugin_id] = {
                "name": meta.display_name if meta else plugin_id,  # type: ignore[union-attr]
                "version": meta.version if meta else version,  # type: ignore[union-attr]
                "available_version": remote_meta.get("version", "-") if remote_meta else "-",
                "source": str(info.get("source", catalog_entry.source if catalog_entry else "local")),
                "author": (catalog_entry.author if catalog_entry and catalog_entry.author else "-"),
                "description": (
                    meta.description  # type: ignore[union-attr]
                    if meta and meta.description  # type: ignore[union-attr]
                    else (
                        "Plugin failed to load."
                        if load_error
                        else "Installed — restart shell to load."
                    )
                ),
                "git_url": git_url,
                "status": "Load Failed" if load_error else "Restart Required",
                "installed": True,
                "requires_core_api": meta.requires_core_api if meta else None,  # type: ignore[union-attr]
                "load_error": load_error,
            }

        # ── Phase 2: not-installed Official and Community catalog plugins ──
        for catalog_entry in self._plugin_catalog.entries:
            git_url = catalog_entry.repository_url
            if git_url in installed_urls:
                continue

            entry = self._fetched_remote_plugins.get(git_url)
            if entry:
                if entry.get("catalog_error"):
                    status = "Invalid Catalog Entry"
                elif entry.get("compatibility_error"):
                    status = "Incompatible"
                else:
                    status = "Not Installed"
                plugins[catalog_entry.plugin_id] = {
                    "name": entry["name"],
                    "version": entry.get("version", "-"),
                    "available_version": entry.get("version", "-"),
                    "source": catalog_entry.source,
                    "author": catalog_entry.author or "-",
                    "description": entry["description"],
                    "git_url": git_url,
                    "status": status,
                    "installed": False,
                    "requires_core_api": entry.get("requires_core_api"),
                    "catalog_error": entry.get("catalog_error"),
                    "compatibility_error": entry.get("compatibility_error"),
                    "fetch_error": entry.get("fetch_error"),
                }
            else:
                plugins[catalog_entry.plugin_id] = {
                    "name": catalog_entry.plugin_id,
                    "version": "-",
                    "available_version": "-",
                    "source": catalog_entry.source,
                    "author": catalog_entry.author or "-",
                    "description": "Fetching remote information...",
                    "git_url": git_url,
                    "status": "Not Installed",
                    "installed": False,
                }

        self._plugin_rows = plugins
        for p_id, info in sorted(plugins.items()):
            table.add_row(
                p_id,
                _plugin_cell(info["name"]),
                _plugin_cell(info.get("version", "-")),
                _plugin_source_cell(str(info.get("source", "local")), self._shell.translator),
                _plugin_cell(info.get("author", "-")),
                _plugin_cell(_plugin_description(info["description"], self._shell.translator)),
                _plugin_cell(_plugin_status_label(info["status"], self._shell.translator)),
                key=p_id,
            )

        self._selected_plugin_id = None
        self._selected_plugin_git_url: str | None = None
        self._selected_plugin_status: str | None = None
        self._selected_plugin_installed = False
        self.query_one("#plugin-update-btn", Button).disabled = True
        self.query_one("#plugin-remove-btn", Button).disabled = True
        self.query_one("#plugin-github-btn", Button).disabled = True
        self._resize_plugin_columns()

        self._plugin_remove_confirm = False
        btn = self.query_one("#plugin-remove-btn", Button)
        btn.label = self._shell.translator("common.remove")
        btn.variant = "default"
        if previous_selection in plugins:
            self._select_plugin(previous_selection)
        else:
            self._show_plugin_detail(None)

    def _select_plugin(self, plugin_id: str) -> None:
        self._plugin_remove_confirm = False
        btn = self.query_one("#plugin-remove-btn", Button)
        btn.label = self._shell.translator("common.remove")
        btn.variant = "default"

        self._selected_plugin_id = plugin_id
        info = getattr(self, "_plugin_rows", {}).get(plugin_id)
        if info is None:
            self._selected_plugin_status = None
            self._selected_plugin_installed = False
            self.query_one("#plugin-update-btn", Button).disabled = True
            self.query_one("#plugin-remove-btn", Button).disabled = True
            self._show_plugin_detail(None)
            return

        self._selected_plugin_status = str(info.get("status", ""))
        self._selected_plugin_installed = bool(info.get("installed", False))

        # Look up git URL from the installed registry or merged catalog.
        self._selected_plugin_git_url = None
        self._selected_plugin_source = "local"
        registry = load_registry(self._shell._plugins_dir)
        if plugin_id in registry:
            self._selected_plugin_git_url = registry[plugin_id].get("git_url")
            self._selected_plugin_source = registry[plugin_id].get("source", "custom")

        if not self._selected_plugin_git_url:
            catalog_entry = self._catalog_entries.get(plugin_id)
            if catalog_entry is not None:
                self._selected_plugin_git_url = catalog_entry.repository_url
                self._selected_plugin_source = catalog_entry.source

        self.query_one("#plugin-update-btn", Button).disabled = not self._selected_plugin_installed
        self.query_one("#plugin-remove-btn", Button).disabled = not self._selected_plugin_installed
        self.query_one("#plugin-github-btn", Button).disabled = (
            self._selected_plugin_git_url is None
        )
        self._show_plugin_detail(plugin_id)

    def _show_plugin_detail(self, plugin_id: str | None) -> None:
        detail = self.query_one("#plugin-detail", Static)
        if plugin_id is None:
            detail.update(self._shell.translator("plugin.detail.prompt"))
            return

        info = getattr(self, "_plugin_rows", {}).get(plugin_id)
        if info is None:
            detail.update(self._shell.translator("plugin.detail.prompt_unavailable"))
            return

        source = str(info.get("source", "local"))
        lines = [
            f"{self._shell.translator('plugin.detail.name')}: {info.get('name', plugin_id)}",
            f"{self._shell.translator('plugin.detail.id')}: {plugin_id}",
            f"{self._shell.translator('plugin.detail.source')}: "
            f"{self._shell.translator(f'plugin.source.{source.lower()}')}",
            f"{self._shell.translator('plugin.detail.author')}: {info.get('author', '-')}",
            f"{self._shell.translator('plugin.detail.description')}: "
            f"{info.get('description', '-')}",
            f"{self._shell.translator('plugin.detail.installed_version')}: "
            f"{info.get('version', '-')}",
            f"{self._shell.translator('plugin.detail.available_version')}: "
            f"{info.get('available_version', '-')}",
            f"{self._shell.translator('plugin.detail.core_compatibility')}: "
            f"{info.get('requires_core_api') or self._shell.translator('plugin.detail.not_specified')}",
            f"{self._shell.translator('plugin.detail.repository')}: {info.get('git_url') or '-'}",
            f"{self._shell.translator('plugin.detail.status')}: "
            f"{_plugin_status_label(info.get('status', '-'), self._shell.translator)}",
        ]
        diagnostics = [
            str(info[key])
            for key in ("load_error", "catalog_error", "compatibility_error", "fetch_error")
            if info.get(key)
        ]
        if diagnostics:
            lines.append(
                f"{self._shell.translator('plugin.detail.diagnostics')}: {'; '.join(diagnostics)}"
            )
        detail.update("\n".join(lines))

    def _install_plugin(self) -> None:
        url_input = self.query_one("#plugin-install-url", Input)
        custom_url = url_input.value.strip()
        git_url = custom_url or self._selected_plugin_git_url
        if not git_url:
            return

        try:
            if not custom_url:
                catalog_entry = self._catalog_entries.get(self._selected_plugin_id or "")
                metadata = (
                    self._fetched_remote_plugins.get(catalog_entry.repository_url)
                    if catalog_entry is not None
                    else None
                )
                if metadata and metadata.get("catalog_error"):
                    raise RuntimeError(str(metadata["catalog_error"]))
                if metadata and metadata.get("compatibility_error"):
                    raise RuntimeError(str(metadata["compatibility_error"]))
            self.query_one("#plugin-info", Static).update(
                self._shell.translator("plugin.info.installing", url=git_url)
            )
            source = "custom" if custom_url else self._selected_plugin_source
            install_plugin(
                self._shell._plugins_dir,
                git_url,
                source=source,
                catalog_id=CATALOG_REPOSITORY if source in {"official", "community"} else None,
            )
            self.query_one("#plugin-info", Static).update(
                "[green]"
                + self._shell.translator("plugin.info.installed", url=git_url)
                + "[/green]"
            )
            url_input.value = ""
            self._load_plugins()
            self._fetch_remote_plugins()
        except Exception as e:
            self.query_one("#plugin-info", Static).update(
                "[red]"
                + self._shell.translator("plugin.info.install_failed", error=e)
                + "[/red]"
            )

    def _show_catalog_status(self) -> None:
        catalog: PluginCatalog = self._plugin_catalog
        count = len(catalog.entries)
        if catalog.status == "stale":
            message = self._shell.translator("plugin.catalog.stale", count=count)
        elif catalog.status == "offline":
            message = self._shell.translator("plugin.catalog.offline", count=count)
        elif catalog.status == "empty":
            message = self._shell.translator("plugin.catalog.empty")
        else:
            message = self._shell.translator(
                "plugin.catalog.status",
                status=catalog.status,
                count=count,
            )
        self.query_one("#plugin-catalog-status", Static).update(message)

    def _update_plugin(self) -> None:
        if not self._selected_plugin_id or not self._selected_plugin_installed:
            return

        try:
            self.query_one("#plugin-info", Static).update(
                self._shell.translator(
                    "plugin.info.updating",
                    plugin_id=self._selected_plugin_id,
                )
            )
            updated = update_plugin(self._shell._plugins_dir, self._selected_plugin_id)
            if updated:
                self.query_one("#plugin-info", Static).update(
                    "[green]"
                    + self._shell.translator(
                        "plugin.info.updated",
                        plugin_id=self._selected_plugin_id,
                    )
                    + "[/green]"
                )
            else:
                self.query_one("#plugin-info", Static).update(
                    "[yellow]"
                    + self._shell.translator(
                        "plugin.info.up_to_date",
                        plugin_id=self._selected_plugin_id,
                    )
                    + "[/yellow]"
                )

            # Since update is done or up-to-date, we can clear it from the update set
            self._installed_plugins_update_available.discard(self._selected_plugin_id)
            self._load_plugins()
            self._fetch_remote_plugins()
        except Exception as e:
            msg = str(e)
            self.query_one("#plugin-info", Static).update(
                "[red]"
                + self._shell.translator("plugin.info.update_failed", error=msg)
                + "[/red]"
            )

    def _remove_plugin(self) -> None:
        if not self._selected_plugin_id or not self._selected_plugin_installed:
            return

        try:
            self.query_one("#plugin-info", Static).update(
                self._shell.translator(
                    "plugin.info.removing",
                    plugin_id=self._selected_plugin_id,
                )
            )
            remove_plugin(self._shell._plugins_dir, self._selected_plugin_id)
            self.query_one("#plugin-info", Static).update(
                "[green]"
                + self._shell.translator(
                    "plugin.info.removed",
                    plugin_id=self._selected_plugin_id,
                )
                + "[/green]"
            )
            self._load_plugins()
            self._fetch_remote_plugins()
            self._selected_plugin_id = None
            self._selected_plugin_status = None
            self._selected_plugin_installed = False
        except Exception as e:
            self.query_one("#plugin-info", Static).update(
                "[red]"
                + self._shell.translator("plugin.info.remove_failed", error=e)
                + "[/red]"
            )

    def _open_plugin_github(self) -> None:
        if self._selected_plugin_git_url:
            url = self._selected_plugin_git_url
            if url.endswith(".git"):
                url = url[:-4]
            try:
                webbrowser.open(url)
            except Exception:
                pass

    def _load_settings(self) -> None:
        try:
            loaded = (
                self._shell._config_loader.load()
                if self._shell._config_loader is not None
                else None
            )
        except ConfigValidationError as error:
            self.query_one("#settings-detail", Static).update(
                self._shell.translator("settings.detail.load_error", error=error)
            )
            return
        self._settings_values = (
            dict(loaded.values)
            if loaded is not None
            else self._shell._settings_registry.schema.defaults()
        )
        self._settings_secrets = dict(loaded.secrets) if loaded is not None else {}
        self._shell.set_language(str(self._settings_values.get("interface.language", "en-US")))
        self._settings_path = ()
        self._settings_selected_key = None
        self.query_one("#settings-save", Button).disabled = True
        self._show_settings_level()

    def _show_settings_level(self) -> None:
        table = self.query_one("#settings-table", DataTable)
        table.clear()
        path = self._settings_path
        translator = self._shell.translator
        path_labels = {
            part: translator(f"settings.category.{part}")
            for part in ("general", "interface", "download", "tasks", "torrent", "network", "plugins", "about")
        }
        self.query_one("#settings-path", Static).update(
            " / ".join(
                (
                    translator("tab.settings"),
                    *(path_labels.get(part, _setting_label(part, translator)) for part in path),
                )
            )
        )
        self.query_one("#settings-back", Button).disabled = not path
        about_active = path == ("about",)
        github_button = self.query_one("#settings-github-btn", Button)
        github_button.display = about_active
        github_button.disabled = not about_active or REPOSITORY_URL is None
        update_button = self.query_one("#settings-update-check-btn", Button)
        update_button.display = about_active
        update_button.disabled = not about_active or self._release_update_checking
        release_button = self.query_one("#settings-release-btn", Button)
        release_button.display = about_active
        release_button.disabled = (
            not about_active
            or self._release_update_result.status is not ReleaseUpdateStatus.UPDATE_AVAILABLE
            or self._release_update_result.release_url is None
        )
        update_text = self._release_update_text()
        rows: list[tuple[str, str, str, str]] = []
        if not path:
            rows = [
                (
                    "nav:general",
                    translator("settings.category.general"),
                    translator("settings.category.general.description"),
                    translator("settings.access.category"),
                ),
                (
                    "nav:interface",
                    translator("settings.category.interface"),
                    translator("settings.category.interface.description"),
                    translator("settings.access.category"),
                ),
                (
                    "nav:download",
                    translator("settings.category.download"),
                    translator("settings.category.download.description"),
                    translator("settings.access.category"),
                ),
                (
                    "nav:tasks",
                    translator("settings.category.tasks"),
                    translator("settings.category.tasks.description"),
                    translator("settings.access.category"),
                ),
                (
                    "nav:torrent",
                    translator("settings.category.torrent"),
                    translator("settings.category.torrent.description"),
                    translator("settings.access.category"),
                ),
                (
                    "nav:network",
                    translator("settings.category.network"),
                    translator("settings.category.network.description"),
                    translator("settings.access.category"),
                ),
                (
                    "nav:plugins",
                    translator("settings.category.plugins"),
                    translator("settings.category.plugins.description"),
                    translator("settings.access.category"),
                ),
                (
                    "nav:about",
                    translator("settings.category.about"),
                    translator("settings.category.about.description"),
                    translator("settings.access.information"),
                ),
            ]
        elif path == ("about",):
            rows = [
                (
                    "about:version",
                    translator("table.version"),
                    __version__,
                    translator("settings.access.read_only"),
                ),
                (
                    "about:repository",
                    translator("plugin.detail.repository"),
                    REPOSITORY_URL or translator("plugin.detail.unavailable"),
                    translator("settings.access.link"),
                ),
                (
                    "about:update",
                    translator("settings.update.label"),
                    update_text,
                    translator("settings.access.information"),
                ),
            ]
        elif path == ("plugins",):
            rows = [
                (
                    f"nav:{plugin_id}",
                    plugin_id,
                    translator("settings.category.plugins.description"),
                    translator("settings.access.category"),
                )
                for plugin_id in self._shell._settings_registry.plugin_ids
            ]
        elif len(path) == 2 and path[0] == "plugins":
            plugin_id = path[1]
            groups = sorted(
                {
                    _plugin_field_group(field.key, plugin_id)
                    for field in self._visible_settings()
                    if field.key.startswith(f"plugins.{plugin_id}.")
                }
            )
            rows = [
                (
                    f"nav:{group}",
                    _setting_label(group, translator),
                    translator("settings.category.plugins.description"),
                    translator("settings.access.category"),
                )
                for group in groups
            ]
        else:
            rows = [
                (
                    f"field:{field.key}",
                    self._setting_label(field),
                    self._setting_display_value(field),
                    translator("settings.access.read_only")
                    if not field.editable
                    else translator("settings.access.editable"),
                )
                for field in self._fields_for_settings_path(path)
            ]
        for index, (row_id, name, value, access) in enumerate(rows, start=1):
            table.add_row(str(index), name, value, access, key=row_id)
        detail = (
                translator(
                    "settings.detail.about",
                    project=PROJECT_NAME,
                    version=__version__,
                    update=update_text,
                    repository=REPOSITORY_URL or translator("plugin.detail.unavailable"),
                )
            if path == ("about",)
            else (
                translator("settings.detail.select")
                if rows
                else translator("settings.detail.empty")
            )
        )
        self.query_one("#settings-detail", Static).update(detail)
        self._disable_setting_editor()

    def _release_update_text(self) -> str:
        translator = self._shell.translator
        if self._release_update_checking:
            return translator("settings.update.checking")
        result = self._release_update_result
        if result.status is ReleaseUpdateStatus.NOT_CHECKED:
            return translator("settings.update.not_checked")
        if result.status is ReleaseUpdateStatus.UP_TO_DATE:
            return translator(
                "settings.update.up_to_date",
                version=result.latest_version or result.current_version,
            )
        if result.status is ReleaseUpdateStatus.UPDATE_AVAILABLE:
            return translator(
                "settings.update.available",
                version=result.latest_version or "-",
            )
        if result.status is ReleaseUpdateStatus.CURRENT_NEWER:
            return translator("settings.update.current_newer")
        if result.status is ReleaseUpdateStatus.DEVELOPMENT_BUILD:
            return translator("settings.update.development_build")
        if result.status is ReleaseUpdateStatus.NO_RELEASES:
            return translator("settings.update.no_releases")
        error_code = result.error_code or "request_failed"
        if error_code not in {
            "offline",
            "rate_limited",
            "invalid_response",
            "unsupported_repository",
            "request_failed",
        }:
            error_code = "request_failed"
        return translator(f"settings.update.error.{error_code}")

    def _start_release_update_check(self):
        if self._release_update_checking:
            return None
        self._release_update_checking = True
        if self._settings_path == ("about",):
            self._show_settings_level()
        return self._check_release_update()

    @work(thread=True, exclusive=True, group="release-update")
    def _check_release_update(self) -> None:
        result = self._release_update_service.check()
        self.call_from_thread(self._finish_release_update_check, result)

    def _finish_release_update_check(self, result: ReleaseUpdateResult) -> None:
        self._release_update_result = result
        self._release_update_checking = False
        if self._settings_path == ("about",):
            self._show_settings_level()

    def _open_settings_release(self) -> None:
        release_url = self._release_update_result.release_url
        if (
            self._release_update_result.status is not ReleaseUpdateStatus.UPDATE_AVAILABLE
            or release_url is None
        ):
            return
        try:
            self._shell._url_opener(release_url)
        except Exception as error:
            self.query_one("#settings-detail", Static).update(
                self._shell.translator("settings.detail.open_release_error", error=error)
            )

    def _open_settings_github(self) -> None:
        if REPOSITORY_URL is None:
            return
        try:
            self._shell._url_opener(REPOSITORY_URL)
        except Exception as error:
            self.query_one("#settings-detail", Static).update(
                self._shell.translator("settings.detail.open_repo_error", error=error)
            )

    def _visible_settings(self) -> tuple[ConfigField, ...]:
        return tuple(
            field for field in self._shell._settings_registry.settings_fields if field.visible
        )

    def _fields_for_settings_path(self, path: tuple[str, ...]) -> tuple[ConfigField, ...]:
        if len(path) == 1 and path[0] != "plugins":
            prefix = "profile." if path[0] == "general" else f"{path[0]}."
            return tuple(
                field for field in self._visible_settings() if field.key.startswith(prefix)
            )
        if len(path) == 3 and path[0] == "plugins":
            plugin_id, group = path[1:]
            return tuple(
                field
                for field in self._visible_settings()
                if field.key.startswith(f"plugins.{plugin_id}.")
                and _plugin_field_group(field.key, plugin_id) == group
            )
        return ()

    def _open_setting_row(self, row_id: str) -> None:
        if row_id.startswith("nav:"):
            self._settings_path = (*self._settings_path, row_id.removeprefix("nav:"))
            self._show_settings_level()
        elif row_id.startswith("field:"):
            self._select_setting(row_id)

    def _select_setting(self, row_id: str) -> None:
        if not row_id.startswith("field:"):
            return
        translator = self._shell.translator
        key = row_id.removeprefix("field:")
        field = self._shell._settings_registry.field(key)
        browser_auth = field.type == "browser_auth"
        browser_connected = browser_auth and self._browser_auth_is_connected(key)
        self._settings_selected_key = key
        editor = self.query_one("#settings-editor", Input)
        select = self.query_one("#settings-select", Select)
        editor.display = not field.enum and not browser_auth
        select.display = bool(field.enum) and not browser_auth
        if field.enum:
            select.set_options(
                (self._setting_value_label(field, value), value) for value in field.enum
            )
            current = _setting_text(self._settings_values.get(key))
            select.value = current if current in field.enum else Select.BLANK
            select.disabled = not field.editable
        elif not browser_auth:
            editor.disabled = not field.editable
            editor.password = field.secret
            editor.value = "" if field.secret else _setting_text(self._settings_values.get(key))
            editor.placeholder = (
                translator("settings.field.enter_secret") if field.secret else field.description
            )
        apply = self.query_one("#settings-apply", Button)
        apply.label = (
            translator("settings.action.disconnect")
            if browser_connected
            else translator("settings.action.login")
            if browser_auth
            else translator("common.apply")
        )
        apply.disabled = not field.editable or (
            browser_auth
            and self._shell._browser_auth_runner is None
            and key not in self._settings_secrets
        )
        clear = self.query_one("#settings-clear", Button)
        clear.display = not browser_auth
        clear.disabled = not (field.secret and field.editable and key in self._settings_secrets)
        constraints = (
            translator("settings.allowed", values=", ".join(field.enum)) if field.enum else ""
        )
        self.query_one("#settings-detail", Static).update(
            f"{field.key}\n{_setting_description(field, translator)}{constraints}"
        )

    def _apply_setting(self) -> None:
        key = self._settings_selected_key
        if key is None:
            return
        field = self._shell._settings_registry.field(key)
        if not field.editable:
            return
        if field.type == "browser_auth":
            self._toggle_browser_auth(key)
            return
        try:
            if field.enum:
                raw = self.query_one("#settings-select", Select).value
                if raw is Select.BLANK:
                    raise ValueError("select a value")
                value = _parse_setting_value(raw, field)
            else:
                value = _parse_setting_value(
                    self.query_one("#settings-editor", Input).value,
                    field,
                )
            field.validate(value)
        except (ConfigValidationError, ValueError) as error:
            self.query_one("#settings-detail", Static).update(
                self._shell.translator("settings.detail.invalid", error=error)
            )
            return
        target = self._settings_secrets if field.secret else self._settings_values
        target[key] = value
        self.query_one("#settings-save", Button).disabled = self._shell._config_loader is None
        self._show_settings_level()

    def _toggle_browser_auth(self, key: str) -> None:
        if self._browser_auth_is_connected(key):
            self._settings_secrets.pop(key)
            self._save_settings()
            self.query_one("#settings-detail", Static).update(
                self._shell.translator("settings.detail.disconnected")
            )
            return
        runner = self._shell._browser_auth_runner
        if runner is None or self._browser_auth_thread is not None:
            return
        self._settings_secrets.pop(key, None)
        auth = self._shell._settings_registry.browser_auth(key)
        self.query_one("#settings-apply", Button).disabled = True
        self.query_one("#settings-detail", Static).update(
            self._shell.translator("settings.detail.login_browser")
        )

        def capture() -> None:
            try:
                session = runner(key.split(".", 2)[1], auth)
            except Exception as error:
                self.call_from_thread(self._finish_browser_auth, key, None, str(error))
            else:
                self.call_from_thread(self._finish_browser_auth, key, session, None)

        self._browser_auth_thread = Thread(target=capture, daemon=True)
        self._browser_auth_thread.start()

    def _finish_browser_auth(
        self,
        key: str,
        session: str | None,
        error: str | None,
    ) -> None:
        self._browser_auth_thread = None
        if error is not None or session is None:
            self.query_one("#settings-detail", Static).update(
                self._shell.translator(
                    "settings.detail.connect_error",
                    error=error or "no browser session returned",
                )
            )
            self.query_one("#settings-apply", Button).disabled = False
            return
        self._settings_secrets[key] = session
        self._save_settings()
        self.query_one("#settings-detail", Static).update(
            self._shell.translator("settings.detail.connected")
        )

    def _clear_setting(self) -> None:
        key = self._settings_selected_key
        if key is None:
            return
        field = self._shell._settings_registry.field(key)
        if not field.secret or not field.editable:
            return
        self._settings_secrets.pop(key, None)
        self.query_one("#settings-save", Button).disabled = self._shell._config_loader is None
        self._show_settings_level()

    def _save_settings(self) -> None:
        loader = self._shell._config_loader
        if loader is None:
            return
        try:
            loaded = loader.save(self._settings_values, self._settings_secrets)
        except ConfigValidationError as error:
            self.query_one("#settings-detail", Static).update(
                self._shell.translator("settings.detail.save_error", error=error)
            )
            return
        self._settings_values = dict(loaded.values)
        self._settings_secrets = dict(loaded.secrets)
        self._shell.set_language(str(self._settings_values.get("interface.language", "en-US")))
        self.query_one("#settings-save", Button).disabled = True
        self._apply_translations()
        self._show_settings_level()
        self.query_one("#settings-detail", Static).update(
            self._shell.translator("settings.detail.saved")
        )

    def _settings_back(self) -> None:
        if self._settings_path:
            self._settings_path = self._settings_path[:-1]
            self._show_settings_level()

    def _disable_setting_editor(self) -> None:
        self._settings_selected_key = None
        editor = self.query_one("#settings-editor", Input)
        editor.value = ""
        editor.password = False
        editor.disabled = True
        editor.placeholder = self._shell.translator("common.select_setting")
        editor.display = True
        select = self.query_one("#settings-select", Select)
        select.set_options(())
        select.disabled = True
        select.display = False
        apply = self.query_one("#settings-apply", Button)
        apply.label = self._shell.translator("common.apply")
        apply.disabled = True
        clear = self.query_one("#settings-clear", Button)
        clear.display = True
        clear.disabled = True

    def _setting_display_value(self, field: ConfigField) -> str:
        if field.type == "browser_auth":
            session = self._settings_secrets.get(field.key)
            if session is None:
                return "Not connected"
            return "Connected" if self._browser_auth_is_connected(field.key) else "Expired"
        if field.secret:
            return "<set>" if field.key in self._settings_secrets else "<not set>"
        if field.key == "interface.language":
            return self._setting_value_label(field, self._settings_values.get(field.key))
        return _setting_text(self._settings_values.get(field.key))

    def _setting_label(self, field: ConfigField) -> str:
        if field.key == "interface.language":
            return self._shell.translator("settings.language")
        return _setting_label(field.key.rsplit(".", 1)[-1], self._shell.translator)

    def _setting_value_label(self, field: ConfigField, value: object) -> str:
        if field.key == "interface.language":
            return self._shell.translator(f"settings.language.{value}")
        return _setting_text(value)

    def _browser_auth_is_connected(self, key: str) -> bool:
        session = self._settings_secrets.get(key)
        if session is None:
            return False
        auth = self._shell._settings_registry.browser_auth(key)
        plugin_id = key.split(".", 2)[1]
        return browser_session_has_cookies(session, auth.required_cookies) and (
            self._shell._browser_auth_validator is None
            or self._shell._browser_auth_validator(plugin_id, session)
        )

    def _run_background(
        self,
        command_line: str,
        argv: Sequence[str],
        selected_items: Sequence[ListItemData],
        block: ShellCommandBlock,
        cancellation: Event,
        job_id: int,
        concurrent: bool,
        operation_submission: bool,
        operation_epoch: int | None,
        operation_source_url: str | None,
        operation_title: str | None,
    ) -> None:
        output = StringIO()

        def progress(reason: str, payload: Mapping[str, object]) -> None:
            self.call_from_thread(
                self._show_progress,
                block,
                reason,
                dict(payload),
            )

        try:
            if selected_items:
                exit_code = self._shell.run_batch_selection(
                    selected_items,
                    operation_source_url,
                    operation_title,
                    output,
                    cancellation,
                    progress,
                )
            else:
                exit_code = self._shell.run_background(
                    argv,
                    output,
                    cancellation,
                    progress,
                )
        except Exception as error:
            exit_code = 1
            output.write(f"{type(error).__name__}: {error}\n")
            command = argv[0] if argv else "batch-selection"
            crash_log = write_crash_log(error, context=f"shell background command: {command}")
            if crash_log is not None:
                output.write(f"Crash log: {crash_log}\n")
        self.call_from_thread(
            self._finish_background,
            job_id,
            concurrent,
            block,
            command_line,
            exit_code,
            output.getvalue(),
            operation_submission,
            operation_epoch,
            operation_source_url,
        )

    def _merge_visible_selection(
        self,
        session: ViewSession,
        *,
        single_selection: bool = False,
    ) -> None:
        data = self._operation_result.data if self._operation_result else None
        plugin_id = self._operation_plugin_id
        if not isinstance(data, ListPageData) or plugin_id is None:
            return
        if single_selection and session.selected_item_ids:
            self._selected_items.clear()
        selected = set(session.selected_item_ids)
        for item in data.items:
            key = (plugin_id, item.source_url)
            if item.item_id in selected:
                self._selected_items[key] = item
            else:
                self._selected_items.pop(key, None)
        self._update_selection_status()

    def _update_selection_status(self) -> None:
        count = len(self._selected_items)
        self.query_one("#operation-view-status", Static).update(
            self._shell.translator("operation.selected", count=count)
        )

    def _enable_selection_buttons(self, enabled: bool) -> None:
        for btn_id in ("select-page", "select-all-pages", "deselect-all"):
            self.query_one(f"#{btn_id}", Button).disabled = not enabled

    def _select_page(self) -> None:
        """Select all items on the current page."""
        data = self._operation_result.data if self._operation_result else None
        plugin_id = self._operation_plugin_id
        if not isinstance(data, ListPageData) or plugin_id is None:
            return
        for item in data.items:
            self._selected_items[(plugin_id, item.source_url)] = item
        try:
            host = self.query_one("#operation-host", Vertical)
            for selection_list in host.query(AsciiSelectionList):
                selection_list.select_all()
        except Exception:
            pass
        self._update_selection_status()

    def _deselect_all(self) -> None:
        """Clear all selections across all pages."""
        self._selected_items.clear()
        try:
            host = self.query_one("#operation-host", Vertical)
            for selection_list in host.query(AsciiSelectionList):
                selection_list.deselect_all()
        except Exception:
            pass
        self._update_selection_status()

    def _select_all_pages(self) -> None:
        """Select all items across all pages by loading each page."""
        data = self._operation_result.data if self._operation_result else None
        source_url = self._operation_source_url
        runner = self._shell._operation_runner
        if not isinstance(data, ListPageData) or source_url is None or runner is None:
            return
        page_info = data.page_info
        total_pages = page_info.total_pages
        if total_pages is None or total_pages <= 1:
            self._select_page()
            return
        self._enable_selection_buttons(False)
        self.query_one("#operation-view-status", Static).update(
            self._shell.translator(
                "operation.loading_pages_start", total=total_pages
            )
        )
        thread = Thread(
            target=self._run_select_all_pages,
            args=(source_url, total_pages, page_info.page_size),
            daemon=True,
        )
        thread.start()

    def _run_select_all_pages(
        self,
        source_url: str,
        total_pages: int,
        page_size: int,
    ) -> None:
        runner = self._shell._operation_runner
        if runner is None:
            return
        collected: list[tuple[str, ListItemData]] = []
        try:
            for page_num in range(1, total_pages + 1):
                self.call_from_thread(
                    self.query_one("#operation-view-status", Static).update,
                    self._shell.translator(
                        "operation.loading_pages",
                        current=page_num,
                        total=total_pages,
                    ),
                )
                plugin_id, result = runner(source_url, {"page": page_num, "page_size": page_size})
                if result is not None and isinstance(result.data, ListPageData):
                    for item in result.data.items:
                        collected.append((plugin_id, item))
        except Exception as error:
            self.call_from_thread(self._finish_select_all_pages, collected, str(error))
            return
        self.call_from_thread(self._finish_select_all_pages, collected, None)

    def _finish_select_all_pages(
        self,
        items: list[tuple[str, ListItemData]],
        error: str | None,
    ) -> None:
        for plugin_id, item in items:
            self._selected_items[(plugin_id, item.source_url)] = item
        try:
            host = self.query_one("#operation-host", Vertical)
            for selection_list in host.query(AsciiSelectionList):
                selection_list.select_all()
        except Exception:
            pass
        self._enable_selection_buttons(True)
        if error:
            count = len(self._selected_items)
            self.query_one("#operation-view-status", Static).update(
                self._shell.translator(
                    "operation.selection_error", count=count, error=error
                )
            )
        else:
            self._update_selection_status()

    def _show_progress(
        self,
        block: ShellCommandBlock,
        reason: str,
        payload: Mapping[str, object],
    ) -> None:
        if self._task_service is None and self._shell._task_service_loader is not None:
            self._task_service = self._shell._task_service_loader()
        if self._task_service is not None:
            self._task_service.handle_progress_event(reason, payload)
        detail = _format_progress(reason, payload)
        block.detail = detail
        if self.query_one("#shell-tabs", TabbedContent).active == "console":
            self._render_console()
        self.query_one("#operation-status", Static).update(detail)

    async def _finish_background(
        self,
        job_id: int,
        concurrent: bool,
        block: ShellCommandBlock,
        command_line: str,
        exit_code: int,
        output: str,
        operation_submission: bool,
        operation_epoch: int | None,
        operation_source_url: str | None,
    ) -> None:
        status = "cancelled" if exit_code == 130 else "completed" if exit_code == 0 else "failed"
        self._finish_command_block(block, status, output)
        self.query_one("#operation-status", Static).update(f"{block.detail}: {command_line}")
        if concurrent:
            self._background_jobs.pop(job_id, None)
        else:
            self._operation_thread = None
            self._operation_cancel = None
        if operation_submission and exit_code != 0:
            # Keep a failed or cancelled selection actionable.  Successful
            # submissions reset the Operation view below; failures must return
            # to it so the user can retry or cancel without losing selection.
            self.query_one("#shell-tabs", TabbedContent).active = "operation"
        self.query_one("#cancel-operation", Button).disabled = not (
            self._background_jobs or self._operation_thread
        )
        if self._running_block is block:
            self._running_block = None
        self._refresh_tasks()
        if self.query_one("#shell-tabs", TabbedContent).active == "console":
            self.query_one("#shell-input", Input).focus()
        if (
            operation_submission
            and exit_code == 0
            and operation_epoch == self._operation_epoch
            and operation_source_url == self._operation_source_url
        ):
            await self._reset_operation_view()

    async def _reset_operation_view(self) -> None:
        host = self.query_one("#operation-host", Vertical)
        await host.remove_children()
        self._operation_source_url = None
        self._operation_plugin_id = None
        self._operation_result = None
        self._operation_session = None
        self._operation_block = None
        self._selected_items.clear()
        self._operation_thread = None
        self._operation_cancel = None
        self._enable_selection_buttons(False)
        self.query_one("#cancel-operation-view", Button).disabled = True
        self.query_one("#operation-view-status", Static).update(
            self._shell.translator("operation.no_active")
        )

    def _cancel_operation(self) -> None:
        if self._operation_cancel is not None:
            self._operation_cancel.set()
            if self._running_block is not None:
                self._running_block.detail = "Cancellation requested"
                self._render_console()
            self.query_one("#operation-status", Static).update("Cancellation requested")
            self.query_one("#cancel-operation", Button).disabled = True
            self.query_one("#cancel-operation-view", Button).disabled = True
            return
        if self._background_jobs:
            _job_id, (_thread, cancellation, block, _task_id, _concurrent) = next(
                reversed(self._background_jobs.items())
            )
            cancellation.set()
            block.detail = "Cancellation requested"
            self._render_console()
            self.query_one("#operation-status", Static).update("Cancellation requested")

    def _refresh_tasks(self) -> None:
        if self._task_service is None and self._shell._task_service_loader is not None:
            self._task_service = self._shell._task_service_loader()
        service = self._task_service
        if service is None:
            self._show_task_nodes((), level="roots")
            return
        # Test doubles and third-party integrations may still expose only the
        # original synchronous list API.  The production hierarchy service
        # advertises read-model methods and is refreshed off the Textual loop.
        if not callable(getattr(service, "list_root_summaries", None)):
            self._refresh_tasks_sync(service)
            return
        if self._task_level == "files" and self._active_task_id is not None:
            # Files are selected-task detail, not the large Batch root/page
            # projection.  Keep their existing targeted refresh semantics.
            self._open_task(self._active_task_id)
            return
        self._task_refresh_generation += 1
        self._task_refresh_pending = True
        if self._task_refresh_thread is not None and self._task_refresh_thread.is_alive():
            return
        generation = self._task_refresh_generation
        navigation = self._task_navigation_token()
        self._task_refresh_pending = False
        thread = Thread(
            target=self._load_task_projection,
            args=(service, generation, navigation),
            daemon=True,
        )
        self._task_refresh_thread = thread
        thread.start()

    def _refresh_tasks_sync(self, service: object) -> None:
        try:
            if self._task_level == "items" and self._active_batch_id is not None:
                nodes = tuple(service.list_children(self._active_batch_id))
                start = self._task_page_offset
                self._show_task_nodes(
                    nodes[start : start + self._task_page_size],
                    level="items",
                    page_total=len(nodes),
                    page_offset=start,
                )
            elif self._task_level == "files" and self._active_task_id is not None:
                self._open_task(self._active_task_id)
            else:
                self._show_task_nodes(
                    tuple(reversed(service.list_roots())),
                    level="roots",
                )
        except TaskNotFoundError:
            # Auto-delete may remove a completed Task while its detail view is
            # still open.  Drop the stale navigation selection and return to
            # the live root projection instead of crashing the Textual app.
            self._active_task_id = None
            self._active_batch_id = None
            self._active_batch_name = None
            self._task_files.clear()
            self._task_page_offset = 0
            self._show_task_nodes(
                tuple(reversed(service.list_roots())),
                level="roots",
            )

    def _task_navigation_token(self) -> tuple[object, ...]:
        try:
            active_tab = self.query_one("#shell-tabs", TabbedContent).active
        except Exception:
            active_tab = "tasks"
        return (
            active_tab,
            self._task_level,
            self._active_batch_id,
            self._active_task_id,
            self._task_page_offset,
        )

    def _load_task_projection(
        self,
        service: TaskHierarchyService,
        generation: int,
        navigation: tuple[object, ...],
    ) -> None:
        try:
            level = navigation[1]
            active_batch_id = navigation[2]
            if level == "items" and isinstance(active_batch_id, str):
                page = service.list_child_summaries(
                    active_batch_id,
                    offset=int(navigation[4]),
                    limit=self._task_page_size,
                )
                nodes = tuple(item.to_task_node() for item in page.items)
                total = page.total_count
                offset = page.offset
            else:
                summaries = service.list_root_summaries()
                nodes = tuple(
                    item.to_task_node()
                    if hasattr(item, "to_task_node")
                    else item
                    for item in reversed(summaries)
                )
                total = None
                offset = 0
            try:
                self.call_from_thread(
                    self._apply_task_projection,
                    generation,
                    navigation,
                    nodes,
                    total,
                    offset,
                    None,
                )
            except Exception:
                # The Shell may have closed while the worker was reading a
                # snapshot. Persisted state remains authoritative on restart.
                return
        except Exception as error:
            try:
                self.call_from_thread(
                    self._apply_task_projection,
                    generation,
                    navigation,
                    (),
                    None,
                    0,
                    error,
                )
            except Exception:
                return

    def _apply_task_projection(
        self,
        generation: int,
        navigation: tuple[object, ...],
        nodes: Sequence[TaskNode],
        page_total: int | None,
        page_offset: int,
        error: Exception | None,
    ) -> None:
        self._task_refresh_thread = None
        current_navigation = self._task_navigation_token()
        if generation != self._task_refresh_generation or navigation != current_navigation:
            self._task_refresh_pending = False
            if current_navigation[0] == "tasks":
                self._refresh_tasks()
            return
        if error is not None:
            if isinstance(error, TaskNotFoundError):
                self._active_task_id = None
                self._active_batch_id = None
                self._active_batch_name = None
                self._task_page_offset = 0
                self._show_task_nodes((), level="roots")
            else:
                self.query_one("#task-detail", Static).update(f"Could not refresh tasks: {error}")
        else:
            self._show_task_nodes(
                nodes,
                level=str(navigation[1]),
                page_total=page_total,
                page_offset=page_offset,
            )
        self._task_refresh_pending = False

    def _task_previous_page(self) -> None:
        if self._task_level != "items" or self._task_page_offset <= 0:
            return
        self._task_page_offset = max(0, self._task_page_offset - self._task_page_size)
        self._task_focus_pending = True
        self._refresh_tasks()

    def _task_next_page(self) -> None:
        if self._task_level != "items":
            return
        if self._task_page_offset + len(self._task_nodes) >= self._task_page_total:
            return
        self._task_page_offset += self._task_page_size
        self._task_focus_pending = True
        self._refresh_tasks()

    def _update_task_page_controls(self) -> None:
        status = self.query_one("#task-page-status", Static)
        previous = self.query_one("#task-page-prev", Button)
        next_button = self.query_one("#task-page-next", Button)
        if self._task_level != "items":
            status.update("")
            previous.display = False
            next_button.display = False
            previous.disabled = True
            next_button.disabled = True
            return
        total = self._task_page_total
        start = self._task_page_offset + 1 if total else 0
        end = min(self._task_page_offset + len(self._task_nodes), total)
        status.update(f"{start}-{end} / {total}")
        previous.display = True
        next_button.display = True
        previous.disabled = self._task_page_offset <= 0
        next_button.disabled = end >= total

    def _refresh_tasks_if_active(self) -> None:
        if self.query_one("#shell-tabs", TabbedContent).active == "tasks" and (
            self._background_jobs
            or any(node.state in _AUTO_REFRESH_TASK_STATES for node in self._task_nodes.values())
        ):
            self._refresh_tasks()

    def _resize_task_columns(self) -> None:
        if not hasattr(self, "_task_column_keys"):
            return
        table = self.query_one("#task-table", DataTable)

        viewport_width = table.scrollable_content_region.width
        if viewport_width <= 0:
            # The Tasks pane is mounted lazily. Recalculate from its actual
            # viewport after activation; this fallback only covers first mount.
            viewport_width = self.size.width - 2
        if viewport_width <= 0:
            return

        row_label_width = getattr(table, "_row_label_column_width", 0)
        padding = 2 * table.cell_padding * len(self._task_column_keys)
        available = max(0, viewport_width - row_label_width - padding)

        fixed_widths: dict[str, int] = {}
        for name, key in self._task_column_keys.items():
            if name == "name":
                continue

            column = table.columns[key]
            label_len = (
                len(column.label.plain)
                if hasattr(column.label, "plain")
                else len(str(column.label))
            )
            fixed_widths[name] = max(column.content_width, label_len)
            column.auto_width = False
            column.width = fixed_widths[name]

        name_col = table.columns[self._task_column_keys["name"]]
        name_col.auto_width = False
        name_col.width = max(8, available - sum(fixed_widths.values()))

        table.virtual_size = Size(
            row_label_width
            + sum(column.get_render_width(table) for column in table.columns.values()),
            table.virtual_size.height,
        )
        table.refresh(layout=True)

    def _resize_plugin_columns(self) -> None:
        if not hasattr(self, "_plugin_column_keys"):
            return
        table = self.query_one("#plugins-table", DataTable)

        # ``DataTable.size`` includes the scrollbar gutter, while its
        # scrollable content region is the actual row viewport.  Using the
        # application width here makes the table two cells too wide in the
        # common case and becomes even more inaccurate when a vertical
        # scrollbar is present.
        viewport_width = table.scrollable_content_region.width
        if viewport_width <= 0:
            # The Plugins pane is mounted lazily.  Keep the first, pre-layout
            # pass useful; the tab activation/resize handlers will recalculate
            # once Textual has assigned the table a real viewport.
            viewport_width = self.size.width - 2
        if viewport_width <= 0:
            return

        row_label_width = getattr(table, "_row_label_column_width", 0)
        padding = 2 * table.cell_padding * len(self._plugin_column_keys)
        available = max(0, viewport_width - row_label_width - padding)

        content = {}
        for name, key in self._plugin_column_keys.items():
            column = table.columns[key]
            label_len = (
                len(column.label.plain)
                if hasattr(column.label, "plain")
                else len(str(column.label))
            )
            content[name] = max(column.content_width, label_len)

        fixed_names = ("id", "name", "version", "source", "author", "status")
        fixed_cap = max(
            (len(str(table.columns[self._plugin_column_keys[name]].label)) for name in fixed_names),
            default=1,
        )
        fixed_cap = max(fixed_cap, available // 3)
        fixed_widths = {name: min(content[name], fixed_cap) for name in fixed_names}
        fixed_widths["author"] = min(fixed_widths["author"], 18)
        fixed_widths["status"] = max(
            fixed_widths["status"],
            min(_PLUGIN_STATUS_MIN_WIDTH, fixed_cap),
        )

        # Keep the fixed columns readable whenever there is room, then shrink
        # them deterministically at very narrow widths so the sum still fits
        # the viewport.  The description is the flexible column and may be
        # reduced to padding-only at the smallest supported terminal size.
        fixed_budget = available
        while sum(fixed_widths.values()) > fixed_budget:
            name = max(fixed_widths, key=fixed_widths.get)
            if fixed_widths[name] <= 0:
                break
            fixed_widths[name] -= 1

        fixed_share = 0
        for name in fixed_names:
            col = table.columns[self._plugin_column_keys[name]]
            col.auto_width = False
            width = fixed_widths[name]
            col.width = width
            fixed_share += width

        desc_col = table.columns[self._plugin_column_keys["description"]]
        desc_col.auto_width = False
        desc_col.width = max(0, available - fixed_share)

        table.virtual_size = Size(
            row_label_width
            + sum(column.get_render_width(table) for column in table.columns.values()),
            table.virtual_size.height,
        )
        table.refresh(layout=True)

    def _show_task_nodes(
        self,
        nodes: Sequence[TaskNode],
        *,
        level: str,
        page_total: int | None = None,
        page_offset: int = 0,
    ) -> None:
        previous_level = self._task_level
        self._task_level = level
        if previous_level != level:
            self._task_focus_pending = True
        self._task_nodes = {node.task_id: node for node in nodes}
        self._task_files.clear()
        self._task_page_offset = page_offset if level == "items" else 0
        self._task_page_total = (
            max(page_total or 0, len(nodes)) if level == "items" else 0
        )
        table = self.query_one("#task-table", DataTable)

        existing_keys = tuple(str(key.value) for key in table.rows)
        desired_keys = tuple(node.task_id for node in nodes)
        cursor_key = (
            existing_keys[table.cursor_row]
            if 0 <= table.cursor_row < len(existing_keys)
            else None
        )
        if existing_keys != desired_keys:
            table.clear()
            for index, node in enumerate(nodes, start=1):
                table.add_row(
                    _number_cell(index),
                    _task_kind_label(node.kind, self._shell.translator),
                    Text(node.title, overflow="ellipsis", no_wrap=True),
                    _display_name(node.site_id or "unknown"),
                    _task_progress(node, self._shell.translator),
                    _display_status(node.state.value, self._shell.translator),
                    _short_datetime(node.details.get("created_at")),
                    key=node.task_id,
                )
            restore_key = self._active_task_id or cursor_key
            if restore_key in desired_keys:
                table.move_cursor(row=desired_keys.index(restore_key))
        else:
            for index, node in enumerate(nodes, start=1):
                values = {
                    "number": _number_cell(index),
                    "type": _task_kind_label(node.kind, self._shell.translator),
                    "name": Text(node.title, overflow="ellipsis", no_wrap=True),
                    "site": _display_name(node.site_id or "unknown"),
                    "progress": _task_progress(node, self._shell.translator),
                    "status": _display_status(node.state.value, self._shell.translator),
                    "created": _short_datetime(node.details.get("created_at")),
                }
                for column, value in values.items():
                    try:
                        if table.get_cell(node.task_id, column) != value:
                            table.update_cell(node.task_id, column, value)
                    except Exception:
                        pass

        table.display = True
        self.query_one("#task-file-table", DataTable).display = False
        layout_signature = (
            self.size.width,
            level,
            tuple(
                (
                    node.kind.value,
                    len(node.site_id or ""),
                    len(node.state.value),
                    len(_short_datetime(node.details.get("created_at"))),
                )
                for node in nodes
            ),
        )
        if layout_signature != self._task_layout_signature:
            self._task_layout_signature = layout_signature
            self._resize_task_columns()
        if level == "roots":
            self._active_batch_id = None
            self._active_batch_name = None
            self.query_one("#task-path", Static).update(
                self._shell.translator("task.path")
            )
            empty_text = self._shell.translator("task.no_tasks")
        else:
            self.query_one("#task-path", Static).update(
                f"{self._shell.translator('task.path')} / "
                f"{self._active_batch_name or self._shell.translator('task.batch')}"
            )
            empty_text = self._shell.translator("task.empty_batch")
        self._update_task_page_controls()
        back = self.query_one("#task-back", Button)
        back.disabled = level == "roots"
        back.display = level != "roots"
        if not nodes:
            detail_text = empty_text
        elif self._active_task_id is None:
            detail_text = self._shell.translator("task.choose_detail")
        else:
            detail_text = None
        detail_signature = (None, detail_text) if detail_text is not None else None
        if detail_signature is not None and detail_signature != self._task_detail_signature:
            self._task_detail_signature = detail_signature
            self.query_one("#task-detail", Static).update(detail_text)
        selected = self._task_nodes.get(self._active_task_id or "")
        if selected is not None:
            self._select_task(selected.task_id)
        else:
            self._active_task_id = None
            self._set_task_actions()
        if (
            nodes
            and self._task_focus_pending
            and self.query_one("#shell-tabs", TabbedContent).active == "tasks"
        ):
            table.focus()
            self._task_focus_pending = False

    def _select_task(self, task_id: str) -> None:
        node = self._task_nodes.get(task_id)
        if node is None:
            return
        self._active_task_id = task_id
        lines = [
            f"[N] {self._shell.translator('task.detail.name'):<10} {node.title}",
            f"[S] {self._shell.translator('task.detail.status'):<10} "
            f"{_display_status(node.state.value, self._shell.translator)}",
            f"[#] {self._shell.translator('task.detail.progress'):<10} "
            f"{_task_progress(node, self._shell.translator)}",
            f"[W] {self._shell.translator('task.detail.website'):<10} "
            f"{_display_name(node.site_id or 'unknown')}",
        ]
        queue_reason = node.details.get("scheduler_waiting_reason")
        if isinstance(queue_reason, str):
            lines.append(
                f"[Q] {self._shell.translator('task.detail.queue'):<10} "
                f"{_scheduler_waiting_label(queue_reason, self._shell.translator)}"
                + (
                    f" #{node.details['scheduler_queue_position']}"
                    if isinstance(node.details.get("scheduler_queue_position"), int)
                    else ""
                )
            )
        failure = node.details.get("failure")
        if isinstance(failure, Mapping):
            lines.extend(_task_failure_lines(failure, self._shell.translator))
        rendered = "\n".join(lines)
        detail_signature = (task_id, rendered)
        if detail_signature != self._task_detail_signature:
            self._task_detail_signature = detail_signature
            self.query_one("#task-detail", Static).update(rendered)
        self._set_task_actions(node)

    def _set_task_actions(self, node: TaskNode | None = None) -> None:
        self._task_file_path = None
        self._task_source_url = (
            node.source_url
            if node is not None and self._task_level != "files"
            else None
        )
        self._task_folder = _folder_for_task(node)
        folder_resolver = getattr(self._task_service, "get_available_folder", None)
        if node is not None and self._task_folder is None and callable(folder_resolver):
            try:
                self._task_folder = folder_resolver(node.task_id)
            except Exception:
                # Folder discovery is a contextual convenience. A stale Task
                # or transient read failure must not break row selection.
                self._task_folder = None
        task_id = node.task_id if node is not None else None
        if self._pending_task_delete != task_id:
            self._pending_task_delete = None
        state = node.state if node is not None else None
        action_signature = (
            task_id,
            state,
            self._task_level,
            self._task_source_url,
            self._task_folder,
            self._task_file_path,
            self._pending_task_delete,
        )
        if action_signature == self._task_action_signature:
            return
        self._task_action_signature = action_signature
        self._update_task_detail_action_buttons(node is not None)
        allow_lifecycle_control = self._task_level == "roots"

        pause = self.query_one("#task-pause", Button)
        pause.disabled = state not in {
            TaskState.PREPARING,
            TaskState.QUEUED,
            TaskState.RUNNING,
        }
        pause.display = allow_lifecycle_control and not pause.disabled
        resume = self.query_one("#task-resume", Button)
        resume.disabled = state is not TaskState.PAUSED
        resume.display = allow_lifecycle_control and not resume.disabled
        cancel = self.query_one("#task-cancel", Button)
        cancel.disabled = state not in {
            TaskState.PREPARING,
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.PAUSED,
        }
        cancel.display = allow_lifecycle_control and not cancel.disabled
        retry = self.query_one("#task-retry", Button)
        retry.disabled = state not in {
            TaskState.FAILED,
            TaskState.PARTIALLY_COMPLETED,
        }
        retry.display = allow_lifecycle_control and not retry.disabled
        delete = self.query_one("#task-delete", Button)
        delete.disabled = state not in {
            TaskState.COMPLETED,
            TaskState.PARTIALLY_COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.SKIPPED,
        }
        delete.display = allow_lifecycle_control and not delete.disabled
        delete.label = "OK" if self._pending_task_delete == task_id else "×"
        self.query_one("#refresh-tasks", Button).display = allow_lifecycle_control

    def _update_task_detail_action_buttons(self, visible: bool) -> None:
        source = self.query_one("#task-source", Button)
        if self._task_level == "files":
            source.label = self._shell.translator("task.action.open_file")
            source.tooltip = self._shell.translator("tooltip.open_file")
            source.disabled = self._task_file_path is None
        else:
            source.label = self._shell.translator("task.action.open_url")
            source.tooltip = self._shell.translator("tooltip.open_source")
            source.disabled = self._task_source_url is None
        source.display = visible
        folder = self.query_one("#task-folder", Button)
        folder.disabled = self._task_folder is None
        folder.display = visible
        self.query_one("#task-detail-actions", Vertical).display = visible

    def _open_task_source(self) -> None:
        if self._task_level == "files" and self._task_file_path is not None:
            self._open_external(self._shell._file_opener, self._task_file_path, "file")
            return
        if self._task_source_url is not None:
            self._open_external(self._shell._url_opener, self._task_source_url, "source")

    def _open_task_folder(self) -> None:
        if self._task_folder is not None:
            self._open_external(self._shell._folder_opener, self._task_folder, "folder")

    def _open_external(
        self,
        opener: ExternalOpener,
        value: str,
        label: str,
    ) -> None:
        try:
            opener(value)
        except OSError as error:
            self.query_one("#task-detail", Static).update(f"Could not open {label}: {error}")

    def _control_task(self, action: str) -> None:
        service = self._task_service
        task_id = self._active_task_id
        if service is None or task_id is None:
            return
        node = service.get(task_id) if action == "resume" else None

        if (
            action == "resume"
            and node is not None
            and node.kind is TaskKind.BATCH
            and not self._is_task_running(task_id)
        ):
            self._resume_task()
            return

        try:
            getattr(service, action)(task_id)
        except Exception as error:
            self.query_one("#task-detail", Static).update(f"Could not {action} this task: {error}")
            return
        if node is not None and node.kind is TaskKind.ITEM and not self._is_task_running(task_id):
            self._start_persisted_task(node)
        self._refresh_tasks()

    def _resume_task(self) -> None:
        service = self._task_service
        task_id = self._active_task_id
        if service is None or task_id is None:
            return
        self.query_one("#task-detail", Static).update("Resume started...")
        self.query_one("#task-resume", Button).disabled = True
        Thread(target=self._run_task_resume, args=(service, task_id), daemon=True).start()

    def _run_task_resume(self, service: TaskHierarchyService, task_id: str) -> None:
        try:
            service.resume(task_id)
        except Exception as error:
            self.call_from_thread(self._finish_task_retry, f"Resume failed: {error}")
            return

        # An active Batch keeps its current Coordinator handle. Starting a
        # second Pipeline here would race that handle and duplicate recovery.
        if service.is_batch_running(task_id):
            self.call_from_thread(self._finish_task_retry, "Resume requested.")
            return

        # A stopped Batch must recover persisted checkpoints through the CLI,
        # which claims a new Coordinator run generation.
        command = f"batches resume {task_id}"
        self.call_from_thread(
            self._start_background,
            command,
            ("batches", "resume", task_id),
        )

    def _retry_task(self) -> None:
        service = self._task_service
        task_id = self._active_task_id
        if service is None or task_id is None:
            return
        self.query_one("#task-detail", Static).update("Retry started...")
        self.query_one("#task-retry", Button).disabled = True
        Thread(target=self._run_task_retry, args=(service, task_id), daemon=True).start()

    def _run_task_retry(self, service: TaskHierarchyService, task_id: str) -> None:
        node = service.get(task_id)
        try:
            if node.kind is not TaskKind.BATCH:
                asyncio.run(service.retry(task_id))
        except Exception as error:
            self.call_from_thread(self._finish_task_retry, f"Retry failed: {error}")
        else:
            self.call_from_thread(self._finish_task_retry, "Retry completed.", node)

    def _finish_task_retry(self, message: str, node: TaskNode | None = None) -> None:
        if node is not None:
            self._start_persisted_task(node)
        self._refresh_tasks()
        self.query_one("#task-detail", Static).update(message)

    def _is_task_running(self, task_id: str) -> bool:
        return any(
            job_task_id == task_id
            for _thread, _cancel, _block, job_task_id, _concurrent in self._background_jobs.values()
        )

    def _start_persisted_task(self, node: TaskNode) -> None:
        if self._is_task_running(node.task_id):
            return
        if node.kind is TaskKind.ITEM:
            if node.source_url is None:
                return
            argv = ("tasks", "resume", node.task_id, "--json")
        elif node.kind is TaskKind.BATCH:
            argv = ("batches", "retry", node.task_id)
        else:
            return
        self._start_background(shlex.join(argv), argv)

    def _delete_task_record(self) -> None:
        service = self._task_service
        task_id = self._active_task_id
        if service is None or task_id is None:
            return
        button = self.query_one("#task-delete", Button)
        if self._pending_task_delete != task_id:
            self._pending_task_delete = task_id
            button.label = "OK"
            button.focus()
            self.query_one("#task-detail", Static).update(
                "Delete this record? Downloaded files will be kept. Press Confirm again."
            )
            return
        try:
            service.delete(task_id)
        except Exception as error:
            self.query_one("#task-detail", Static).update(f"Could not delete record: {error}")
            return
        self._pending_task_delete = None
        if self._task_level == "files":
            self._task_back()
        else:
            self._refresh_tasks()

    def _open_task(self, task_id: str) -> None:
        node = self._task_nodes.get(task_id)
        service = self._task_service
        if node is None or service is None:
            return
        self._active_task_id = task_id
        if node.kind is TaskKind.BATCH:
            self._active_batch_id = task_id
            self._active_batch_name = node.title
            self._task_page_offset = 0
            self._task_focus_pending = True
            if callable(getattr(service, "list_child_summaries", None)):
                self._task_level = "items"
                self._active_task_id = None
                self._refresh_tasks()
            else:
                self._show_task_nodes(service.list_children(task_id), level="items")
            return
        detail_node = node
        get_detail = getattr(service, "get", None)
        if callable(get_detail):
            try:
                detail_node = get_detail(task_id)
            except TaskNotFoundError:
                self._refresh_tasks()
                return
        files = service.list_files(task_id)
        self._task_files = {file.file_id: file for file in files}
        table = self.query_one("#task-file-table", DataTable)

        existing_keys = tuple(str(key.value) for key in table.rows)
        desired_keys = tuple(file.file_id for file in files)
        if existing_keys != desired_keys:
            table.clear()
            for index, file in enumerate(files, start=1):
                table.add_row(
                    _number_cell(index),
                    file.name,
                    _task_file_progress(file),
                    _display_status(file.state, self._shell.translator),
                    str(file.attempts),
                    file.location or "-",
                    key=file.file_id,
                )
        else:
            for index, file in enumerate(files, start=1):
                values = {
                    "index": _number_cell(index),
                    "name": file.name,
                    "progress": _task_file_progress(file),
                    "status": _display_status(file.state, self._shell.translator),
                    "attempts": str(file.attempts),
                    "location": file.location or "-",
                }
                for column, value in values.items():
                    try:
                        if table.get_cell(file.file_id, column) != value:
                            table.update_cell(file.file_id, column, value)
                    except Exception:
                        pass
        self._task_level = "files"
        self._update_task_page_controls()
        task_label = self._shell.translator("task.path")
        parent_path = (
            f"{task_label} / {self._active_batch_name} / "
            if detail_node.parent_id
            else f"{task_label} / "
        )
        self.query_one("#task-path", Static).update(f"{parent_path}{detail_node.title}")
        self.query_one("#task-table", DataTable).display = False
        table.display = True
        back = self.query_one("#task-back", Button)
        back.disabled = False
        back.display = True
        lines = [
            f"[S] {self._shell.translator('task.detail.status'):<10} "
            f"{_display_status(detail_node.state.value, self._shell.translator)}",
            f"[#] {self._shell.translator('task.detail.progress'):<10} "
            f"{_task_progress(detail_node, self._shell.translator)}",
            f"[F] {self._shell.translator('task.detail.files'):<10} {len(files)}",
        ]
        failure = detail_node.details.get("failure")
        if isinstance(failure, Mapping):
            lines.extend(_task_failure_lines(failure, self._shell.translator))
        self.query_one("#task-detail", Static).update("\n".join(lines))
        self._set_task_actions(detail_node)
        if files:
            table.focus()
        else:
            self.query_one("#task-back", Button).focus()

    def _show_task_file(self, file_id: str) -> None:
        file = self._task_files.get(file_id)
        if file is None:
            return
        lines = [
            f"[F] {self._shell.translator('table.file'):<10} {file.name}",
            f"[S] {self._shell.translator('task.detail.status'):<10} "
            f"{_display_status(file.state, self._shell.translator)}",
            f"[#] {self._shell.translator('task.detail.attempts'):<10} {file.attempts}",
            f"[L] {self._shell.translator('task.detail.location'):<10} {file.location or '-'}",
        ]
        if file.failure is not None:
            lines.extend(_task_failure_lines(file.failure, self._shell.translator))
        self.query_one("#task-detail", Static).update("\n".join(lines))
        self._task_folder = (
            str(Path(file.location).parent) if file.location is not None else None
        )
        self._task_file_path = file.location
        self._task_source_url = None
        self._task_action_signature = None
        self._update_task_detail_action_buttons(True)

    def _task_back(self) -> None:
        service = self._task_service
        if self._task_level == "files" and self._active_batch_id is not None and service:
            self._task_page_offset = 0
            self._task_focus_pending = True
            if callable(getattr(service, "list_child_summaries", None)):
                self._task_level = "items"
                self._active_task_id = None
                self._refresh_tasks()
            else:
                children = service.list_children(self._active_batch_id)
                self._show_task_nodes(
                    children[: self._task_page_size],
                    level="items",
                    page_total=len(children),
                    page_offset=0,
                )
        elif self._task_level in {"files", "items"} and service:
            return_task_id = (
                self._active_task_id
                if self._task_level == "files" and self._active_batch_id is None
                else None
            )
            self._active_batch_id = None
            self._active_batch_name = None
            self._task_page_offset = 0
            self._task_focus_pending = True
            if callable(getattr(service, "list_root_summaries", None)):
                self._task_level = "roots"
                self._active_task_id = return_task_id
                self._refresh_tasks()
            else:
                self._active_task_id = return_task_id
                self._show_task_nodes(tuple(reversed(service.list_roots())), level="roots")


def _format_shell_output(output: str) -> str:
    text = output.strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        rows = [line.split("\t") for line in text.splitlines()]
        if rows and len({len(row) for row in rows}) == 1 and len(rows[0]) > 1:
            return "Results\n" + _format_table(rows)
        return text
    if isinstance(value, dict):
        summary = _format_shell_summary(value)
        if summary is not None:
            return summary
    if isinstance(value, list) and all(
        isinstance(item, dict) and {"type", "name", "progress", "status"} <= item.keys()
        for item in value
    ):
        rows = [["Type", "Name", "Progress", "Status", "Created"]]
        rows.extend(
            [
                _format_scalar(item.get("type")),
                _truncate_cell(_format_scalar(item.get("name")), 36),
                _format_scalar(item.get("progress")),
                _display_status_text(item.get("status")),
                _short_datetime(item.get("created_at")),
            ]
            for item in value
        )
        return "Tasks\n" + _format_table(rows)
    title = "Result"
    return "\n".join((title, *_format_structured_value(value, 1)))


def _format_shell_summary(value: Mapping[str, object]) -> str | None:
    error = value.get("error")
    if error:
        return f"Error\n  x {error}"
    operation = value.get("operation_result")
    if not isinstance(operation, Mapping):
        operation = value
    if operation.get("result_kind") == "error":
        data = operation.get("data")
        if isinstance(data, Mapping):
            details = data.get("details")
            details = details if isinstance(details, Mapping) else {}
            lines = ["Error", f"  Reason: {_format_scalar(data.get('message'))}"]
            if details.get("stage"):
                lines.append(f"  Stage: {_display_status_text(details.get('stage'))}")
            lines.append(f"  Retry: {'Available' if data.get('retryable') else 'Not available'}")
            if details.get("next_retry_at"):
                lines.append(f"  Next retry: {details['next_retry_at']}")
            return "\n".join(lines)
    if "downloaded_files_preserved" in value and "matched" in value:
        lines = [
            "Task deletion preview" if value.get("dry_run") else "Task records deleted",
            f"  Matched: {_format_scalar(value.get('matched'))}",
            f"  Deleted: {_format_scalar(value.get('deleted'))}",
            "  Downloaded files: Preserved",
        ]
        blocked = value.get("blocked_active")
        if isinstance(blocked, list) and blocked:
            lines.append(f"  Active tasks skipped: {len(blocked)}")
        if value.get("error"):
            lines.append(f"  Action: {value['error']}")
        return "\n".join(lines)
    if {"task_id", "type", "name", "progress", "status"} <= value.keys():
        lines = [
            str(value.get("name")),
            f"  Type: {_display_status_text(value.get('type'))}",
            f"  Progress: {_format_scalar(value.get('progress'))}",
            f"  Status: {_display_status_text(value.get('status'))}",
        ]
        for label, key in (("Source", "source"), ("Folder", "output_folder")):
            if value.get(key):
                lines.append(f"  {label}: {value[key]}")
        lines.append(f"  Files: {_format_scalar(value.get('file_count'))}")
        failure = value.get("failure")
        if isinstance(failure, Mapping):
            lines.extend(_task_failure_lines(failure))
        return "\n".join(lines)
    if value.get("command") != "download":
        return None
    if value.get("cancelled") is True:
        return "Download cancelled\n  Saved files were kept"
    if value.get("dry_run") is True:
        if value.get("matched") is not True:
            return "URL is not supported"
        return "\n".join(
            (
                "URL supported",
                f"  Site: {_format_scalar(value.get('site_id'))}",
                f"  Type: {_format_scalar(value.get('input_kind'))}",
            )
        )
    output_files = value.get("output_files")
    if not isinstance(output_files, list) or "status" not in value:
        return None
    title = value.get("title")
    heading = "Batch complete" if "batch" in value else "Download complete"
    lines = [heading, f"  Status: {_display_status_text(value.get('status'))}"]
    if title:
        lines.append(f"  Title: {title}")
    lines.append(f"  Files: {len(output_files)}")
    folder = _common_output_folder(output_files)
    if folder:
        lines.append(f"  Folder: {folder}")
    batch = value.get("batch")
    if isinstance(batch, Mapping):
        counts = batch.get("counts")
        if isinstance(counts, Mapping):
            completed = counts.get("completed", 0)
            total = counts.get("total")
            if not isinstance(total, int):
                total = sum(item for item in counts.values() if isinstance(item, int))
            lines.insert(2, f"  Items: {completed}/{total}")
    return "\n".join(lines)


def _common_output_folder(output_files: Sequence[object]) -> str | None:
    paths = [str(path) for path in output_files if path]
    if not paths:
        return None
    return os.path.commonpath([str(Path(path).parent) for path in paths])


def _display_status_text(value: object) -> str:
    return str(value or "unknown").replace("_", " ").title()


def _format_structured_value(value: object, level: int) -> list[str]:
    indent = "  " * level
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            label = str(key).replace("_", " ").title()
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{indent}{label}")
                lines.extend(_format_structured_value(item, level + 1))
            else:
                lines.append(f"{indent}{label}: {_format_scalar(item)}")
        return lines
    if isinstance(value, list):
        if value and all(
            isinstance(row, dict)
            and all(not isinstance(item, (dict, list)) for item in row.values())
            for row in value
        ):
            keys = list(dict.fromkeys(key for row in value for key in row))
            rows = [[str(key).replace("_", " ").title() for key in keys]]
            rows.extend([[_format_scalar(row.get(key)) for key in keys] for row in value])
            return [f"{indent}{line}" for line in _format_table(rows).splitlines()]
        lines = []
        for index, item in enumerate(value, start=1):
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}{index}.")
                lines.extend(_format_structured_value(item, level + 1))
            else:
                lines.append(f"{indent}{index}. {_format_scalar(item)}")
        return lines or [f"{indent}-"]
    return [f"{indent}{_format_scalar(value)}"]


def _format_table(rows: Sequence[Sequence[str]]) -> str:
    if not rows:
        return ""
    return render_table_text(
        TableSpec(
            columns=tuple(TableColumn(label) for label in rows[0]),
            rows=tuple(tuple(row) for row in rows[1:]),
        )
    )


def _format_scalar(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value)


def _truncate_cell(value: str, width: int) -> str:
    from cli_downloader.cli.formatting import truncate_display

    return truncate_display(value, width)


def _short_datetime(value: object) -> str:
    if not isinstance(value, str):
        return "-"
    return value.replace("T", " ")[:16]


def _task_column_widths(total_width: int) -> dict[str, int]:
    fixed = {
        "number": 3,
        "type": 3,
        "site": 8,
        "progress": 11,
        "status": 14,
        "created": 16,
    }
    padding = 2 * (len(fixed) + 1)
    return {
        **fixed,
        "name": max(8, max(48, total_width) - sum(fixed.values()) - padding),
    }


def _format_progress(reason: str, payload: Mapping[str, object]) -> str:
    label = reason.replace("_", " ").title()
    if not payload:
        return label
    details = ", ".join(
        f"{key.replace('_', ' ')}: {_format_scalar(value)}" for key, value in payload.items()
    )
    return f"{label} | {details}"


def _normalize_shell_command(command_line: str, known_plugins: set[str] | None = None) -> str:
    command_line = command_line.strip()
    if not command_line:
        return ""
    try:
        argv = shlex.split(command_line)
    except (ValueError, UnicodeError):
        return command_line
    if not argv:
        return ""

    parsed = urlsplit(argv[0])
    if (
        len(argv) == 1
        and parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname is not None
    ):
        return shlex.join(("download", argv[0]))

    plugin_id = argv[0].lower()
    if known_plugins and plugin_id in known_plugins:
        # If the user typed `pixiv ...` we rewrite it as `operation pixiv ...`
        # But if they typed `operation pixiv ...`, it's already there
        return shlex.join(("operation", *argv))

    return command_line


def _is_allowed_shell_command(argv: Sequence[str]) -> bool:
    if not argv:
        return False

    command = argv[0]
    if command == "operation":
        return True
    if command == "sites":
        return len(argv) == 1
    if command == "tasks":
        return _is_tasks_command(argv)
    if command == "history":
        return True
    if command == "batches":
        return len(argv) >= 3 and argv[1] in {"show", "retry"}
    if command == "config":
        return len(argv) == 2 and argv[1] in {"show", "defaults"}
    if command == "download":
        return len(argv) >= 2
    return False


def _is_embedded_operation_command(argv: Sequence[str]) -> bool:
    if len(argv) == 2 and argv[0] == "download":
        return True
    if len(argv) >= 2 and argv[0] == "operation":
        return True
    return False


def _shell_task_id(argv: Sequence[str]) -> str | None:
    if len(argv) >= 3 and argv[0] == "tasks" and argv[1] in {
        "show",
        "retry",
        "resume",
        "hard-retry",
        "cancel",
    }:
        return argv[2]
    if len(argv) >= 3 and argv[0] == "batches" and argv[1] in {"show", "retry", "resume"}:
        return argv[2]
    if len(argv) >= 3 and argv[0] == "history" and argv[1] in {"show", "delete"}:
        return argv[2]
    return None


def _is_tasks_command(argv: Sequence[str]) -> bool:
    if len(argv) == 1:
        return True
    if argv[1] == "list":
        return True
    if argv[1] in {"show", "hard-retry", "cancel", "retry", "resume"}:
        return len(argv) >= 3
    if argv[1] == "delete":
        return len(argv) >= 3
    if argv[1].startswith("--"):
        return True
    return False


def _display_name(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()


def _setting_label(
    value: str,
    translator: TranslationService | None = None,
) -> str:
    translator = translator or TranslationService()
    key = f"settings.label.{value.replace('-', '_')}"
    translated = translator(key)
    return translated if translated != key else value.replace("_", " ").replace("-", " ").title()


def _setting_description(field: ConfigField, translator: TranslationService) -> str:
    value = field.key.rsplit(".", 1)[-1].replace("-", "_")
    key = f"settings.description.{value}"
    translated = translator(key)
    return translated if translated != key else field.description


def _plugin_field_group(key: str, plugin_id: str) -> str:
    relative = key.removeprefix(f"plugins.{plugin_id}.")
    return relative.split(".", 1)[0] if "." in relative else "general"


def _setting_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _parse_setting_value(value: str, field: ConfigField) -> object:
    text = value.strip()
    if field.type == "string":
        return value
    if field.type == "integer":
        return int(text)
    if field.type == "number":
        return float(text)
    if field.type == "boolean":
        if text.lower() in {"true", "yes", "1", "on"}:
            return True
        if text.lower() in {"false", "no", "0", "off"}:
            return False
        raise ValueError("use true or false")
    raise ValueError(f"unsupported setting type: {field.type}")


def _display_status(value: str, translator: TranslationService | None = None) -> str:
    translation_keys = {
        "pending": "status.waiting",
        "pending_resolution": "status.waiting",
        "resolving": "status.preparing",
        "running": "status.in_progress",
        "queued": "status.queued",
        "downloading": "status.downloading",
        "completed": "status.completed",
        "partially_completed": "status.partial_success",
        "failed": "status.failed",
        "resolution_failed": "status.failed",
        "package_rejected": "status.failed",
        "duplicate_skipped": "status.already_downloaded",
        "cancelled": "status.cancelled",
        "package_ready": "status.ready",
    }
    key = translation_keys.get(value)
    if translator is not None and key is not None:
        return translator(key)
    return {
        "pending": "Waiting",
        "pending_resolution": "Waiting",
        "resolving": "Preparing",
        "running": "In progress",
        "queued": "Queued",
        "downloading": "Downloading",
        "completed": "Completed",
        "partially_completed": "Partial success",
        "failed": "Failed",
        "resolution_failed": "Failed",
        "package_rejected": "Failed",
        "duplicate_skipped": "Already downloaded",
        "cancelled": "Cancelled",
        "package_ready": "Ready",
    }.get(value, _display_name(value))


def _scheduler_waiting_label(
    reason: str,
    translator: TranslationService | None = None,
) -> str:
    keys = {
        "global_slot": "task.queue.global_slot",
        "site_slot": "task.queue.site_slot",
        "root_order": "task.queue.root_order",
        "fair_turn": "task.queue.fair_turn",
    }
    labels = {
        "global_slot": "Waiting for global slot",
        "site_slot": "Waiting for site slot",
        "root_order": "Waiting for Batch order",
        "fair_turn": "Waiting for fair turn",
    }
    key = keys.get(reason)
    if translator is not None and key is not None:
        return translator(key)
    return labels.get(reason, _display_name(reason))


def _number_cell(index: int) -> Text:
    return Text(str(index), style="#777777")


def _plugin_cell(value: object) -> Text:
    return Text(str(value), overflow="ellipsis", no_wrap=True)


def _plugin_source_cell(source: str, translator: TranslationService | None = None) -> Text:
    style = {
        "official": "bold #67b279",
        "community": "bold #ffaa55",
        "custom": "bold #75aadb",
        "local": "bold #999999",
    }.get(source.lower(), "#e8e8e8")
    label = (
        translator(f"plugin.source.{source.lower()}")
        if translator is not None
        else _display_name(source)
    )
    return Text(label, style=style, overflow="ellipsis", no_wrap=True)


def _plugin_status_label(value: object, translator: TranslationService) -> str:
    status = str(value)
    keys = {
        "Installed": "plugin.status.installed",
        "Update Available": "plugin.status.update_available",
        "Restart Required": "plugin.status.restart_load",
        "Load Failed": "plugin.status.load_failed",
        "Invalid Catalog Entry": "plugin.status.invalid_catalog",
        "Incompatible": "plugin.status.incompatible",
        "Not Installed": "plugin.status.not_installed",
    }
    return translator(keys[status]) if status in keys else status


def _plugin_description(value: object, translator: TranslationService) -> str:
    description = str(value)
    if description == "Installed external plugin.":
        return translator("plugin.description.installed_external")
    if description == "Installed — restart shell to load.":
        return translator("plugin.description.restart_load")
    if description == "Plugin failed to load.":
        return translator("plugin.description.load_failed")
    if description == "Fetching remote information...":
        return translator("plugin.status.fetching")
    return description


def _task_kind_label(
    kind: TaskKind,
    translator: TranslationService | None = None,
) -> Text:
    translator = translator or TranslationService()
    if kind is TaskKind.BATCH:
        return Text(f"📦 {translator('task.kind.batch')}", style="#e58a32")
    return Text(f"⬇ {translator('task.kind.item')}", style="#67b279")


def _format_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    elif b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    elif b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    else:
        return f"{b / (1024 * 1024 * 1024):.1f} GB"


def _task_progress(
    node: TaskNode,
    translator: TranslationService | None = None,
) -> str:
    translator = translator or TranslationService()
    progress = node.progress
    if progress.total_files is None:
        return translator("task.progress.preparing")
    suffix = (
        f" + {progress.preparing_items} {translator('task.progress.preparing').lower()}"
        if progress.preparing_items
        else ""
    )

    file_part = (
        f"{progress.completed_files}/{progress.total_files} "
        f"{translator('task.progress.files')}{suffix}"
    )

    if progress.completed_bytes > 0:
        if progress.total_bytes is not None:
            completed_bytes, total_bytes = normalise_byte_progress(
                progress.completed_bytes,
                progress.total_bytes,
            )
            pct = byte_percentage(completed_bytes, total_bytes)
            byte_part = f"({_format_bytes(completed_bytes)} / {_format_bytes(total_bytes)} {pct}%)"
        else:
            byte_part = f"({_format_bytes(progress.completed_bytes)})"
        return f"{file_part} {byte_part}"

    return file_part


def _task_file_progress(file: TaskFileNode) -> str:
    if file.completed_bytes > 0:
        if file.total_bytes is not None:
            completed_bytes, total_bytes = normalise_byte_progress(
                file.completed_bytes,
                file.total_bytes,
            )
            pct = byte_percentage(completed_bytes, total_bytes)
            return f"{_format_bytes(completed_bytes)} / {_format_bytes(total_bytes)} ({pct}%)"
        else:
            return _format_bytes(file.completed_bytes)
    return "-"


def _task_failure_lines(
    failure: Mapping[str, object],
    translator: TranslationService | None = None,
) -> list[str]:
    translator = translator or TranslationService()
    owner = (
        translator("task.failure.owner.site")
        if failure.get("origin") == "plugin"
        else translator("task.failure.owner.downloader")
    )
    stage_value = str(failure.get("stage", "unknown"))
    stage = translator(f"task.failure.stage.{stage_value}")
    if stage == f"task.failure.stage.{stage_value}":
        stage = _display_name(stage_value).lower()
    return [
        f"[!] {translator('task.detail.problem'):<8} "
        f"{failure.get('message', translator('task.detail.problem'))}",
        f"[@] {translator('task.detail.origin'):<8} {owner}, {stage}",
        f"[R] {translator('task.detail.retry'):<8} "
        f"{translator('task.retry.available') if failure.get('retryable') else translator('task.retry.unavailable')}",
    ]


def _folder_for_task(node: TaskNode | None) -> str | None:
    path = next(iter(node.output_files), None) if node is not None else None
    return str(Path(path).parent) if path else None


def _open_url(url: str) -> None:
    if not webbrowser.open(url):
        raise OSError("the system did not accept the URL")


def _open_folder(folder: str) -> None:
    path = Path(folder).resolve()
    if not path.is_dir():
        raise OSError(f"folder does not exist: {path}")
    os.startfile(path)


def _open_file(file_path: str) -> None:
    path = Path(file_path).resolve()
    if not path.is_file():
        raise OSError(f"file does not exist: {path}")
    os.startfile(path)
