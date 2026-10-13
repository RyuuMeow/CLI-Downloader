"""Download operation wiring used by the CLI entry point.

This module owns generic URL/gallery and Batch runtime assembly around the
application services.  The services themselves remain in
``cli_downloader.application``; keeping this adapter layer separate makes
``cli.app`` responsible for parser and command dispatch only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from threading import Event
from uuid import uuid4

from cli_downloader.application.batch_execution_runner import CoordinatedBatchRunner
from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.batch_resume import BatchResumeService
from cli_downloader.application.batch_retry import BatchRetryService
from cli_downloader.application.batch_service import (
    BatchService,
    RuntimePackageResolver,
    SystemBatchDownloader,
)
from cli_downloader.application.download_service import DownloadService
from cli_downloader.application.download_limiter import GalleryDownloadLimiter, RequestPolicy
from cli_downloader.application.item_scheduler import SchedulerPolicy
from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.application.task_execution_coordinator import TaskExecutionCoordinator
from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.cli._inspect_ops import (
    _dispatch_operation,
    _with_default_strategy,
)
from cli_downloader.cli._output import (
    ProgressSink,
    _EXPLICIT_DOWNLOAD_CONFIRMATIONS,
    _print_generic_download_error,
    _report_progress,
)
from cli_downloader.cli.rendering import (
    RenderMode,
    prepare_result_for_mode,
    serialize_operation_result,
)
from cli_downloader.cli.textual_renderer import render_interactive_result
from cli_downloader.config.loader import ConfigLoader, LoadedConfig
from cli_downloader.core.errors import CLIDownloaderError
from cli_downloader.core.environment import diagnose_environment
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.output import resolve_output_template
from cli_downloader.core.operations import (
    GalleryData,
    ListItemData,
    ListPageData,
    OperationResult,
    ResultKind,
)
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.core.widgets import ActionKind
from cli_downloader.integrations.torrent import QbittorrentClient
from cli_downloader.plugins.runtime import ApplicationRuntime
from cli_downloader.storage.file_writer import AssetFileWriter
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.paths import StorageLayout
from cli_downloader.storage.task_store import TaskDesiredControl, TaskRecord, TaskStatus


AcceptanceSink = Callable[[str, str], None]


class ListSelectionPolicy(str, Enum):
    """Choose how a command-line list result becomes a Batch."""

    ALL = "all"
    EXPLICIT = "explicit"
    INTERACTIVE = "interactive"


def _build_download_limiter(
    runtime: ApplicationRuntime,
    values: Mapping[str, object],
) -> GalleryDownloadLimiter:
    fallback = RequestPolicy(
        max_concurrency=int(values["network.max_concurrent_requests"]),
        min_interval_seconds=float(values["network.min_request_interval_seconds"]),
        max_attempts=int(values["network.max_attempts"]),
        max_backoff_seconds=float(values["network.max_backoff_seconds"]),
    )
    policies: dict[str, RequestPolicy] = {}
    for plugin_id, entry in runtime.plugins_by_id.items():
        network = entry.manifest.network
        if network is None:
            continue
        prefix = f"plugins.{plugin_id}.network."
        policies[entry.site_id] = RequestPolicy(
            max_concurrency=int(values[f"{prefix}max_concurrency"]),
            min_interval_seconds=float(values[f"{prefix}min_interval_seconds"]),
            retryable_status_codes=network.retryable_status_codes,
            max_attempts=int(values[f"{prefix}max_attempts"]),
            max_backoff_seconds=float(values[f"{prefix}max_backoff_seconds"]),
        )
    return GalleryDownloadLimiter(
        int(
            values.get(
                "scheduler.max_concurrent_downloads", values["download.max_concurrent_galleries"]
            )
        ),
        request_policies=policies,
        default_request_policy=fallback,
        item_policy=str(values.get("scheduler.policy", SchedulerPolicy.ROUND_ROBIN.value)),
        item_site_limit=int(
            values.get(
                "scheduler.max_concurrent_downloads_per_site",
                values.get(
                    "scheduler.max_concurrent_downloads",
                    values["download.max_concurrent_galleries"],
                ),
            )
        ),
    )


def _configured_file_writer(
    layout: StorageLayout,
    config: LoadedConfig,
) -> AssetFileWriter:
    return AssetFileWriter(
        layout.downloads_dir,
        folder_template=str(config.values["download.folder_template"]),
    )


def _create_torrent_client(config: LoadedConfig) -> QbittorrentClient | None:
    client_type = config.values.get("torrent.client", "none")
    if client_type != "qbittorrent":
        return None
    url = str(config.values.get("torrent.qbittorrent.url", "http://localhost:8080"))
    username = str(config.values.get("torrent.qbittorrent.username", "admin"))
    password = str(config.secrets.get("torrent.qbittorrent.password", ""))
    poll_interval = int(config.values.get("torrent.poll_interval_seconds", 10))
    return QbittorrentClient(
        url=url,
        username=username,
        password=password,
        poll_interval_seconds=poll_interval,
    )


def _batch_details(store: StateBatchStore, parent_id: str) -> dict[str, object]:
    snapshot = store.get_snapshot(parent_id)
    entries = store.list_entries(parent_id)
    children = store.list_children(parent_id)
    return {
        "batch": snapshot.to_dict(),
        "entries": [entry.to_dict() for entry in entries],
        "children": [
            {
                **child.to_dict(),
                "assets": [record.to_dict() for record in store.list_assets(child.child_id)],
            }
            for child in children
        ],
        "attempts": {
            target_id: [attempt.to_dict() for attempt in store.list_attempts(target_id)]
            for target_id in (
                *(entry.entry_id for entry in entries),
                *(child.child_id for child in children),
            )
            if store.list_attempts(target_id)
        },
    }


def _run_batch_retry(
    parent_id: str,
    *,
    runtime: ApplicationRuntime,
    store: StateBatchStore,
    layout: StorageLayout,
    render_mode: RenderMode,
    config: LoadedConfig,
    profile: str | None,
    cancellation: Event | None = None,
    progress: ProgressSink | None = None,
    resume: bool = False,
    download_limiter: GalleryDownloadLimiter | None = None,
    task_service: TaskHierarchyService | None = None,
) -> int:
    runtime.configure_plugins(config.values, config.secrets)
    entries = store.list_entries(parent_id)
    if not entries:
        print(json.dumps({"error": f"batch parent not found: {parent_id}"}, indent=2))
        return 1
    history_store = StateHistoryStore(StateDatabase(layout.state_db))
    task_store = StateTaskStore(StateDatabase(layout.state_db))
    if task_store.get(parent_id) is None:
        parent = store.get_snapshot(parent_id).parent
        task_store.upsert(
            TaskRecord(
                task_id=parent_id,
                status=TaskStatus.PENDING,
                kind=TaskKind.BATCH,
                title=parent.title,
            )
        )
    completion_store = SQLiteCompletionStore(StateDatabase(layout.state_db))
    download_limiter = download_limiter or _build_download_limiter(runtime, config.values)
    resolver = RuntimePackageResolver(
        runtime,
        profile=profile,
        config_values=config.values,
        request_limiter=download_limiter,
    )
    fetchers = dict(runtime.asset_fetchers_by_site_id)
    pipeline = BatchPipeline(
        store=store,
        resolver=resolver,
        download_limiter=download_limiter,
        downloader=SystemBatchDownloader(
            file_writer=_configured_file_writer(layout, config),
            store=store,
            fetchers=fetchers,
            request_limiter=download_limiter,
            torrent_client=_create_torrent_client(config),
            auto_delete_torrent_file=bool(
                config.values.get("torrent.auto_delete_torrent_file", True)
            ),
        ),
        history_store=history_store,
        completion_store=completion_store,
        event_sink=lambda event: _report_progress(progress, event.reason, event.payload),
    )
    coordinator = TaskExecutionCoordinator(task_store=task_store)
    runner = CoordinatedBatchRunner(pipeline=pipeline, coordinator=coordinator)
    retry_service = BatchRetryService(store=store, resolver=resolver, history_store=history_store)
    resume_service = BatchResumeService(store=store, resolver=resolver)
    if task_service is not None:
        task_service.configure_batch_runtime(
            task_id=parent_id,
            pipeline=pipeline,
            retry_service=retry_service,
            execution_coordinator=coordinator,
        )
    operation = "batch_resume" if resume else "batch_retry"
    _report_progress(
        progress, operation, {"task_id": parent_id, "reason": f"{operation}_requested"}
    )
    try:

        async def run_operation() -> object:
            record = task_store.get(parent_id)
            if resume:
                if record is not None and record.status is TaskStatus.PAUSED:
                    await runner.request_resume(parent_id)
                return await resume_service.resume_parent(parent_id, pipeline=runner)
            if record is not None and record.status in {
                TaskStatus.COMPLETED,
                TaskStatus.PARTIALLY_COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
                TaskStatus.SKIPPED,
            }:
                await runner.request_retry(parent_id)
            return await retry_service.retry_parent(parent_id, pipeline=runner)

        result = asyncio.run(
            _run_cancellable_batch(
                run_operation(),
                pipeline=pipeline,
                parent_id=parent_id,
                cancellation=cancellation,
            )
        )
    except ValueError as error:
        print(json.dumps({"error": str(error)}, indent=2))
        return 1
    except Exception as error:
        print(
            json.dumps(
                {"error_type": type(error).__name__, "error": str(error)},
                indent=2,
            )
        )
        return 1
    finally:
        if task_service is not None:
            task_service.release_batch_runtime(parent_id, pipeline)
    if result.run.task_status is None:
        print(json.dumps({"error": "coordinated batch run did not commit a Task status"}))
        return 1
    if render_mode is RenderMode.JSON:
        payload = result.to_dict()
        payload["status"] = result.run.task_status.value
        payload["cancelled"] = bool(cancellation and cancellation.is_set())
        payload["details"] = _batch_details(store, parent_id)
        print(json.dumps(payload))
    else:
        payload = {
            "status": result.run.task_status.value,
            "cancelled": bool(cancellation and cancellation.is_set()),
        }
        if resume:
            payload["resume_plans"] = len(result.resume.plans)
        else:
            payload["retry_plans"] = len(result.retry.plans)
            payload["attempts"] = len(result.attempts)
        print(json.dumps(payload, indent=2))
    if (
        config.values.get("tasks.auto_delete_on_complete", False)
        and result.run.task_status is TaskStatus.COMPLETED
    ):
        TaskHierarchyService(task_store=task_store, batch_store=store).delete(parent_id)
    if cancellation is not None and cancellation.is_set():
        return 130
    return 0 if result.run.task_status is TaskStatus.COMPLETED else 1


async def _run_cancellable_batch(
    operation,
    *,
    pipeline: BatchPipeline,
    parent_id: str,
    cancellation: Event | None,
):
    if cancellation is None:
        return await operation
    task = asyncio.create_task(operation)
    await asyncio.sleep(0)
    cancel_sent = False
    while not task.done():
        if cancellation.is_set() and not cancel_sent:
            shutdown = getattr(cancellation, "is_owner_shutdown", None)
            if callable(shutdown) and shutdown():
                shutdown_pipeline = getattr(pipeline, "shutdown", None)
                cancel_sent = (
                    shutdown_pipeline(parent_id)
                    if callable(shutdown_pipeline)
                    else pipeline.cancel(parent_id)
                )
            else:
                cancel_sent = pipeline.cancel(parent_id)
        await asyncio.wait((task,), timeout=0.05)
    return await task


def _run_generic_download(
    url: str,
    *,
    runtime: ApplicationRuntime,
    render_mode: RenderMode,
    profile: str | None = None,
    strategy: str | None = None,
    selected_item_ids: tuple[str, ...] = (),
    selection_policy: ListSelectionPolicy = ListSelectionPolicy.INTERACTIVE,
    force: bool = False,
    cancellation: Event | None = None,
    progress: ProgressSink | None = None,
    download_limiter: GalleryDownloadLimiter | None = None,
    task_service: TaskHierarchyService | None = None,
    task_id: str | None = None,
    interactive_renderer=None,
    on_accept: AcceptanceSink | None = None,
) -> int:
    environment = diagnose_environment()
    loaded_config = ConfigLoader(
        paths=environment.paths,
        registry=runtime.settings_registry,
    ).load()
    runtime.configure_plugins(loaded_config.values, loaded_config.secrets)
    download_limiter = download_limiter or _build_download_limiter(
        runtime,
        loaded_config.values,
    )
    entry = runtime.entry_for_url(url)
    dispatcher = OperationDispatcher(
        runtime,
        config=loaded_config,
    )
    plugin_id = entry.plugin_id if entry is not None else "unknown"
    output_mode = RenderMode.PLAIN if render_mode is RenderMode.INTERACTIVE else render_mode
    _report_progress(progress, "operation_dispatch_started", {"url": url})
    operation_params = {"strategy": strategy} if strategy is not None else {}
    operation_params = _with_default_strategy(
        runtime,
        url,
        operation_params,
        loaded_config,
    )
    result = _dispatch_operation(
        dispatcher,
        url,
        params=operation_params or None,
        profile=profile,
        confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
    )
    _report_progress(
        progress,
        "operation_dispatch_finished",
        {"result_kind": result.result_kind.value},
    )
    if cancellation is not None and cancellation.is_set():
        print(json.dumps({"command": "download", "cancelled": True}))
        return 130
    while True:
        if result.result_kind is ResultKind.ERROR:
            print(serialize_operation_result(result, output_mode))
            return 1
        if result.result_kind is ResultKind.GALLERY:
            return _download_generic_gallery(
                result,
                runtime=runtime,
                render_mode=render_mode,
                profile=profile,
                force=force,
                cancellation=cancellation,
                progress=progress,
                download_limiter=download_limiter,
                task_id=task_id,
                task_service=task_service,
                on_accept=on_accept,
            )
        if result.result_kind is not ResultKind.LIST_PAGE:
            print(serialize_operation_result(result, output_mode))
            return 0
        data = result.data
        if not isinstance(data, ListPageData):
            print(serialize_operation_result(result, output_mode))
            return 1
        _record_list_browse(
            result,
            url=url,
            runtime=runtime,
        )
        if selected_item_ids:
            return _run_confirmed_list_batch(
                result,
                selected_item_ids,
                source_url=url,
                runtime=runtime,
                render_mode=render_mode,
                profile=profile,
                force=force,
                cancellation=cancellation,
                progress=progress,
                download_limiter=download_limiter,
                task_service=task_service,
                on_accept=on_accept,
            )
        if selection_policy is ListSelectionPolicy.EXPLICIT:
            return _print_batch_selection_error(
                "At least one gallery must be selected.",
                render_mode,
            )
        if selection_policy is ListSelectionPolicy.ALL:
            enumeration_service: BatchService | None = None
            provisional_parent_id: str | None = None
            if task_id is not None:
                enumeration_service = _state_batch_service()
                provisional_parent_id = task_id
            elif on_accept is not None and entry is not None:
                route = entry.instance.classify_url(url)
                if route is not None:
                    enumeration_service = _state_batch_service()
                    provisional_parent_id = enumeration_service.begin_enumeration(
                        plugin_id=entry.plugin_id,
                        site_id=entry.site_id,
                        source_operation_id=route.operation_id,
                        source_url=url,
                        title=data.title,
                    )
                    on_accept(provisional_parent_id, TaskKind.BATCH.value)
            try:
                items, error = _collect_all_list_items(
                    result,
                    dispatcher=dispatcher,
                    url=url,
                    operation_params=operation_params,
                    profile=profile,
                    cancellation=cancellation,
                )
            except Exception as failure:
                if _owner_shutdown_requested(cancellation):
                    print(json.dumps({"command": "download", "interrupted": True}))
                    return 130
                if enumeration_service is not None and provisional_parent_id is not None:
                    enumeration_service.fail_enumeration(
                        provisional_parent_id,
                        str(failure),
                        cancelled=bool(cancellation is not None and cancellation.is_set()),
                    )
                raise
            if error is not None:
                owner_shutdown = _owner_shutdown_requested(cancellation)
                if (
                    not owner_shutdown
                    and enumeration_service is not None
                    and provisional_parent_id is not None
                ):
                    enumeration_service.fail_enumeration(
                        provisional_parent_id,
                        error,
                        cancelled=bool(cancellation is not None and cancellation.is_set()),
                    )
                result_code = _print_batch_selection_error(error, render_mode)
                return 130 if cancellation is not None and cancellation.is_set() else result_code
            return _run_selected_list_batch(
                items,
                batch_source_url=url,
                batch_title=data.title,
                runtime=runtime,
                render_mode=render_mode,
                profile=profile,
                force=force,
                cancellation=cancellation,
                progress=progress,
                download_limiter=download_limiter,
                task_service=task_service,
                on_accept=None if provisional_parent_id is not None else on_accept,
                provisional_parent_id=provisional_parent_id,
                enumeration_service=enumeration_service,
            )
        if render_mode is not RenderMode.INTERACTIVE:
            print(serialize_operation_result(result, render_mode))
            return 0

        renderer = interactive_renderer or render_interactive_result
        outcome = renderer(result, plugin_id=plugin_id)
        if outcome.cancelled or outcome.back_location is not None or outcome.action is None:
            return 1
        if outcome.action.kind is ActionKind.INVOKE_OPERATION:
            result = _dispatch_operation(
                dispatcher,
                url,
                params=_with_default_strategy(
                    runtime,
                    url,
                    dict(outcome.action.params),
                    loaded_config,
                ),
                profile=profile,
                confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
            )
            continue
        if outcome.action.kind is ActionKind.DOWNLOAD:
            return _run_confirmed_list_batch(
                result,
                outcome.session.selected_item_ids,
                source_url=url,
                runtime=runtime,
                render_mode=render_mode,
                profile=profile,
                force=force,
                cancellation=cancellation,
                progress=progress,
                download_limiter=download_limiter,
                task_service=task_service,
                on_accept=on_accept,
            )
        return 1


def _collect_all_list_items(
    initial_result: OperationResult,
    *,
    dispatcher: OperationDispatcher,
    url: str,
    operation_params: Mapping[str, object],
    profile: str | None,
    cancellation: Event | None,
) -> tuple[tuple[ListItemData, ...], str | None]:
    """Load every page represented by a list result for CLI bulk downloads."""

    initial_data = initial_result.data
    if not isinstance(initial_data, ListPageData):
        return (), "The current result is not a list page."

    page_info = initial_data.page_info
    total_pages = page_info.total_pages
    if total_pages is None and page_info.total_items is not None:
        total_pages = max(
            1,
            (page_info.total_items + page_info.page_size - 1) // page_info.page_size,
        )

    pages: dict[int, ListPageData] = {page_info.current_page: initial_data}
    if total_pages is not None:
        target_pages = range(1, total_pages + 1)
    else:
        # Token-only pagination still uses the core page parameter convention.
        # Start at page 1 so a URL pointing at a later page still means all items.
        target_pages = range(1, 10_001)

    for page_number in target_pages:
        if cancellation is not None and cancellation.is_set():
            return (), "Batch selection was cancelled."
        page = pages.get(page_number)
        if page is None:
            page_result = _dispatch_operation(
                dispatcher,
                url,
                params={**operation_params, "page": page_number},
                profile=profile,
                confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
            )
            page = page_result.data if page_result.result_kind is ResultKind.LIST_PAGE else None
            if not isinstance(page, ListPageData):
                return (), f"Could not load Batch page {page_number}."
            if page.page_info.current_page != page_number:
                return (), f"Batch page {page_number} returned an unexpected page."
            pages[page_number] = page

        if total_pages is None and page.page_info.next_token is None:
            break
    else:
        if total_pages is None:
            return (), "Batch pagination exceeded the 10000-page safety limit."

    items_by_id: dict[str, ListItemData] = {}
    for page_number in sorted(pages):
        for item in pages[page_number].items:
            items_by_id.setdefault(item.item_id, item)
    return tuple(items_by_id.values()), None


def _state_batch_service() -> BatchService:
    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    database = StateDatabase(layout.state_db)
    return BatchService(
        store=StateBatchStore(database),
        task_store=StateTaskStore(database),
        history_store=StateHistoryStore(database),
        completion_store=SQLiteCompletionStore(database),
    )


def _owner_shutdown_requested(cancellation: Event | None) -> bool:
    predicate = getattr(cancellation, "is_owner_shutdown", None)
    return bool(callable(predicate) and predicate())


def _download_generic_gallery(
    result: OperationResult,
    *,
    runtime: ApplicationRuntime,
    render_mode: RenderMode,
    profile: str | None = None,
    force: bool = False,
    cancellation: Event | None = None,
    progress: ProgressSink | None = None,
    download_limiter: GalleryDownloadLimiter | None = None,
    task_id: str | None = None,
    on_accept: AcceptanceSink | None = None,
    task_service: TaskHierarchyService | None = None,
) -> int:
    data = result.data
    if not isinstance(data, GalleryData):
        print(serialize_operation_result(result, RenderMode.PLAIN))
        return 1
    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    loaded_config = ConfigLoader(
        paths=environment.paths,
        registry=runtime.settings_registry,
    ).load()
    runtime_entry = runtime.plugins_by_site_id.get(data.site_id)
    manifest = runtime_entry.manifest if runtime_entry is not None else None
    output_template = resolve_output_template(
        loaded_config.values,
        plugin_id=runtime_entry.plugin_id if runtime_entry is not None else data.site_id,
        plugin_default=(manifest.output.default_folder_template if manifest is not None else None),
        declared_fields=manifest.output.fields if manifest is not None else (),
    )
    progress_sink = progress
    enumeration_progress: list[dict[str, int]] = []
    strategy = DownloadStrategy(str(data.metadata.get("strategy", "auto")))

    def report_progress(current: int, total: int) -> None:
        enumeration_progress.append({"current": current, "total": total})
        _report_progress(
            progress_sink,
            "gallery_enumeration",
            {"current": current, "total": total},
        )
        if render_mode is RenderMode.INTERACTIVE:
            print(
                f"\rEnumerating gallery assets: {current}/{total}",
                end="",
                flush=True,
            )

    registry = runtime.site_registry
    fetcher = runtime.fetcher_for_site_id(data.site_id)
    operation_metadata = {
        "network_request_performed": bool(data.metadata.get("network_request_performed", True))
    }
    task_id = task_id or uuid4().hex
    if on_accept is not None:
        task_store = StateTaskStore(StateDatabase(layout.state_db))
        task_store.upsert(
            TaskRecord(
                task_id=task_id,
                status=TaskStatus.PENDING,
                site_id=data.site_id,
                source_url=data.source_url,
                title=data.title,
                metadata={"owner_acceptance": True, "strategy": strategy.value},
            )
        )
        on_accept(task_id, TaskKind.ITEM.value)
    if fetcher is None:
        print(serialize_operation_result(result, RenderMode.PLAIN))
        return 1
    if task_service is not None:
        task_service.register_standalone_runtime(task_id)
    try:
        completed = asyncio.run(
            DownloadService(
                registry=registry,
                task_store=StateTaskStore(StateDatabase(layout.state_db)),
                history_store=StateHistoryStore(StateDatabase(layout.state_db)),
                completion_store=SQLiteCompletionStore(StateDatabase(layout.state_db)),
                environment=environment,
                output_folder_template=output_template,
                output_field_names=manifest.output.fields if manifest is not None else (),
                auto_delete_completed=loaded_config.values.get(
                    "tasks.auto_delete_on_complete", False
                ),
                torrent_client=_create_torrent_client(loaded_config),
                auto_delete_torrent_file=bool(
                    loaded_config.values.get("torrent.auto_delete_torrent_file", True)
                ),
            ).download_task(
                data.source_url,
                fetcher=fetcher,
                file_writer=_configured_file_writer(
                    layout,
                    loaded_config,
                ),
                profile=profile,
                strategy=strategy,
                record_metadata={
                    "operation_request_id": result.request_id,
                    "scheduler_managed": task_id is not None,
                    **operation_metadata,
                },
                force=force,
                cancel_signal=cancellation,
                progress=lambda current, total: _report_progress(
                    progress_sink,
                    "asset_download",
                    {"current": current, "total": total},
                ),
                byte_progress=lambda asset_id, bytes_so_far, total_bytes: _report_progress(
                    progress_sink,
                    "byte_progress",
                    {
                        "asset_id": asset_id,
                        "bytes_received": bytes_so_far,
                        "total_bytes": total_bytes,
                    },
                ),
                download_limiter=download_limiter,
                task_id=task_id,
            )
        )
    except asyncio.CancelledError:
        print(
            json.dumps(
                {"command": "download", "cancelled": True},
                indent=None if render_mode is RenderMode.JSON else 2,
            )
        )
        return 130
    except KeyboardInterrupt:
        if render_mode is RenderMode.INTERACTIVE:
            print()
        print(
            json.dumps(
                {"command": "download", "cancelled": True},
                indent=None if render_mode is RenderMode.JSON else 2,
            )
        )
        return 130
    except CLIDownloaderError as error:
        if cancellation is not None and cancellation.is_set():
            print(
                json.dumps(
                    {"command": "download", "cancelled": True},
                    indent=None if render_mode is RenderMode.JSON else 2,
                )
            )
            return 130
        if render_mode is RenderMode.INTERACTIVE and enumeration_progress:
            print()
        return _print_generic_download_error(
            data,
            render_mode=render_mode,
            error=error,
            metadata=operation_metadata,
        )
    finally:
        if task_service is not None:
            task_service.release_standalone_runtime(task_id)
    if render_mode is RenderMode.INTERACTIVE and enumeration_progress:
        print()
    print(
        json.dumps(
            {
                "command": "download",
                "render_mode": render_mode.value,
                "operation_result": prepare_result_for_mode(result, render_mode).to_dict(),
                **operation_metadata,
                **completed.to_dict(),
            },
            indent=None if render_mode is RenderMode.JSON else 2,
        )
    )
    return 0


def _record_list_browse(
    result: OperationResult,
    *,
    url: str,
    runtime: ApplicationRuntime,
) -> None:
    data = result.data
    entry = runtime.entry_for_url(url)
    if not isinstance(data, ListPageData) or entry is None:
        return
    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    BatchService(
        history_store=StateHistoryStore(StateDatabase(layout.state_db)),
    ).record_browse(
        request_id=result.request_id,
        plugin_id=entry.plugin_id,
        site_id=entry.site_id,
        source_url=url,
        page=data,
    )


def _run_confirmed_list_batch(
    result: OperationResult,
    selected_item_ids: Sequence[str],
    *,
    source_url: str,
    runtime: ApplicationRuntime,
    render_mode: RenderMode,
    profile: str | None,
    force: bool,
    cancellation: Event | None,
    progress: ProgressSink | None,
    download_limiter: GalleryDownloadLimiter | None = None,
    task_service: TaskHierarchyService | None = None,
    on_accept: AcceptanceSink | None = None,
) -> int:
    data = result.data
    if not isinstance(data, ListPageData):
        return _print_batch_selection_error("The current result is not a list page.", render_mode)
    selected = set(selected_item_ids)
    items = tuple(item for item in data.items if item.item_id in selected)
    if not selected or len(items) != len(selected):
        return _print_batch_selection_error(
            "Every selected gallery must exist on the current page.",
            render_mode,
        )

    return _run_selected_list_batch(
        items,
        batch_source_url=source_url,
        batch_title=data.title,
        runtime=runtime,
        render_mode=render_mode,
        profile=profile,
        force=force,
        cancellation=cancellation,
        progress=progress,
        download_limiter=download_limiter,
        task_service=task_service,
        on_accept=on_accept,
    )


def _run_selected_list_batch(
    items: Sequence[ListItemData],
    *,
    batch_source_url: str | None = None,
    batch_title: str | None = None,
    runtime: ApplicationRuntime,
    render_mode: RenderMode,
    profile: str | None,
    force: bool,
    cancellation: Event | None,
    progress: ProgressSink | None,
    task_service: TaskHierarchyService | None = None,
    download_limiter: GalleryDownloadLimiter | None = None,
    on_accept: AcceptanceSink | None = None,
    provisional_parent_id: str | None = None,
    enumeration_service: BatchService | None = None,
) -> int:
    items = tuple(items)

    def selection_error(message: str) -> int:
        if provisional_parent_id is not None and enumeration_service is not None:
            enumeration_service.fail_enumeration(provisional_parent_id, message)
        return _print_batch_selection_error(message, render_mode)

    if not items:
        return selection_error("At least one gallery must be selected.")

    routes = []
    for item in items:
        runtime_entry = runtime.entry_for_url(item.source_url)
        route = (
            runtime_entry.instance.classify_url(item.source_url)
            if runtime_entry is not None
            else None
        )
        if runtime_entry is None or route is None:
            return selection_error(f"Selected gallery is not routable: {item.item_id}")
        routes.append((runtime_entry, route))
    first_entry, first_route = routes[0]
    if any(
        entry.plugin_id != first_entry.plugin_id
        or entry.site_id != first_entry.site_id
        or route.operation_id != first_route.operation_id
        for entry, route in routes[1:]
    ):
        return selection_error("Selected galleries must use the same plugin operation.")

    strategy_result = _selection_strategy(items)
    if strategy_result is None:
        return selection_error("Selected galleries do not have a consistent download strategy.")
    strategy = strategy_result

    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    store = StateBatchStore(StateDatabase(layout.state_db))
    history_store = StateHistoryStore(StateDatabase(layout.state_db))
    completion_store = SQLiteCompletionStore(StateDatabase(layout.state_db))
    task_store = StateTaskStore(StateDatabase(layout.state_db))
    creation = BatchService(
        store=store,
        task_store=task_store,
        history_store=history_store,
        completion_store=completion_store,
    ).confirm_selection(
        plugin_id=first_entry.plugin_id,
        site_id=first_entry.site_id,
        source_operation_id=first_route.operation_id,
        items=items,
        source_url=batch_source_url,
        title=batch_title,
        strategy=strategy,
        force=force,
        parent_id=provisional_parent_id,
    )
    parent_id = creation.snapshot.parent.parent_id
    current_task = task_store.get(parent_id)
    if current_task is not None and (
        current_task.status is TaskStatus.PAUSED
        or current_task.desired_control is TaskDesiredControl.PAUSE
    ):
        _report_progress(progress, "batch_created", {"parent_id": parent_id})
        print(
            json.dumps(
                {
                    "command": "download",
                    "render_mode": render_mode.value,
                    "status": TaskStatus.PAUSED.value,
                    "site_id": first_entry.site_id,
                    "asset_count": 0,
                    "batch_creation": creation.to_dict(),
                    "batch": creation.snapshot.to_dict(),
                    "duplicates": {
                        candidate_id: kind.value
                        for candidate_id, kind in creation.duplicates.items()
                    },
                    "output_files": [],
                    "network_request_performed": bool(
                        items[0].metadata.get("network_request_performed", True)
                    ),
                    "cancelled": False,
                },
                indent=None if render_mode is RenderMode.JSON else 2,
            )
        )
        return 0
    if current_task is not None and (
        current_task.status is TaskStatus.CANCELED
        or current_task.desired_control is TaskDesiredControl.CANCEL
    ):
        print(json.dumps({"command": "download", "cancelled": True, "task_id": parent_id}))
        return 130
    loaded_config = ConfigLoader(paths=environment.paths, registry=runtime.settings_registry).load()
    runtime.configure_plugins(loaded_config.values, loaded_config.secrets)
    download_limiter = download_limiter or _build_download_limiter(runtime, loaded_config.values)
    fetchers = dict(runtime.asset_fetchers_by_site_id)
    override_instances = {}
    resolver = RuntimePackageResolver(
        runtime,
        profile=profile,
        override_instances=override_instances,
        config_values=loaded_config.values,
        request_limiter=download_limiter,
    )
    pipeline = BatchPipeline(
        store=store,
        resolver=resolver,
        download_limiter=download_limiter,
        downloader=SystemBatchDownloader(
            file_writer=_configured_file_writer(
                layout,
                loaded_config,
            ),
            store=store,
            fetchers=fetchers,
            request_limiter=download_limiter,
            torrent_client=_create_torrent_client(loaded_config),
            auto_delete_torrent_file=bool(
                loaded_config.values.get("torrent.auto_delete_torrent_file", True)
            ),
        ),
        history_store=history_store,
        completion_store=completion_store,
        event_sink=lambda event: _report_progress(progress, event.reason, event.payload),
    )
    coordinator = TaskExecutionCoordinator(task_store=task_store)
    runner = CoordinatedBatchRunner(pipeline=pipeline, coordinator=coordinator)
    if task_service is not None:
        task_service.configure_batch_runtime(
            task_id=parent_id,
            pipeline=pipeline,
            retry_service=BatchRetryService(
                store=store,
                resolver=resolver,
                history_store=history_store,
            ),
            execution_coordinator=coordinator,
        )
    try:
        if on_accept is not None:
            on_accept(parent_id, TaskKind.BATCH.value)
        _report_progress(progress, "batch_created", {"parent_id": parent_id})
        run = asyncio.run(
            _run_cancellable_batch(
                runner.run_batch(parent_id),
                pipeline=pipeline,
                parent_id=parent_id,
                cancellation=cancellation,
            )
        )
    except KeyboardInterrupt:
        print(json.dumps({"command": "download", "cancelled": True}))
        return 130
    finally:
        if task_service is not None:
            task_service.release_batch_runtime(parent_id, pipeline)

    output_files = [
        asset.final_path
        for child in store.list_children(creation.snapshot.parent.parent_id)
        for asset in store.list_assets(child.child_id)
        if asset.final_path is not None
    ]
    if run.task_status is None:
        raise RuntimeError("coordinated batch run did not commit a Task status")
    payload = {
        "command": "download",
        "render_mode": render_mode.value,
        "status": run.task_status.value,
        "site_id": first_entry.site_id,
        "asset_count": len(output_files),
        "batch_creation": creation.to_dict(),
        "batch": run.snapshot.to_dict(),
        "duplicates": {
            candidate_id: kind.value for candidate_id, kind in creation.duplicates.items()
        },
        "output_files": output_files,
        "network_request_performed": bool(items[0].metadata.get("network_request_performed", True)),
        "cancelled": bool(cancellation and cancellation.is_set()),
    }
    print(json.dumps(payload, indent=None if render_mode is RenderMode.JSON else 2))

    if (
        loaded_config.values.get("tasks.auto_delete_on_complete", False)
        and run.task_status is TaskStatus.COMPLETED
    ):
        parent_id = creation.snapshot.parent.parent_id
        if task_service is not None:
            task_service.delete(parent_id)
        else:
            TaskHierarchyService(task_store=task_store, batch_store=store).delete(parent_id)

    if cancellation is not None and cancellation.is_set():
        return 130
    return 0 if run.task_status is TaskStatus.COMPLETED else 1


def _selection_strategy(items: Sequence[ListItemData]) -> DownloadStrategy | None:
    values = {item.metadata.get("strategy", DownloadStrategy.AUTO.value) for item in items}
    if len(values) != 1:
        return None
    value = values.pop()
    if not isinstance(value, str):
        return None
    try:
        return DownloadStrategy(value)
    except ValueError:
        return None


def _print_batch_selection_error(message: str, render_mode: RenderMode) -> int:
    print(
        json.dumps(
            {"error_type": "InvalidBatchSelection", "error": message},
            indent=None if render_mode is RenderMode.JSON else 2,
        )
    )
    return 1
