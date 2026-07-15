import asyncio
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Event
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch

from textual.widgets import (
    Button,
    DataTable,
    Input,
    RichLog,
    Select,
    SelectionList,
    Static,
    TabbedContent,
)

from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.application.release_update_service import (
    ReleaseUpdateResult,
    ReleaseUpdateStatus,
)
from cli_downloader import __version__
from cli_downloader.cli.shell import (
    InteractiveShell,
    TextualShellApp,
    _PLUGIN_STATUS_MIN_WIDTH,
    _format_shell_output,
    _plugin_status_label,
    _task_column_widths,
)
from cli_downloader.cli.i18n import TranslationService
from cli_downloader.cli.textual_renderer import CorePageView
from cli_downloader.config.loader import ConfigLoader
from cli_downloader.core.errors import TaskNotFoundError
from cli_downloader.core.task_tree import (
    TaskFileNode,
    TaskKind,
    TaskNode,
    TaskProgress,
    TaskRootSummary,
    TaskState,
)
from tests.example_site import build_example_runtime
from cli_downloader.plugins.catalog import (
    CATALOG_REPOSITORY,
    PluginCatalog,
    PluginCatalogEntry,
)
from cli_downloader.plugins.runtime import build_application_runtime
from tests.support import make_environment


class InteractiveShellTest(TestCase):
    def test_plugin_attention_status_replaces_installed_prefix(self) -> None:
        english = TranslationService("en-US")
        traditional_chinese = TranslationService("zh-TW")

        self.assertEqual(_plugin_status_label("Update Available", english), "Update Available")
        self.assertEqual(_plugin_status_label("Restart Required", english), "Restart Required")
        self.assertEqual(_plugin_status_label("Load Failed", english), "Load Failed")
        self.assertEqual(
            _plugin_status_label("Update Available", traditional_chinese), "有可用更新"
        )
        self.assertEqual(
            _plugin_status_label("Restart Required", traditional_chinese), "需要重新啟動"
        )
        self.assertEqual(_plugin_status_label("Load Failed", traditional_chinese), "載入失敗")

    def test_shell_download_result_is_concise(self) -> None:
        output = _format_shell_output(
            """{
                "command": "download",
                "status": "completed",
                "title": "Example Gallery",
                "asset_count": 2,
                "output_files": ["downloads/example/001.jpg", "downloads/example/002.jpg"],
                "events": ["planned", "started", "finished"],
                "operation_result": {"data": {"metadata": {"internal": true}}}
            }"""
        )

        self.assertEqual(
            output,
            "\n".join(
                (
                    "Download complete",
                    "  Status: Completed",
                    "  Title: Example Gallery",
                    "  Files: 2",
                    f"  Folder: {Path('downloads/example')}",
                )
            ),
        )
        self.assertNotIn("Events", output)
        self.assertNotIn("Operation Result", output)

    def test_shell_batch_result_summarizes_items(self) -> None:
        output = _format_shell_output(
            """{
                "command": "download",
                "status": "partially_completed",
                "output_files": ["downloads/one/001.jpg"],
                "batch": {"counts": {"completed": 1, "failed": 1, "skipped": 0, "total": 2}}
            }"""
        )

        self.assertIn("Batch complete", output)
        self.assertIn("Status: Partially Completed", output)
        self.assertIn("Items: 1/2", output)
        self.assertNotIn("Counts", output)

    def test_shell_error_hides_internal_fields(self) -> None:
        output = _format_shell_output(
            '{"error_type":"UnsupportedInputUrl","error":"URL is not supported",'
            '"diagnostics":{"trace":"internal"}}'
        )

        self.assertEqual(output, "Error\n  x URL is not supported")

    def test_tty_starts_textual_shell(self) -> None:
        with patch("sys.stdin.isatty", return_value=True):
            with patch("sys.stdout.isatty", return_value=True):
                with patch(
                    "cli_downloader.cli.shell.TextualShellApp.run",
                    return_value=0,
                ) as run:
                    exit_code = InteractiveShell().run()

        self.assertEqual(exit_code, 0)
        run.assert_called_once_with()

    def test_help_and_exit(self) -> None:
        output = StringIO()
        shell = InteractiveShell()

        exit_code = shell.run(
            input_stream=StringIO("help\nexit\n"),
            output_stream=output,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Commands:", output.getvalue())
        self.assertIn("download <url> --dry-run", output.getvalue())
        self.assertNotIn("confirm-live-network", output.getvalue())

    def test_shell_warns_before_releasing_owner_with_unfinished_tasks(self) -> None:
        output = StringIO()
        shell = InteractiveShell(
            shutdown_warning=lambda: "2 unfinished tasks are persisted; resume later."
        )

        exit_code = shell.run(
            input_stream=StringIO("exit\n"),
            output_stream=output,
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("Warning: 2 unfinished tasks are persisted; resume later.", output.getvalue())

    def test_help_and_errors_follow_the_selected_interface_language(self) -> None:
        output = StringIO()
        shell = InteractiveShell()
        shell.set_language("zh-TW")

        keep_running, exit_code = shell.execute_with_status("help", output)
        self.assertTrue(keep_running)
        self.assertEqual(exit_code, 0)
        self.assertIn("指令：", output.getvalue())
        self.assertIn("下載項目或開啟清單", output.getvalue())

        output.seek(0)
        output.truncate(0)
        keep_running, exit_code = shell.execute_with_status("not-a-command", output)
        self.assertTrue(keep_running)
        self.assertEqual(exit_code, 2)
        self.assertIn("錯誤:", output.getvalue())
        self.assertIn("輸入 `help` 查看可用指令。", output.getvalue())

    def test_dispatches_allowed_command_to_runner(self) -> None:
        output = StringIO()
        calls: list[tuple[str, ...]] = []

        def runner(argv: tuple[str, ...]) -> int:
            calls.append(tuple(argv))
            print("runner output")
            return 0

        shell = InteractiveShell(runner=runner)

        exit_code = shell.run(
            input_stream=StringIO("sites\nquit\n"),
            output_stream=output,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("sites",)])
        self.assertIn("runner output", output.getvalue())

    def test_download_dry_run_is_allowed(self) -> None:
        output = StringIO()
        calls: list[tuple[str, ...]] = []

        def runner(argv: tuple[str, ...]) -> int:
            calls.append(tuple(argv))
            return 0

        shell = InteractiveShell(runner=runner)

        exit_code = shell.run(
            input_stream=StringIO("download https://example.test/g/123/abcdef/ --dry-run\nexit\n"),
            output_stream=output,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls,
            [("download", "https://example.test/g/123/abcdef/", "--dry-run")],
        )

    def test_batch_show_is_allowed(self) -> None:
        calls: list[tuple[str, ...]] = []
        shell = InteractiveShell(runner=lambda argv: calls.append(tuple(argv)) or 0)

        shell.run(
            input_stream=StringIO("batches show parent-1\nexit\n"),
            output_stream=StringIO(),
        )

        self.assertEqual(calls, [("batches", "show", "parent-1")])

    def test_plain_shell_dispatches_download_execution(self) -> None:
        output = StringIO()
        calls: list[tuple[str, ...]] = []

        def runner(argv: tuple[str, ...]) -> int:
            calls.append(tuple(argv))
            return 0

        shell = InteractiveShell(runner=runner)

        exit_code = shell.run(
            input_stream=StringIO("download example://gallery/welcome\nexit\n"),
            output_stream=output,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(calls, [("download", "example://gallery/welcome")])

    def test_plain_shell_normalizes_only_a_single_http_url(self) -> None:
        calls: list[tuple[str, ...]] = []
        shell = InteractiveShell(runner=lambda argv: calls.append(tuple(argv)) or 0)

        shell.run(
            input_stream=StringIO(
                "https://www.pixiv.net/artworks/133927048\n"
                "https://example.test/gallery extra\n"
                "file:///tmp/gallery\n"
                "exit\n"
            ),
            output_stream=StringIO(),
        )

        self.assertEqual(
            calls,
            [("download", "https://www.pixiv.net/artworks/133927048")],
        )

    def test_batch_selection_requires_runner(self) -> None:
        output = StringIO()

        exit_code = InteractiveShell().run_batch_selection(
            (),
            None,
            None,
            output,
            Event(),
            lambda reason, payload: None,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(), "Batch selection runner is unavailable.\n")

    def test_task_columns_give_extra_width_to_name(self) -> None:
        narrow = _task_column_widths(80)
        wide = _task_column_widths(120)

        self.assertEqual(narrow["progress"], wide["progress"])
        self.assertGreater(wide["name"], narrow["name"])

    def test_cli_shell_entrypoint_can_exit(self) -> None:
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from cli_downloader.cli.app import main

        output = StringIO()

        with patch("sys.stdin", StringIO("exit\n")):
            with redirect_stdout(output):
                exit_code = main(["shell"])

        self.assertEqual(exit_code, 0)
        self.assertIn("CLI-Downloader shell", output.getvalue())

    def test_cli_shell_reuses_one_application_runtime(self) -> None:
        from contextlib import redirect_stdout

        from cli_downloader.cli.app import main

        output = StringIO()
        runtime = build_example_runtime()

        with patch(
            "cli_downloader.cli.app.build_application_runtime", return_value=runtime
        ) as build:
            with patch("sys.stdin", StringIO("sites\nsites\nexit\n")):
                with redirect_stdout(output):
                    exit_code = main(["shell"])

        self.assertEqual(exit_code, 0)
        build.assert_called_once()
        self.assertEqual(output.getvalue().count("example\tExample Site"), 2)


class TextualShellAppTest(IsolatedAsyncioTestCase):
    async def test_plugins_page_reports_activation_failure_instead_of_restart(self) -> None:
        with TemporaryDirectory() as directory:
            plugins_dir = Path(directory) / "plugins"
            plugin_dir = plugins_dir / "broken"
            manifests_dir = plugin_dir / "manifests"
            manifests_dir.mkdir(parents=True)
            (plugins_dir / "registry.json").write_text(
                json.dumps({"broken": {"git_url": "https://example.test/broken.git"}}),
                encoding="utf-8",
            )
            (plugin_dir / "plugin.json").write_text(
                json.dumps(
                    {
                        "plugin_id": "broken",
                        "display_name": "Broken Plugin",
                        "version": "1.2.3",
                        "description": "A plugin that fails during activation.",
                        "entrypoint": "plugin.py:MissingPlugin",
                        "manifest": "site.json",
                    }
                ),
                encoding="utf-8",
            )
            (manifests_dir / "site.json").write_text(
                json.dumps(
                    {
                        "site_id": "broken",
                        "display_name": "Broken Plugin",
                        "domains": ["broken.example.test"],
                        "url_rules": [],
                        "operations": {},
                    }
                ),
                encoding="utf-8",
            )
            (plugin_dir / "plugin.py").write_text("class OtherPlugin: pass\n", encoding="utf-8")
            runtime = build_application_runtime(
                plugin_roots=(plugins_dir,),
                enable_external_code=True,
            )
            app = TextualShellApp(InteractiveShell(runtime=runtime, plugins_dir=plugins_dir))

            async with app.run_test():
                app.query_one(TabbedContent).active = "plugins"
                table = app.query_one("#plugins-table", DataTable)
                self.assertEqual(str(table.get_row("broken")[1]), "Broken Plugin")
                self.assertEqual(str(table.get_row("broken")[2]), "1.2.3")
                self.assertEqual(str(table.get_row("broken")[6]), "Load Failed")
                app._select_plugin("broken")
                detail = str(app.query_one("#plugin-detail", Static).content)
                self.assertIn("entrypoint class not found: MissingPlugin", detail)
                self.assertNotIn("restart shell", detail.lower())

    async def test_plugins_page_merges_catalog_sources_and_preserves_install_source(self) -> None:
        catalog = PluginCatalog(
            entries=(
                PluginCatalogEntry(
                    plugin_id="official-example",
                    repository_url="https://github.com/example/official.git",
                    source="official",
                ),
                PluginCatalogEntry(
                    plugin_id="community-example",
                    repository_url="https://github.com/example/community.git",
                    source="community",
                ),
            ),
            status="cached",
            refreshed_at=100.0,
        )
        service = SimpleNamespace(load=lambda **_kwargs: catalog)

        def metadata(url: str) -> dict[str, object]:
            plugin_id = "community-example" if "community" in url else "official-example"
            return {
                "id": plugin_id,
                "name": plugin_id,
                "version": "1.0.0",
                "description": "Catalog plugin",
                "git_url": url,
            }

        with TemporaryDirectory() as directory:
            app = TextualShellApp(
                InteractiveShell(
                    runtime=build_example_runtime(),
                    plugins_dir=Path(directory) / "plugins",
                    cache_dir=Path(directory) / "cache",
                    plugin_catalog_service=service,
                )
            )
            with (
                patch(
                    "cli_downloader.cli.shell.fetch_remote_plugin_metadata",
                    side_effect=metadata,
                ),
                patch("cli_downloader.cli.shell.install_plugin", return_value="1.0.0") as install,
            ):
                async with app.run_test() as pilot:
                    app.query_one(TabbedContent).active = "plugins"
                    worker = app._fetch_remote_plugins()
                    await worker.wait()
                    await pilot.pause()
                    table = app.query_one("#plugins-table", DataTable)
                    self.assertEqual(str(table.get_row("official-example")[6]), "Not Installed")
                    self.assertEqual(str(table.get_row("community-example")[6]), "Not Installed")
                    self.assertEqual(str(table.get_row("official-example")[3]), "Official")
                    self.assertEqual(str(table.get_row("community-example")[3]), "Community")
                    self.assertIn(
                        "cached (2 entries)",
                        str(app.query_one("#plugin-catalog-status", Static).content),
                    )

                    app._select_plugin("community-example")
                    detail = str(app.query_one("#plugin-detail", Static).content)
                    self.assertIn("ID: community-example", detail)
                    self.assertIn("Source: Community", detail)
                    self.assertIn("Description: Catalog plugin", detail)
                    app._shell.set_language("zh-TW")
                    app._apply_translations()
                    app._load_plugins()
                    app._select_plugin("community-example")
                    localized_detail = str(app.query_one("#plugin-detail", Static).content)
                    self.assertIn("名稱:", localized_detail)
                    self.assertIn("來源: 社群", localized_detail)
                    self.assertIn("說明:", localized_detail)
                    self.assertIn("未安裝", str(table.get_row("community-example")[6]))
                    app._install_plugin()

            install.assert_called_once_with(
                Path(directory) / "plugins",
                "https://github.com/example/community.git",
                source="community",
                catalog_id=CATALOG_REPOSITORY,
            )

    async def test_settings_about_shows_version_and_opens_repository(self) -> None:
        runtime = build_example_runtime()
        opened_urls: list[str] = []
        app = TextualShellApp(
            InteractiveShell(
                runtime=runtime,
                url_opener=opened_urls.append,
            )
        )

        async with app.run_test():
            app.query_one(TabbedContent).active = "settings"
            app._open_setting_row("nav:about")

            self.assertEqual(
                str(app.query_one("#settings-path", Static).content),
                "Settings / About",
            )
            detail = str(app.query_one("#settings-detail", Static).content)
            self.assertIn(f"Version: {__version__}", detail)
            self.assertIn("https://github.com/RyuuMeow/CLI-Downloader", detail)
            self.assertFalse(app.query_one("#settings-github-btn", Button).disabled)
            self.assertFalse(app.query_one("#settings-update-check-btn", Button).disabled)
            self.assertTrue(app.query_one("#settings-release-btn", Button).disabled)
            app._open_settings_github()

        self.assertEqual(opened_urls, ["https://github.com/RyuuMeow/CLI-Downloader"])

    async def test_settings_about_checks_and_opens_available_release(self) -> None:
        opened_urls: list[str] = []
        release_url = "https://github.com/RyuuMeow/CLI-Downloader/releases/tag/v9.9.9"
        service = SimpleNamespace(
            check=lambda: ReleaseUpdateResult(
                ReleaseUpdateStatus.UPDATE_AVAILABLE,
                __version__,
                "9.9.9",
                release_url,
            )
        )
        app = TextualShellApp(
            InteractiveShell(
                runtime=build_example_runtime(),
                url_opener=opened_urls.append,
            ),
            release_update_service=service,
        )

        async with app.run_test() as pilot:
            app.query_one(TabbedContent).active = "settings"
            app._open_setting_row("nav:about")
            table = app.query_one("#settings-table", DataTable)
            self.assertEqual(str(table.get_row("about:update")[2]), "Not checked")

            worker = app._start_release_update_check()
            self.assertIsNotNone(worker)
            await worker.wait()
            await pilot.pause()

            self.assertEqual(
                str(table.get_row("about:update")[2]),
                "Update available (9.9.9)",
            )
            self.assertFalse(app.query_one("#settings-release-btn", Button).disabled)
            app._open_settings_release()

            app._shell.set_language("zh-TW")
            app._apply_translations()
            app._show_settings_level()
            self.assertEqual(
                str(table.get_row("about:update")[2]),
                "有可用更新（9.9.9）",
            )

        self.assertEqual(opened_urls, [release_url])

    async def test_plugin_detail_keeps_full_metadata_when_table_refreshes(self) -> None:
        catalog = PluginCatalog(
            entries=(
                PluginCatalogEntry(
                    plugin_id="long-author",
                    repository_url="https://github.com/example/long-author.git",
                    source="community",
                    author="A Very Long Plugin Author Name That Must Be Kept In Details",
                ),
            ),
            status="cached",
            refreshed_at=100.0,
        )
        service = SimpleNamespace(load=lambda **_kwargs: catalog)
        runtime = build_example_runtime()

        def metadata(url: str) -> dict[str, object]:
            return {
                "id": "long-author",
                "name": "Long Author Plugin",
                "version": "2.0.0",
                "description": "A complete description that remains available in details.",
                "git_url": url,
            }

        app = TextualShellApp(
            InteractiveShell(
                runtime=runtime,
                plugin_catalog_service=service,
            )
        )
        with patch(
            "cli_downloader.cli.shell.fetch_remote_plugin_metadata",
            side_effect=metadata,
        ):
            async with app.run_test(size=(80, 30)) as pilot:
                app.query_one(TabbedContent).active = "plugins"
                worker = app._fetch_remote_plugins()
                await worker.wait()
                await pilot.pause()
                app._select_plugin("long-author")

                detail = str(app.query_one("#plugin-detail", Static).content)
                self.assertIn(
                    "Author: A Very Long Plugin Author Name That Must Be Kept In Details", detail
                )
                self.assertIn("Available version: 2.0.0", detail)
                self.assertLessEqual(
                    app.query_one("#plugins-table", DataTable)
                    .columns[app._plugin_column_keys["author"]]
                    .width,
                    18,
                )

                app._load_plugins()
                self.assertEqual(app._selected_plugin_id, "long-author")
                self.assertIn(
                    "ID: long-author",
                    str(app.query_one("#plugin-detail", Static).content),
                )

    async def test_plugin_attention_status_keeps_installed_actions_enabled(self) -> None:
        with TemporaryDirectory() as directory:
            app = TextualShellApp(
                InteractiveShell(
                    runtime=build_example_runtime(),
                    plugins_dir=Path(directory) / "plugins",
                )
            )

            async with app.run_test():
                app.query_one(TabbedContent).active = "plugins"
                for status in ("Update Available", "Restart Required"):
                    app._plugin_rows["attention"] = {
                        "name": "Attention",
                        "status": status,
                        "installed": True,
                    }
                    app._select_plugin("attention")
                    self.assertFalse(app.query_one("#plugin-update-btn", Button).disabled)
                    self.assertFalse(app.query_one("#plugin-remove-btn", Button).disabled)

    async def test_settings_explorer_edits_persists_and_masks_values(self) -> None:
        runtime = build_example_runtime()
        with TemporaryDirectory() as directory:
            paths = make_environment(Path(directory)).paths
            loader = ConfigLoader(paths=paths, registry=runtime.settings_registry)
            app = TextualShellApp(
                InteractiveShell(
                    settings_registry=runtime.settings_registry,
                    config_loader=loader,
                )
            )

            async with app.run_test():
                app.query_one(TabbedContent).active = "settings"
                app._open_setting_row("nav:download")
                self.assertEqual(
                    str(app.query_one("#settings-path", Static).content),
                    "Settings / Download",
                )
                app._select_setting("field:download.folder_template")
                editor = app.query_one("#settings-editor", Input)
                editor.value = "{site_id}/{gallery_id}"
                app._apply_setting()
                app._save_settings()

                app._settings_path = ()
                app._show_settings_level()
                app._open_setting_row("nav:interface")
                self.assertEqual(
                    str(app.query_one("#settings-path", Static).content),
                    "Settings / Interface",
                )
                app._select_setting("field:interface.language")
                language = app.query_one("#settings-select", Select)
                self.assertTrue(language.display)
                language.value = "zh-TW"
                app._apply_setting()
                app._save_settings()
                self.assertEqual(app._shell.translator.locale, "zh-TW")
                self.assertEqual(
                    str(app.query_one("#shell-tabs").get_tab("settings").label),
                    "設定",
                )
                self.assertEqual(app.query_one("#settings-save", Button).label, "儲存")
                self.assertEqual(app.query_one("#task-back", Button).label, "←")
                app._settings_path = ()
                app._show_settings_level()
                settings_rows = [
                    str(app.query_one("#settings-table", DataTable).get_row_at(index)[1])
                    for index in range(app.query_one("#settings-table", DataTable).row_count)
                ]
                self.assertIn("下載", settings_rows)
                self.assertIn("網路", settings_rows)

                app._select_setting("field:download.strategy")
                strategy = app.query_one("#settings-select", Select)
                self.assertTrue(strategy.display)
                self.assertFalse(editor.display)
                strategy.value = "direct"
                app._apply_setting()
                app._save_settings()

            loaded = loader.load()
            self.assertEqual(
                loaded.values["download.folder_template"],
                "{site_id}/{gallery_id}",
            )
            self.assertEqual(loaded.values["download.strategy"], "direct")
            self.assertEqual(loaded.values["interface.language"], "zh-TW")

    async def test_plugins_table_columns_follow_viewport_after_resize(self) -> None:
        runtime = build_example_runtime()
        empty_catalog = PluginCatalog((), "cached", 100.0)
        app = TextualShellApp(
            InteractiveShell(
                runtime=runtime,
                plugin_catalog_service=SimpleNamespace(load=lambda **_kwargs: empty_catalog),
            )
        )

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "plugins"
            for _ in range(3):
                await pilot.pause()
            table = app.query_one("#plugins-table", DataTable)

            for index in range(40):
                table.add_row(
                    f"plugin-{index}",
                    "Example",
                    "1.0",
                    "Local",
                    "Example Author",
                    "A plugin description",
                    "Installed",
                    key=f"extra-{index}",
                )
            for _ in range(2):
                await pilot.pause()

            for width in (120, 80):
                if width != 120:
                    await pilot.resize_terminal(width, 30)
                    await pilot.pause()
                app._resize_plugin_columns()
                await pilot.pause()

                viewport_width = table.scrollable_content_region.width
                rendered_width = sum(
                    column.get_render_width(table) for column in table.columns.values()
                )
                self.assertEqual(rendered_width, viewport_width)
                self.assertEqual(
                    table.virtual_size.width,
                    viewport_width + getattr(table, "_row_label_column_width", 0),
                )
                if width == 120:
                    self.assertGreaterEqual(
                        table.columns[app._plugin_column_keys["status"]].width,
                        _PLUGIN_STATUS_MIN_WIDTH,
                    )

    async def test_tasks_table_fills_viewport_and_scrolls_only_when_narrow(self) -> None:
        tasks = tuple(
            TaskNode(
                f"task-{index}",
                TaskKind.BATCH if index == 0 else TaskKind.ITEM,
                TaskState.COMPLETED,
                f"Task {index}",
                TaskProgress(165, 165) if index == 0 else TaskProgress(1, 1),
                site_id="example",
                details={"created_at": "2026-07-16T12:34:56+08:00"},
            )
            for index in range(40)
        )
        service = SimpleNamespace(list_roots=lambda: tasks)
        app = TextualShellApp(
            InteractiveShell(
                runtime=build_example_runtime(),
                task_service_loader=lambda: service,
            )
        )

        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()
            table = app.query_one("#task-table", DataTable)

            wide_viewport = table.scrollable_content_region.width
            wide_rendered = sum(
                column.get_render_width(table) for column in table.columns.values()
            )
            self.assertEqual(wide_rendered, wide_viewport)
            self.assertGreaterEqual(
                table.columns[app._task_column_keys["type"]].width,
                len("Batch") + 2,
            )
            self.assertGreaterEqual(
                table.columns[app._task_column_keys["progress"]].width,
                len("165/165 files"),
            )
            self.assertGreaterEqual(
                table.columns[app._task_column_keys["status"]].width,
                len("Completed"),
            )
            self.assertGreaterEqual(
                table.columns[app._task_column_keys["created"]].width,
                len("2026-07-16 12:34"),
            )

            await pilot.resize_terminal(45, 30)
            await pilot.pause()
            app._resize_task_columns()
            await pilot.pause()
            narrow_viewport = table.scrollable_content_region.width
            narrow_rendered = sum(
                column.get_render_width(table) for column in table.columns.values()
            )
            self.assertGreater(narrow_rendered, narrow_viewport)
            self.assertGreater(table.virtual_size.width, narrow_viewport)

    async def test_embedded_example_list_retains_state_paginates_and_downloads(self) -> None:
        runtime = build_example_runtime()
        dispatcher = OperationDispatcher(runtime)
        operation_calls: list[dict[str, object]] = []
        download_calls: list[tuple[str, ...]] = []

        def load_operation(url, params):
            operation_calls.append(dict(params))
            result = asyncio.run(
                dispatcher.dispatch(
                    url,
                    request_id=f"request-{len(operation_calls)}",
                    params=params,
                )
            )
            return "example", result

        batch_source_urls: list[str | None] = []

        def run_batch_selection(items, source_url, title, cancellation, progress) -> int:
            download_calls.append(tuple(item.item_id for item in items))
            batch_source_urls.append(source_url)
            return 0

        app = TextualShellApp(
            InteractiveShell(
                operation_runner=load_operation,
                batch_selection_runner=run_batch_selection,
            )
        )

        async with app.run_test() as pilot:
            await pilot.press(*"https://example.test/list?page=1", "enter")
            for _ in range(30):
                await pilot.pause()
                if app._operation_thread is None:
                    break

            self.assertEqual(
                app._history,
                ["download 'https://example.test/list?page=1'"],
            )
            self.assertEqual(
                app._command_blocks[0].command,
                "download 'https://example.test/list?page=1'",
            )

            choices = app.query_one("#results", SelectionList)
            marker = "".join(segment.text for segment in choices.render_line(0))
            self.assertTrue(marker.startswith("[   ] "))
            self.assertGreater(choices.size.height, choices.option_count)
            self.assertEqual(choices.styles.max_height.value, 100)
            choices.focus()
            await pilot.press("space")
            await pilot.pause()
            marker = "".join(segment.text for segment in choices.render_line(0))
            self.assertTrue(marker.startswith("[ x ] "))
            page_view = app.query_one(CorePageView)
            self.assertEqual(page_view.session.selected_item_ids, ("welcome",))

            tabs = app.query_one(TabbedContent)
            tabs.active = "tasks"
            await pilot.pause()
            tabs.active = "operation"
            await pilot.pause()
            self.assertEqual(page_view.session.selected_item_ids, ("welcome",))
            self.assertIs(app.query_one("#operation-page"), app.query_one(CorePageView))

            choices.focus()
            await pilot.press("right")
            for _ in range(30):
                await pilot.pause()
                if app._operation_thread is None and len(operation_calls) == 2:
                    break

            self.assertEqual(operation_calls, [{}, {"page": 2}])
            self.assertEqual(app._operation_session.current_page, 2)
            self.assertEqual(app._operation_result.data.items[0].item_id, "portrait")

            second_page = app.query_one("#results", SelectionList)
            second_page.focus()
            await pilot.press("space")
            self.assertEqual(
                str(app.query_one("#operation-view-status", Static).render()),
                "Ready | 2 selected",
            )

            await pilot.press("left")
            for _ in range(30):
                await pilot.pause()
                if app._operation_thread is None and len(operation_calls) == 3:
                    break
            first_page = app.query_one("#results", SelectionList)
            self.assertEqual(tuple(first_page.selected), ("welcome",))
            first_page.focus()
            await pilot.press("space", "down", "space", "right")
            for _ in range(30):
                await pilot.pause()
                if app._operation_thread is None and len(operation_calls) == 4:
                    break
            self.assertEqual(
                str(app.query_one("#operation-view-status", Static).render()),
                "Ready | 2 selected",
            )
            self.assertEqual(
                tuple(app.query_one("#results", SelectionList).selected),
                ("portrait",),
            )
            app.query_one("#download_selected").focus()
            await pilot.press("enter")
            for _ in range(30):
                await pilot.pause()
                if not app._background_jobs:
                    break

            self.assertFalse(app._background_jobs)
            self.assertIsNone(app._operation_source_url)
            self.assertIsNone(app._operation_plugin_id)
            self.assertIsNone(app._operation_result)
            self.assertIsNone(app._operation_session)
            self.assertEqual(app._selected_items, {})
            self.assertEqual(
                str(app.query_one("#operation-view-status", Static).render()),
                "No active operation.",
            )
            self.assertEqual(tuple(app.query_one("#operation-host").children), ())

            await pilot.press(*"https://example.test/list?page=1", "enter")
            for _ in range(30):
                await pilot.pause()
                if app._operation_thread is None and app._operation_result is not None:
                    break
            self.assertIsNotNone(app._operation_result)
            self.assertEqual(len(app._selected_items), 0)
            self.assertEqual(
                str(app.query_one("#operation-view-status", Static).render()),
                "Ready | 0 selected",
            )

        self.assertEqual(
            download_calls,
            [("portrait", "landscape")],
        )
        self.assertEqual(batch_source_urls, ["https://example.test/list?page=1"])

    async def test_operation_submission_failure_and_cancel_keep_selection(self) -> None:
        runtime = build_example_runtime()
        dispatcher = OperationDispatcher(runtime)
        mode = "failed"
        started = Event()

        def load_operation(url, params):
            result = asyncio.run(
                dispatcher.dispatch(
                    url,
                    request_id="operation-request",
                    params=params,
                )
            )
            return "example", result

        def run_batch_selection(items, source_url, title, cancellation, progress) -> int:
            del items, source_url, title, progress
            if mode == "cancel":
                started.set()
                cancellation.wait(3)
                return 130 if cancellation.is_set() else 0
            return 1

        app = TextualShellApp(
            InteractiveShell(
                operation_runner=load_operation,
                batch_selection_runner=run_batch_selection,
            )
        )

        async with app.run_test() as pilot:
            await pilot.press(*"https://example.test/list?page=1", "enter")
            for _ in range(30):
                await pilot.pause()
                if app._operation_thread is None and app._operation_result is not None:
                    break

            choices = app.query_one("#results", SelectionList)
            choices.focus()
            await pilot.press("space")
            self.assertEqual(len(app._selected_items), 1)

            app.query_one("#download_selected").focus()
            await pilot.press("enter")
            for _ in range(30):
                await pilot.pause()
                if not app._background_jobs:
                    break

            self.assertFalse(app._background_jobs)
            # Ensure the operation tab re-activation has settled before
            # pressing the download button again (CI scheduling variance).
            for _ in range(30):
                await pilot.pause()
                if app.query_one(TabbedContent).active == "operation":
                    break
            self.assertIsNotNone(app.query_one("#operation-page", CorePageView))
            self.assertEqual(len(app._selected_items), 1)
            self.assertIn(
                "selected",
                str(app.query_one("#operation-view-status", Static).render()),
            )

            mode = "cancel"
            started.clear()
            # The active tab property changes before Textual has necessarily
            # completed the pane visibility/focus transition. Clicking through
            # Pilot targets the rendered action instead of stale Console focus.
            await pilot.click("#download_selected")
            for _ in range(30):
                await pilot.pause()
                if started.is_set():
                    break
            # Windows CI can take longer to schedule the Textual worker after
            # the operation page has been redrawn; this is not the cancellation
            # behavior under test.
            self.assertTrue(await asyncio.to_thread(started.wait, 30))
            app._cancel_operation()
            for _ in range(30):
                await pilot.pause()
                if not app._background_jobs:
                    break

            self.assertFalse(app._background_jobs)
            self.assertIsNotNone(app.query_one("#operation-page", CorePageView))
            self.assertEqual(len(app._selected_items), 1)
            self.assertIn(
                "selected",
                str(app.query_one("#operation-view-status", Static).render()),
            )

    async def test_textual_shell_dispatches_commands_and_recalls_history(self) -> None:
        calls: list[tuple[str, ...]] = []

        def runner(argv: tuple[str, ...]) -> int:
            calls.append(tuple(argv))
            print("example output")
            return 0

        app = TextualShellApp(InteractiveShell(runner=runner))
        async with app.run_test() as pilot:
            command = app.query_one("#shell-input", Input)
            await pilot.press(*"sites", "enter", "up")
            await pilot.pause()

            self.assertEqual(calls, [("sites",)])
            self.assertEqual(command.value, "sites")
            self.assertIn(
                "example output",
                "\n".join(line.text for line in app.query_one(RichLog).lines),
            )

    async def test_async_cli_command_runs_outside_textual_event_loop(self) -> None:
        async def inspect() -> str:
            await asyncio.sleep(0)
            return "inspection complete"

        def runner(argv: tuple[str, ...]) -> int:
            self.assertEqual(argv, ("sites",))
            print(asyncio.run(inspect()))
            return 0

        app = TextualShellApp(InteractiveShell(runner=runner))
        async with app.run_test() as pilot:
            await pilot.press(*"sites", "enter")
            for _ in range(30):
                await pilot.pause()
                if app._operation_thread is None:
                    break

            output = "\n".join(line.text for line in app.query_one(RichLog).lines)
            self.assertIsNone(app._operation_thread)
            self.assertIn("inspection complete", output)
            self.assertNotIn("asyncio.run() cannot be called", output)
            self.assertEqual(app._command_blocks[-1].status, "completed")

    async def test_console_blocks_are_readable_copyable_and_clearable(self) -> None:
        def runner(argv: tuple[str, ...]) -> int:
            print('[{"site_id":"example","name":"Example Site"}]')
            return 0

        app = TextualShellApp(InteractiveShell(runner=runner))
        async with app.run_test() as pilot:
            await pilot.press(*"sites", "enter")
            await pilot.pause()
            log = app.query_one(RichLog)
            output = "\n".join(line.text for line in log.lines)

            self.assertIn("> sites", output)
            self.assertIn("+ Completed", output)
            self.assertIn("Site Id", output)
            self.assertIn("Example Site", output)
            self.assertNotIn('{"site_id"', output)
            self.assertTrue(log.allow_select)
            self.assertTrue(app.screen.allow_select)

            await pilot.press(*"unsupported", "enter")
            await pilot.pause()
            output = "\n".join(line.text for line in log.lines)
            self.assertIn("> unsupported", output)
            self.assertIn("x Failed", output)
            self.assertIn("Run help", output)
            self.assertNotIn("error_type", output)

            command = app.query_one("#shell-input", Input)
            command.value = 'download "'
            command.focus()
            await pilot.press("enter")
            await pilot.pause()
            output = "\n".join(line.text for line in log.lines)
            self.assertIn("Could not parse this command", output)
            self.assertNotIn("ShellParseError", output)

            app.query_one("#copy-command", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn('> download "', app.clipboard)
            self.assertIn("x Failed", app.clipboard)

            app.query_one("#shell-input", Input).focus()
            await pilot.press(*"clear", "enter")
            await pilot.pause()
            self.assertEqual(len(log.lines), 0)
            await pilot.press(*"sites", "enter", *"cls", "enter")
            await pilot.pause()
            self.assertEqual(len(log.lines), 0)
            self.assertEqual(
                app._history,
                ["sites", "unsupported", 'download "', "clear", "sites", "cls"],
            )

        for color in ("#171717", "#222222", "#5c4026", "#e58a32", "#ffaa55"):
            self.assertIn(color, TextualShellApp.CSS)
        self.assertIn(
            "ContentTabs:focus ContentTab.-active { background: #222222; }",
            TextualShellApp.CSS,
        )
        self.assertIn("overflow-x: auto", TextualShellApp.CSS)

    async def test_task_view_supports_keyboard_rows_and_details(self) -> None:
        loads = 0
        tasks = (
            TaskNode(
                "task-1",
                TaskKind.ITEM,
                TaskState.COMPLETED,
                "First",
                TaskProgress(1, 1),
                site_id="example",
            ),
            TaskNode(
                "task-2",
                TaskKind.ITEM,
                TaskState.FAILED,
                "Second",
                TaskProgress(0, 1),
                site_id="sample",
            ),
        )

        def list_roots() -> tuple[TaskNode, ...]:
            nonlocal loads
            loads += 1
            return tasks

        service = SimpleNamespace(list_roots=list_roots)
        shell = InteractiveShell(task_service_loader=lambda: service)
        app = TextualShellApp(shell)

        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()
            app._refresh_tasks_if_active()
            self.assertEqual(loads, 1)
            app._refresh_tasks()
            table = app.query_one("#task-table", DataTable)
            table.focus()
            await pilot.press("down")
            await pilot.pause()

            detail = str(app.query_one("#task-detail", Static).content)
            self.assertEqual(table.row_count, 2)
            self.assertEqual(loads, 2)
            self.assertIn("Completed", detail)
            self.assertIn("1/1 files", detail)
            await pilot.press("escape")
            self.assertEqual(app.query_one(TabbedContent).active, "console")
            self.assertTrue(app.query_one("#shell-input", Input).has_focus)

    async def test_task_hierarchy_and_controls_remain_visible_in_narrow_shell(self) -> None:
        task = TaskNode(
            "task-visible",
            TaskKind.BATCH,
            TaskState.RUNNING,
            "Visible task",
            TaskProgress(0, 1),
            site_id="example",
            source_url="https://example.test/batch",
            output_files=("downloads/example/file.jpg",),
        )
        child = TaskNode(
            "child-visible",
            TaskKind.ITEM,
            TaskState.QUEUED,
            "Visible child",
            TaskProgress(0, 1),
            parent_id=task.task_id,
            site_id="example",
        )
        service = SimpleNamespace(
            list_roots=lambda: (task,),
            list_children=lambda _task_id: (child,),
        )
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))

        async with app.run_test(size=(80, 30)) as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()

            toolbar = app.query_one("#task-toolbar")
            task_path = app.query_one("#task-path", Static)
            pause = app.query_one("#task-pause", Button)
            refresh = app.query_one("#refresh-tasks", Button)

            self.assertTrue(toolbar.display)
            self.assertGreater(toolbar.region.height, 0)
            self.assertGreater(task_path.region.width, 0)
            self.assertGreater(pause.region.width, 0)
            self.assertGreater(refresh.region.width, 0)
            self.assertLess(refresh.region.right, app.screen.region.right + 1)
            self.assertEqual(app.query_one("#task-source", Button).label, "Open URL")
            self.assertEqual(app.query_one("#task-folder", Button).label, "Open Folder")
            self.assertEqual(
                app.query_one("#task-source", Button).parent.id,
                "task-detail-actions",
            )

            app.query_one("#task-table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()

            back = app.query_one("#task-back", Button)
            next_page = app.query_one("#task-page-next", Button)
            self.assertTrue(back.display)
            self.assertTrue(next_page.display)
            self.assertGreater(back.region.width, 0)
            self.assertLess(next_page.region.right, app.screen.region.right + 1)
            self.assertFalse(pause.display)
            self.assertFalse(refresh.display)
            self.assertFalse(app.query_one("#task-cancel", Button).display)
            self.assertFalse(app.query_one("#task-delete", Button).display)
            app._select_task(child.task_id)
            self.assertTrue(app.query_one("#task-source", Button).disabled)
            self.assertTrue(app.query_one("#task-folder", Button).disabled)
            self.assertTrue(app.query_one("#task-detail-actions").display)

    async def test_root_item_folder_and_selection_are_restored_after_back(self) -> None:
        task = TaskNode(
            "item-root",
            TaskKind.ITEM,
            TaskState.COMPLETED,
            "Root item",
            TaskProgress(1, 1),
            site_id="example",
            source_url="https://example.test/item-root",
        )

        class Service:
            def list_root_summaries(self):
                return (
                    TaskRootSummary(
                        task.task_id,
                        task.kind,
                        task.state,
                        task.title,
                        task.progress,
                        site_id=task.site_id,
                        source_url=task.source_url,
                    ),
                )

            def get(self, task_id):
                self.assert_task(task_id)
                return task

            def list_files(self, task_id):
                self.assert_task(task_id)
                return ()

            def get_available_folder(self, task_id):
                self.assert_task(task_id)
                return "downloads/item-root"

            @staticmethod
            def assert_task(task_id):
                if task_id != task.task_id:
                    raise TaskNotFoundError(task_id)

        app = TextualShellApp(InteractiveShell(task_service_loader=Service))

        async with app.run_test(size=(80, 30)) as pilot:
            app.query_one(TabbedContent).active = "tasks"
            for _ in range(20):
                await pilot.pause()
                if app.query_one("#task-table", DataTable).row_count == 1:
                    break
            table = app.query_one("#task-table", DataTable)
            table.focus()
            app._select_task(task.task_id)
            self.assertFalse(app.query_one("#task-folder", Button).disabled)

            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._task_level, "files")

            await pilot.press("escape")
            for _ in range(20):
                await pilot.pause()
                if app._task_level == "roots" and app._task_refresh_thread is None:
                    break

            self.assertEqual(app._active_task_id, task.task_id)
            self.assertTrue(table.has_focus)
            selected_key = tuple(str(key.value) for key in table.rows)[table.cursor_row]
            self.assertEqual(selected_key, task.task_id)
            self.assertFalse(app.query_one("#task-folder", Button).disabled)

    async def test_task_dynamic_labels_follow_selected_language(self) -> None:
        task = TaskNode(
            "task-zh",
            TaskKind.ITEM,
            TaskState.COMPLETED,
            "Gallery",
            TaskProgress(1, 1),
            site_id="example",
        )
        app = TextualShellApp(
            InteractiveShell(
                task_service_loader=lambda: SimpleNamespace(list_roots=lambda: (task,))
            )
        )

        async with app.run_test():
            app._shell.set_language("zh-TW")
            app._apply_translations()
            app._refresh_tasks()
            app._select_task(task.task_id)

            self.assertIn("項目", str(app.query_one("#task-table", DataTable).get_row_at(0)[1]))
            detail = str(app.query_one("#task-detail", Static).content)
            self.assertIn("名稱", detail)
            self.assertIn("狀態", detail)
            self.assertIn("進度", detail)
            self.assertIn("網站", detail)
            self.assertIn("已完成", detail)
            self.assertEqual(app.query_one("#task-source", Button).label, "開啟 URL")
            self.assertEqual(app.query_one("#task-folder", Button).label, "開啟資料夾")

    async def test_empty_unified_task_explorer(self) -> None:
        service = SimpleNamespace(list_roots=lambda: ())
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))

        async with app.run_test():
            self.assertEqual(app.query_one("#task-table", DataTable).row_count, 0)
            self.assertIn(
                "No tasks yet.",
                str(app.query_one("#task-detail", Static).content),
            )

    async def test_external_task_acceptance_refreshes_the_tasks_list(self) -> None:
        tasks: list[TaskNode] = []
        service = SimpleNamespace(list_roots=lambda: tuple(tasks))
        shell = InteractiveShell(task_service_loader=lambda: service)
        app = TextualShellApp(shell)

        async with app.run_test() as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()
            table = app.query_one("#task-table", DataTable)
            self.assertEqual(table.row_count, 0)

            tasks.append(
                TaskNode(
                    "external-task",
                    TaskKind.ITEM,
                    TaskState.QUEUED,
                    "Externally submitted",
                    TaskProgress(0, 1),
                )
            )
            await asyncio.to_thread(shell.notify_external_tasks)
            await pilot.pause()

            self.assertEqual(table.row_count, 1)
            self.assertIn("Externally submitted", str(table.get_row_at(0)[2]))

    async def test_unified_task_explorer_drills_into_batch_items_and_files(self) -> None:
        batch = TaskNode(
            "batch-1",
            TaskKind.BATCH,
            TaskState.PARTIALLY_COMPLETED,
            "Example batch",
            TaskProgress(1, 2),
            site_id="example",
            source_url="https://example.test/search",
            output_files=("downloads/image-01.jpg",),
        )
        failed = TaskNode(
            "entry-1",
            TaskKind.ITEM,
            TaskState.FAILED,
            "Gallery one",
            TaskProgress(),
            parent_id="batch-1",
            site_id="example",
            source_url="https://example.test/gallery/one",
            details={
                "failure": {
                    "origin": "plugin",
                    "stage": "resolve",
                    "message": "Could not parse gallery",
                    "retryable": True,
                }
            },
        )
        partial = TaskNode(
            "entry-2",
            TaskKind.ITEM,
            TaskState.PARTIALLY_COMPLETED,
            "Gallery two",
            TaskProgress(1, 2),
            parent_id="batch-1",
            site_id="example",
            source_url="https://example.test/gallery/two",
            output_files=("downloads/image-01.jpg",),
        )
        file = TaskFileNode(
            "file-1",
            "image-02.jpg",
            "failed",
            attempts=2,
            location="downloads/image-02.part",
            failure={
                "origin": "system",
                "stage": "fetch",
                "message": "Server rejected image",
                "retryable": True,
            },
        )
        service = SimpleNamespace(
            list_roots=lambda: (batch,),
            list_children=lambda task_id: (failed, partial),
            list_files=lambda task_id: () if task_id == "entry-1" else (file,),
        )
        opened_urls: list[str] = []
        opened_folders: list[str] = []
        opened_files: list[str] = []
        app = TextualShellApp(
            InteractiveShell(
                task_service_loader=lambda: service,
                url_opener=opened_urls.append,
                folder_opener=opened_folders.append,
                file_opener=opened_files.append,
            )
        )

        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "tasks"
            await pilot.pause()
            task_table = app.query_one("#task-table", DataTable)
            self.assertEqual(task_table.row_count, 1)
            self.assertIn("Batch", str(task_table.get_row_at(0)[1]))
            self.assertNotIn("batches", [pane.id for pane in tabs.query("TabPane")])
            source_button = app.query_one("#task-source", Button)
            folder_button = app.query_one("#task-folder", Button)
            self.assertFalse(source_button.disabled)
            self.assertFalse(folder_button.disabled)
            self.assertTrue(source_button.display)
            self.assertTrue(source_button.parent.display)
            self.assertGreater(source_button.region.height, 0)
            self.assertEqual(source_button.label, "Open URL")
            self.assertEqual(folder_button.label, "Open Folder")
            source_button.press()
            folder_button.press()
            await pilot.pause()
            self.assertEqual(
                opened_urls,
                ["https://example.test/search"],
            )
            self.assertEqual(opened_folders, ["downloads"])

            task_table.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._task_level, "items")
            self.assertEqual(task_table.row_count, 2)
            self.assertIn("Tasks / Example batch", str(app.query_one("#task-path", Static).content))

            await pilot.press("enter")
            await pilot.pause()
            detail = str(app.query_one("#task-detail", Static).content)
            self.assertEqual(app._task_level, "files")
            self.assertIn("Could not parse gallery", detail)
            self.assertIn("Site extension", detail)
            self.assertIn("[R] Retry    Available", detail)
            self.assertTrue(app.query_one("#task-back", Button).display)
            self.assertFalse(app.query_one("#task-page-prev", Button).display)
            self.assertFalse(app.query_one("#task-pause", Button).display)
            self.assertFalse(app.query_one("#refresh-tasks", Button).display)
            self.assertTrue(source_button.disabled)
            self.assertTrue(folder_button.disabled)
            self.assertEqual(source_button.label, "Open File")

            await pilot.press("escape", "down", "enter")
            await pilot.pause()
            file_table = app.query_one("#task-file-table", DataTable)
            detail = str(app.query_one("#task-detail", Static).content)
            self.assertEqual(file_table.row_count, 1)
            await pilot.press("enter")
            detail = str(app.query_one("#task-detail", Static).content)
            self.assertIn("Server rejected image", detail)
            self.assertIn("Downloader", detail)
            self.assertNotIn("Child", detail)
            self.assertNotIn("Asset", detail)
            self.assertFalse(source_button.disabled)
            self.assertFalse(folder_button.disabled)
            self.assertEqual(source_button.label, "Open File")
            source_button.press()
            folder_button.press()
            await pilot.pause()
            self.assertEqual(opened_files, ["downloads/image-02.part"])
            self.assertEqual(opened_folders, ["downloads", "downloads"])
            self.assertEqual(opened_urls, ["https://example.test/search"])

            await pilot.press("escape", "escape", "escape")
            self.assertEqual(tabs.active, "console")

    async def test_switching_equal_length_items_replaces_file_rows(self) -> None:
        torrent = TaskNode(
            "torrent-task",
            TaskKind.ITEM,
            TaskState.COMPLETED,
            "Torrent item",
            TaskProgress(1, 1),
            site_id="ehentai",
        )
        direct = TaskNode(
            "direct-task",
            TaskKind.ITEM,
            TaskState.COMPLETED,
            "Direct item",
            TaskProgress(1, 1),
            site_id="pixiv",
        )
        files = {
            "torrent-task": (
                TaskFileNode(
                    "torrent-file",
                    "gallery.torrent",
                    "completed",
                    location="downloads/ehentai/gallery.torrent",
                ),
            ),
            "direct-task": (
                TaskFileNode(
                    "direct-file",
                    "illustration.png",
                    "completed",
                    location="downloads/pixiv/illustration.png",
                ),
            ),
        }
        service = SimpleNamespace(
            list_roots=lambda: (direct, torrent),
            list_files=lambda task_id: files[task_id],
        )
        app = TextualShellApp(
            InteractiveShell(task_service_loader=lambda: service)
        )

        async with app.run_test() as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()
            table = app.query_one("#task-file-table", DataTable)

            app._open_task("torrent-task")
            self.assertEqual(str(table.get_row_at(0)[1]), "gallery.torrent")
            self.assertEqual(tuple(str(key.value) for key in table.rows), ("torrent-file",))

            app._task_back()
            app._open_task("direct-task")
            self.assertEqual(table.row_count, 1)
            self.assertEqual(str(table.get_row_at(0)[1]), "illustration.png")
            self.assertEqual(
                str(table.get_row_at(0)[5]),
                "downloads/pixiv/illustration.png",
            )
            self.assertEqual(tuple(str(key.value) for key in table.rows), ("direct-file",))

    async def test_task_refresh_returns_to_roots_when_open_batch_is_auto_deleted(self) -> None:
        present = True
        batch = TaskNode(
            "batch-1",
            TaskKind.BATCH,
            TaskState.RUNNING,
            "Auto-delete batch",
            TaskProgress(0, 1),
        )
        child = TaskNode(
            "entry-1",
            TaskKind.ITEM,
            TaskState.RUNNING,
            "Gallery",
            TaskProgress(0, 1),
            parent_id="batch-1",
        )

        def list_children(task_id: str) -> tuple[TaskNode, ...]:
            if not present:
                raise TaskNotFoundError(f"task not found: {task_id}")
            return (child,)

        service = SimpleNamespace(
            list_roots=lambda: (batch,) if present else (),
            list_children=list_children,
        )
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))

        async with app.run_test() as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()
            app.query_one("#task-table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app._task_level, "items")

            present = False
            app._refresh_tasks()

            self.assertEqual(app._task_level, "roots")
            self.assertIsNone(app._active_batch_id)
            self.assertIsNone(app._active_task_id)
            self.assertEqual(app.query_one("#task-table", DataTable).row_count, 0)

    async def test_task_refresh_returns_to_roots_when_open_child_is_auto_deleted(self) -> None:
        present = True
        batch = TaskNode(
            "batch-1",
            TaskKind.BATCH,
            TaskState.RUNNING,
            "Auto-delete batch",
            TaskProgress(0, 1),
        )
        child = TaskNode(
            "entry-1",
            TaskKind.ITEM,
            TaskState.RUNNING,
            "Gallery",
            TaskProgress(0, 1),
            parent_id="batch-1",
        )

        def list_files(task_id: str) -> tuple[TaskFileNode, ...]:
            if not present:
                raise TaskNotFoundError(f"task not found: {task_id}")
            return ()

        service = SimpleNamespace(
            list_roots=lambda: (batch,) if present else (),
            list_children=lambda _task_id: (child,),
            list_files=list_files,
        )
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))

        async with app.run_test() as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()
            app.query_one("#task-table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            app._open_task("entry-1")
            self.assertEqual(app._task_level, "files")

            present = False
            app._refresh_tasks()

            self.assertEqual(app._task_level, "roots")
            self.assertIsNone(app._active_batch_id)
            self.assertIsNone(app._active_task_id)
            self.assertEqual(app._task_files, {})
            self.assertEqual(app.query_one("#task-table", DataTable).row_count, 0)

    async def test_task_record_delete_requires_confirmation(self) -> None:
        deleted: list[str] = []
        present = True
        batch = TaskNode(
            "batch-1",
            TaskKind.BATCH,
            TaskState.FAILED,
            "Failed batch",
            TaskProgress(0, 1),
        )

        def delete(task_id):
            nonlocal present
            deleted.append(task_id)
            present = False

        service = SimpleNamespace(
            list_roots=lambda: (batch,) if present else (),
            delete=delete,
        )
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))

        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            tabs.active = "tasks"
            await pilot.pause()
            delete_button = app.query_one("#task-delete", Button)
            self.assertFalse(delete_button.disabled)
            delete_button.press()
            await pilot.pause()
            self.assertEqual(deleted, [])
            self.assertIn(
                "Downloaded files will be kept",
                str(app.query_one("#task-detail", Static).content),
            )
            delete_button.press()
            await pilot.pause()

            self.assertEqual(deleted, ["batch-1"])
            self.assertEqual(app.query_one("#task-table", DataTable).row_count, 0)

    async def test_task_toolbar_routes_lifecycle_actions_through_service(self) -> None:
        calls: list[str] = []
        task = TaskNode(
            "batch-1",
            TaskKind.BATCH,
            TaskState.RUNNING,
            "Active batch",
            TaskProgress(1, 3),
        )
        service = SimpleNamespace(
            list_roots=lambda: (task,),
            pause=lambda task_id: calls.append(f"pause:{task_id}"),
            cancel=lambda task_id: calls.append(f"cancel:{task_id}"),
        )
        app = TextualShellApp(InteractiveShell(task_service_loader=lambda: service))

        async with app.run_test() as pilot:
            app.query_one(TabbedContent).active = "tasks"
            await pilot.pause()
            pause = app.query_one("#task-pause", Button)
            cancel = app.query_one("#task-cancel", Button)
            self.assertFalse(pause.disabled)
            self.assertFalse(cancel.disabled)
            pause.press()
            cancel.press()
            await pilot.pause()

        self.assertEqual(calls, ["pause:batch-1", "cancel:batch-1"])

    async def test_background_download_progress_cancel_and_refresh(self) -> None:
        started = Event()
        calls: list[tuple[str, ...]] = []
        loads = 0

        def list_roots() -> tuple[TaskNode, ...]:
            nonlocal loads
            loads += 1
            return ()

        def run_background(argv, cancellation, progress) -> int:
            calls.append(tuple(argv))
            if argv[0] == "sites":
                return 0
            progress("asset_download", {"current": 1, "total": 2})
            started.set()
            cancellation.wait(2)
            return 130 if cancellation.is_set() else 0

        app = TextualShellApp(
            InteractiveShell(
                background_runner=run_background,
                task_service_loader=lambda: SimpleNamespace(
                    list_roots=list_roots, handle_progress_event=lambda *a: None
                ),
            )
        )

        async with app.run_test() as pilot:
            await pilot.press(*"download example://gallery/welcome", "enter")
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            await pilot.pause()
            self.assertIn(
                "Asset Download",
                str(app.query_one("#operation-status", Static).content),
            )
            await pilot.press(*"sites", "enter")
            await pilot.pause()
            for _ in range(20):
                await pilot.pause()
                if len(calls) == 2:
                    break
            self.assertEqual(calls[1], ("sites",))
            app._cancel_operation()
            for _ in range(20):
                await pilot.pause()
                if not app._background_jobs:
                    break

            self.assertNotIn("--task-id", calls[0])
            self.assertEqual(calls[0][:2], ("download", "example://gallery/welcome"))
            self.assertIn("--json", calls[0])
            self.assertEqual(calls[1], ("sites",))
            self.assertIsNone(app._operation_thread)
            self.assertFalse(app._background_jobs)
            self.assertGreaterEqual(loads, 2)
            self.assertEqual(
                [block.status for block in app._command_blocks],
                ["cancelled", "completed"],
            )
            self.assertIn(
                "Cancelled",
                str(app.query_one("#operation-status", Static).content),
            )
