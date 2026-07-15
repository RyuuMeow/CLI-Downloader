"""Interactive shell construction and startup recovery wiring.

The shell UI remains in ``cli.shell``.  This module only assembles its runtime
services and callbacks so the command parser does not own application startup
state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.application.download_limiter import GalleryDownloadLimiter
from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.cli._download_ops import _build_download_limiter
from cli_downloader.cli._download_ops import _record_list_browse
from cli_downloader.cli._download_ops import _run_selected_list_batch
from cli_downloader.cli._inspect_ops import (
    _dispatch_operation,
    _dispatch_plugin_operation,
    _with_default_strategy,
)
from cli_downloader.cli._output import _EXPLICIT_DOWNLOAD_CONFIRMATIONS
from cli_downloader.cli._output import ProgressSink
from cli_downloader.cli.rendering import RenderMode
from cli_downloader.cli.shell import InteractiveShell
from cli_downloader.config.loader import ConfigLoader
from cli_downloader.core.batch import (
    AssetStatus,
    BatchEntryStatus,
    DownloadChildStatus,
)
from cli_downloader.core.environment import diagnose_environment
from cli_downloader.core.owner_protocol import OwnerLifecycleState, OwnerMode
from cli_downloader.core.owner_scope import (
    OwnerDiscoveryMetadata,
    OwnerLease,
    OwnerManager,
    OwnerScope,
    new_owner_session_id,
)
from cli_downloader.core.errors import OwnerScopeMismatchError
from cli_downloader.core.operations import OperationResult
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.integrations.browser_login import BrowserLoginService
from cli_downloader.plugins.runtime import ApplicationRuntime
from cli_downloader.storage.paths import StorageLayout
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import TaskDesiredControl, TaskStatus
from cli_downloader.about import __version__


def build_interactive_shell(
    *,
    runtime: ApplicationRuntime,
    plugins_dir: Path,
    runner: Callable[[Sequence[str]], int],
    background_runner: Callable[
        [Sequence[str], object, ProgressSink, GalleryDownloadLimiter], int
    ],
    operation_runner: Callable[[str, Mapping[str, object]], object] | None = None,
    owner_lease: OwnerLease | None = None,
) -> int:
    """Claim the Scheduler owner before recovery and run the visible Shell."""

    environment = diagnose_environment()
    scope = OwnerScope.from_app_paths(environment.paths)
    session_id = new_owner_session_id()
    owns_lease = owner_lease is None
    lease = owner_lease
    if lease is None:
        metadata = OwnerDiscoveryMetadata.for_current_process(
            scope,
            application_version=__version__,
            owner_mode=OwnerMode.SHELL,
            lifecycle_state=OwnerLifecycleState.RECOVERING,
            session_id=session_id,
            capabilities=("download", "task_control", "task_query", "history_query", "shell_status"),
        )
        lease = OwnerManager(scope).acquire(metadata)
    elif lease.scope != scope:
        raise OwnerScopeMismatchError("owner lease belongs to a different data-root scope")

    assert lease is not None
    try:
        return _build_interactive_shell_impl(
            runtime=runtime,
            plugins_dir=plugins_dir,
            runner=runner,
            background_runner=background_runner,
            operation_runner=operation_runner,
            owner_lease=lease,
            owner_session_id=session_id,
        )
    finally:
        if owns_lease:
            stopping = OwnerDiscoveryMetadata.for_current_process(
                scope,
                application_version=__version__,
                owner_mode=OwnerMode.SHELL,
                lifecycle_state=OwnerLifecycleState.STOPPING,
                session_id=session_id,
                capabilities=("download", "task_control", "task_query", "history_query", "shell_status"),
            )
            try:
                lease.publish(stopping)
            finally:
                lease.release()


def _build_interactive_shell_impl(
    *,
    runtime: ApplicationRuntime,
    plugins_dir: Path,
    runner: Callable[[Sequence[str]], int],
    background_runner: Callable[
        [Sequence[str], object, ProgressSink, GalleryDownloadLimiter], int
    ],
    operation_runner: Callable[[str, Mapping[str, object]], object] | None = None,
    owner_lease: OwnerLease | None = None,
    owner_session_id: str | None = None,
) -> int:
    """Build and run the interactive shell for an already-loaded runtime."""

    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    shell_config = ConfigLoader(
        paths=environment.paths,
        registry=runtime.settings_registry,
    ).load()
    runtime.configure_plugins(shell_config.values, shell_config.secrets)
    shell_download_limiter = _build_download_limiter(
        runtime,
        shell_config.values,
    )
    task_service: TaskHierarchyService | None = None
    task_store = StateTaskStore(StateDatabase(layout.state_db))
    batch_store = StateBatchStore(StateDatabase(layout.state_db))
    history_store = StateHistoryStore(StateDatabase(layout.state_db))
    startup_commands = _recover_interrupted_downloads(task_store, batch_store, history_store)
    if owner_lease is not None:
        ready_scope = owner_lease.scope
        ready_session = owner_session_id or new_owner_session_id()
        owner_lease.publish(
            OwnerDiscoveryMetadata.for_current_process(
                ready_scope,
                application_version=__version__,
                owner_mode=OwnerMode.SHELL,
                lifecycle_state=OwnerLifecycleState.READY,
                session_id=ready_session,
                capabilities=("download", "task_control", "task_query", "history_query", "shell_status"),
            )
        )
    browser_login = BrowserLoginService(
        profile_root=environment.paths.cache_dir / "browser-auth"
    )

    def validate_browser_session(plugin_id, session):
        plugin = runtime.plugin_for_id(plugin_id)
        validator = getattr(plugin, "validate_auth_session", None)
        return not callable(validator) or bool(validator(session))

    def capture_browser_session(plugin_id, auth):
        return browser_login.capture(
            auth,
            validator=lambda session: validate_browser_session(plugin_id, session),
        )

    def load_task_service() -> TaskHierarchyService:
        nonlocal task_service
        if task_service is None:
            task_service = TaskHierarchyService(
                task_store=task_store,
                batch_store=StateBatchStore(StateDatabase(layout.state_db)),
                history_store=StateHistoryStore(StateDatabase(layout.state_db)),
                scheduler=shell_download_limiter,
            )
        return task_service

    def submit_selected_batch(items, source_url, title, cancellation, progress):
        return _run_selected_list_batch(
            items,
            batch_source_url=source_url,
            batch_title=title,
            runtime=runtime,
            render_mode=RenderMode.JSON,
            profile=None,
            force=False,
            cancellation=cancellation,
            progress=progress,
            task_service=load_task_service(),
            download_limiter=shell_download_limiter,
        )

    def run_background(shell_argv, cancellation, progress):
        return background_runner(
            shell_argv,
            cancellation,
            progress,
            shell_download_limiter,
        )

    operation_callback = operation_runner or (
        lambda url, params: _load_shell_operation(runtime, url, params)
    )

    return InteractiveShell(
        runner=runner,
        background_runner=run_background,
        task_service_loader=load_task_service,
        operation_runner=operation_callback,
        batch_selection_runner=submit_selected_batch,
        settings_registry=runtime.settings_registry,
        config_loader=ConfigLoader(
            paths=environment.paths,
            registry=runtime.settings_registry,
        ),
        browser_auth_runner=capture_browser_session,
        browser_auth_validator=validate_browser_session,
        startup_commands=startup_commands,
        runtime=runtime,
        plugins_dir=plugins_dir,
        cache_dir=environment.paths.cache_dir,
    ).run()


def _load_shell_operation(
    runtime: ApplicationRuntime,
    url: str,
    params: Mapping[str, object],
) -> tuple[str, OperationResult]:
    environment = diagnose_environment()
    config = ConfigLoader(paths=environment.paths, registry=runtime.settings_registry).load()
    runtime.configure_plugins(config.values, config.secrets)

    if url.startswith("plugin://"):
        from urllib.parse import parse_qsl, urlsplit

        parsed = urlsplit(url)
        plugin_id = parsed.hostname
        operation_id = parsed.path.lstrip("/")
        query_params = dict(parse_qsl(parsed.query))

        merged_params = dict(params)
        merged_params.update(query_params)
        merged_params = _with_default_strategy(runtime, url, merged_params, config)

        result = _dispatch_plugin_operation(
            OperationDispatcher(runtime, config=config),
            plugin_id,
            operation_id,
            params=merged_params,
            confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
        )
        return (plugin_id, result)

    entry = runtime.entry_for_url(url)
    result = _dispatch_operation(
        OperationDispatcher(runtime, config=config),
        url,
        params=_with_default_strategy(runtime, url, params, config),
        confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
    )
    _record_list_browse(result, url=url, runtime=runtime)
    return (entry.plugin_id if entry is not None else "unknown", result)


def _recover_interrupted_downloads(
    task_store: StateTaskStore,
    batch_store: StateBatchStore | None = None,
    history_store: object | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Release stale startup leases and rebuild resumable shell commands."""

    commands = []
    for record in sorted(task_store.list(), key=lambda value: (value.created_at, value.task_id)):
        if record.status is TaskStatus.PAUSED and record.active_run_id is not None:
            record = record.transition(active_run_id=None)
            task_store.upsert(record)
        if (
            record.status not in {TaskStatus.PLANNED, TaskStatus.RUNNING}
            or not record.source_url
            or record.metadata.get("scheduler_managed") is not True
        ):
            continue
        if record.status is TaskStatus.PAUSED:
            continue
        if record.status is TaskStatus.RUNNING:
            task_store.upsert(replace(record, status=TaskStatus.PLANNED))
        commands.append(("tasks", "resume", record.task_id, "--json"))

    if batch_store is not None:
        for parent_id in batch_store.list_parent_ids():
            parent_record = task_store.get(parent_id)
            if parent_record is None or parent_record.kind is not TaskKind.BATCH:
                continue
            if parent_record.status is TaskStatus.PAUSED:
                continue
            if parent_record.status in {
                TaskStatus.PENDING,
                TaskStatus.PLANNED,
                TaskStatus.RUNNING,
            }:
                if parent_record.status is TaskStatus.RUNNING:
                    parent_record = parent_record.transition(
                        status=TaskStatus.PLANNED,
                        desired_control=TaskDesiredControl.RUN,
                        active_run_id=None,
                    )
                    task_store.upsert(parent_record)
                for entry_id in batch_store.get_snapshot(parent_id).parent.entry_ids:
                    entry = batch_store.get_entry(entry_id)
                    if entry:
                        if entry.status in {
                            BatchEntryStatus.PENDING_RESOLUTION,
                            BatchEntryStatus.RESOLVING,
                        }:
                            batch_store.update_entry(
                                replace(entry, status=BatchEntryStatus.CANCELLED)
                            )
                        if entry.child_id is not None:
                            child = batch_store.get_child(entry.child_id)
                            if child and child.status in {
                                DownloadChildStatus.QUEUED,
                                DownloadChildStatus.DOWNLOADING,
                            }:
                                for asset in batch_store.list_assets(child.child_id):
                                    if asset.status in {
                                        AssetStatus.QUEUED,
                                        AssetStatus.DOWNLOADING,
                                    }:
                                        batch_store.update_asset(
                                            child.child_id,
                                            replace(asset, status=AssetStatus.CANCELLED),
                                        )
                                batch_store.update_child(
                                    replace(child, status=DownloadChildStatus.CANCELLED)
                                )

                commands.append(("batches", "retry", parent_id))
    return tuple(commands)
