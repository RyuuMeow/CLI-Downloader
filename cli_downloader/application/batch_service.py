from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from urllib.request import Request, urlopen
from uuid import uuid4

from cli_downloader.application.batch_pipeline import (
    BatchControlSignal,
    BatchDownloadExecutor,
    BatchPackageResolver,
    ProgressReporter,
)
from cli_downloader.application.download_limiter import GalleryDownloadLimiter
from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    BatchSnapshot,
    DownloadAsset,
    DownloadChild,
    DownloadPackage,
    DuplicateKind,
    FailureData,
    FailureOrigin,
    FailureStage,
    RetryPlan,
    derive_batch_snapshot,
)
from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.core.fetch import AssetFetcher
from cli_downloader.core.models import Asset, AssetKind, DownloadStrategy, ResolveRequest
from cli_downloader.integrations.torrent import (
    TorrentAsset,
    TorrentClient,
    TorrentClientError,
    TorrentHandle,
    TorrentOutcome,
    poll_supports_argument,
    reconcile_torrent_checkpoint,
)
from cli_downloader.core.operations import ListItemData, ListPageData
from cli_downloader.core.output import output_folder_fields, resolve_output_template
from cli_downloader.plugins.runtime import ApplicationRuntime
from cli_downloader.storage.completion_store import CompletionStore
from cli_downloader.storage.file_writer import AssetFileWriter, WrittenAsset
from cli_downloader.storage.history_store import HistoryStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus, TaskStore
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.core.task_tree import TaskKind


@dataclass(frozen=True)
class ConfirmedBatchResult:
    snapshot: BatchSnapshot
    entries: tuple[BatchEntry, ...]
    duplicates: Mapping[str, DuplicateKind]
    forced: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "duplicates", MappingProxyType(dict(self.duplicates)))

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "entries": [entry.to_dict() for entry in self.entries],
            "duplicates": {
                candidate_id: kind.value for candidate_id, kind in self.duplicates.items()
            },
            "forced": self.forced,
        }


def _uses_shared_state_stores(
    store: object,
    task_store: object,
    history_store: object,
) -> bool:
    return (
        isinstance(store, StateBatchStore)
        and isinstance(task_store, StateTaskStore)
        and isinstance(history_store, StateHistoryStore)
        and store.database.path == task_store.database.path == history_store.database.path
    )


class BatchService:
    def __init__(
        self,
        *,
        store: StateBatchStore | None = None,
        task_store: TaskStore | None = None,
        history_store: HistoryStore | None = None,
        completion_store: CompletionStore | None = None,
    ) -> None:
        self._store = store
        self._task_store = task_store
        self._history_store = history_store
        self._completion_store = completion_store

    def record_browse(
        self,
        *,
        request_id: str,
        plugin_id: str,
        site_id: str,
        source_url: str,
        page: ListPageData,
    ) -> None:
        if self._history_store is None:
            return
        self._history_store.append(
            TaskEvent(
                task_id=request_id,
                name="browse_history",
                reason="list_page_viewed",
                payload={
                    "plugin_id": plugin_id,
                    "site_id": site_id,
                    "source_url": source_url,
                    "item_count": len(page.items),
                    "page": page.page_info.current_page,
                },
            )
        )

    def confirm_selection(
        self,
        *,
        plugin_id: str,
        site_id: str,
        source_operation_id: str,
        items: Sequence[ListItemData],
        source_url: str | None = None,
        title: str | None = None,
        strategy: DownloadStrategy = DownloadStrategy.AUTO,
        force: bool = False,
        parent_id: str | None = None,
    ) -> ConfirmedBatchResult:
        selected = tuple(items)
        if self._store is None:
            raise RuntimeError("batch storage is required to confirm a selection")
        if self._task_store is None:
            raise RuntimeError("task storage is required to confirm a selection")
        if not selected:
            raise ValueError("at least one gallery must be selected")
        candidate_ids = tuple(item.item_id for item in selected)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("selected gallery ids must be unique")
        duplicates = dict(self._store.find_duplicates(site_id, candidate_ids))
        if not force and self._completion_store is not None:
            for item in selected:
                if self._completion_store.is_completed(site_id, item.source_url):
                    duplicates[item.item_id] = DuplicateKind.COMPLETED
        parent_id = parent_id or uuid4().hex

        def _is_duplicate(item: ListItemData) -> bool:
            if force:
                return False
            if item.item_id in duplicates:
                return True
            return False

        entries = tuple(
            BatchEntry(
                entry_id=uuid4().hex,
                parent_id=parent_id,
                plugin_id=plugin_id,
                site_id=site_id,
                candidate_id=item.item_id,
                source_operation_id=source_operation_id,
                source_url=item.source_url,
                title=item.title,
                strategy=strategy,
                status=(
                    BatchEntryStatus.DUPLICATE_SKIPPED
                    if _is_duplicate(item)
                    else BatchEntryStatus.PENDING_RESOLUTION
                ),
            )
            for item in selected
        )
        parent = BatchParent(
            parent_id=parent_id,
            entry_ids=tuple(entry.entry_id for entry in entries),
            title=title or parent_id[:8],
        )
        task_record = TaskRecord(
            task_id=parent_id,
            status=TaskStatus.PENDING,
            kind=TaskKind.BATCH,
            site_id=site_id,
            source_url=source_url,
            title=parent.title,
            metadata={
                "batch_selection": {
                    "plugin_id": plugin_id,
                    "site_id": site_id,
                    "source_operation_id": source_operation_id,
                    "strategy": strategy.value,
                    "items": [
                        {
                            "candidate_id": item.item_id,
                            "source_url": item.source_url,
                            "title": item.title,
                        }
                        for item in selected
                    ],
                },
                "scheduler": {
                    "root_id": parent_id,
                    "enqueue_sequence": 0,
                    "site_id": site_id,
                    "entry_order": {
                        entry.entry_id: sequence for sequence, entry in enumerate(entries)
                    },
                },
            },
        )
        parent_history_record = DownloadHistoryRecord(
            record_id=parent_id,
            task_id=parent_id,
            kind=HistoryKind.BATCH,
            plugin_id=plugin_id,
            site_id=site_id,
            title=parent.title,
            source_url=source_url,
            status=TaskStatus.PENDING.value,
        )
        provisional_snapshot = derive_batch_snapshot(parent, entries, ())
        events_to_append = tuple(
            [
                TaskEvent(
                    task_id=parent_id,
                    name="batch_created",
                    reason="selection_confirmed",
                    payload={
                        "site_id": site_id,
                        "title": parent.title,
                        "source_url": source_url,
                        "strategy": strategy.value,
                        "selected": len(entries),
                        "skipped": provisional_snapshot.counts.skipped,
                        "forced": force,
                        "entry_ids": list(parent.entry_ids),
                    },
                ),
                *[
                    TaskEvent(
                        task_id=entry.entry_id,
                        name="task_persisted",
                        reason="batch_entry_persisted",
                        payload={
                            "site_id": entry.site_id,
                            "title": entry.title or entry.candidate_id,
                            "source_url": entry.source_url,
                            "status": entry.status.value,
                            "batch_id": parent_id,
                        },
                    )
                    for entry in entries
                ],
            ]
        )
        if _uses_shared_state_stores(self._store, self._task_store, self._history_store):
            snapshot = self._store.create_task_and_batch(
                task_record,
                parent,
                entries,
                events_to_append,
                history_records=(parent_history_record,),
            )
        else:
            self._task_store.upsert(task_record)
            try:
                self._store.create_batch(parent, entries)
            except Exception:
                self._task_store.delete(task_record.task_id)
                raise
            snapshot = self._store.get_snapshot(parent_id)
            if self._history_store is not None:
                self._history_store.append_many(events_to_append)
                if isinstance(self._history_store, StateHistoryStore):
                    self._history_store.upsert_record(parent_history_record)
        return ConfirmedBatchResult(snapshot, entries, duplicates, force)


class RuntimePackageResolver(BatchPackageResolver):
    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        profile: str | None = None,
        override_instances: Mapping[str, object] | None = None,
        config_values: Mapping[str, object] | None = None,
        request_limiter: GalleryDownloadLimiter | None = None,
    ) -> None:
        self._runtime = runtime
        self._profile = profile
        self._override_instances = dict(override_instances or {})
        self._config_values = {
            **runtime.settings_registry.schema.defaults(),
            **(config_values or {}),
        }
        self._request_limiter = request_limiter

    async def resolve(
        self,
        entry: BatchEntry,
        cancel_event: BatchControlSignal,
        progress: ProgressReporter,
    ) -> DownloadPackage:
        runtime_plugin = self._runtime.plugins_by_id.get(entry.plugin_id)
        if runtime_plugin is None or runtime_plugin.site_id != entry.site_id:
            raise ValueError(f"batch plugin is unavailable: {entry.plugin_id}")
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await _wait_if_paused(cancel_event)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        instance = self._override_instances.get(entry.plugin_id, runtime_plugin.instance)

        async def resolve_item():
            return await instance.resolve(
                ResolveRequest(
                    url=entry.source_url,
                    profile=self._profile,
                    strategy=entry.strategy,
                )
            )

        item = (
            await self._request_limiter.request(
                entry.site_id,
                resolve_item,
                report_retry=lambda payload: progress("request_rate_limited", payload),
            )
            if self._request_limiter is not None
            else await resolve_item()
        )
        progress("gallery_resolved", {"candidate_id": entry.candidate_id})
        await _wait_if_paused(cancel_event)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        plan = await instance.choose_strategy(item)
        gallery_id = _gallery_id(item.metadata, entry.candidate_id)
        assets = tuple(
            DownloadAsset(
                site_id=entry.site_id,
                gallery_id=gallery_id,
                plugin_asset_key=_asset_key(asset, index),
                url=asset.url,
                filename=asset.filename,
                index=index,
                metadata={**asset.metadata, "asset_kind": asset.kind.value},
            )
            for index, asset in enumerate(plan.assets.assets, start=1)
        )
        progress("package_built", {"asset_count": len(assets)})
        manifest = runtime_plugin.manifest
        folder_template = resolve_output_template(
            self._config_values,
            plugin_id=entry.plugin_id,
            plugin_default=manifest.output.default_folder_template,
            declared_fields=manifest.output.fields,
        )
        return DownloadPackage(
            package_id=uuid4().hex,
            version=1,
            plugin_id=entry.plugin_id,
            site_id=entry.site_id,
            gallery_id=gallery_id,
            source_operation_id=entry.source_operation_id,
            source_url=entry.source_url,
            strategy=plan.strategy,
            assets=assets,
            title=entry.title or item.title,
            folder_template=folder_template,
            folder_fields=output_folder_fields(
                site_id=item.site_id,
                gallery_id=gallery_id,
                title=entry.title or item.title,
                metadata=item.metadata,
                declared_fields=manifest.output.fields,
            ),
        )

    async def retry(
        self,
        entry: BatchEntry,
        plan: RetryPlan,
        cancel_event: BatchControlSignal,
        progress: ProgressReporter,
    ) -> DownloadPackage:
        progress(
            "retry_checkpoint",
            {"plan_id": plan.plan_id, "start_at": plan.start_at.value},
        )
        return await self.resolve(entry, cancel_event, progress)


class SystemBatchDownloader(BatchDownloadExecutor):
    def __init__(
        self,
        *,
        file_writer: AssetFileWriter,
        store: StateBatchStore | None = None,
        fetchers: Mapping[str, AssetFetcher] | None = None,
        request_limiter: GalleryDownloadLimiter | None = None,
        torrent_client: TorrentClient | None = None,
        auto_delete_torrent_file: bool = True,
    ) -> None:
        self._file_writer = file_writer
        self._store = store
        self._fetchers = dict(fetchers or {})
        self._request_limiter = request_limiter
        self._torrent_client = torrent_client
        self._auto_delete_torrent_file = auto_delete_torrent_file

    async def execute(
        self,
        child: DownloadChild,
        assets: tuple[AssetRecord, ...],
        cancel_event: BatchControlSignal,
        progress: ProgressReporter,
    ) -> tuple[AssetRecord, ...]:
        if child.package.strategy == DownloadStrategy.TORRENT:
            return await self._execute_torrent(child, assets, cancel_event, progress)
        outcomes: list[AssetRecord] = []

        def append_outcome(outcome: AssetRecord) -> None:
            outcomes.append(outcome)
            if self._store is not None:
                self._store.update_asset(child.child_id, outcome)

        for record in assets:
            wait_if_paused = getattr(cancel_event, "wait_if_paused", None)
            if callable(wait_if_paused):
                await wait_if_paused()
            if cancel_event.is_set():
                append_outcome(replace(record, status=AssetStatus.CANCELLED))
                continue
            legacy_asset = _legacy_asset(record.asset)
            resumed = None
            if record.partial_path is not None and record.content_sha256 is not None:
                resumed = self._file_writer.resume(
                    _legacy_asset(record.asset, include_plugin_asset_key=False),
                    task_id=child.child_id,
                    partial_path=record.partial_path,
                    content_sha256=record.content_sha256,
                    folder_fields=_package_folder_fields(child.package),
                    folder_template=child.package.folder_template,
                )
                if resumed is None:
                    resumed = self._file_writer.resume(
                        legacy_asset,
                        task_id=child.child_id,
                        partial_path=record.partial_path,
                        content_sha256=record.content_sha256,
                        folder_fields=_package_folder_fields(child.package),
                        folder_template=child.package.folder_template,
                    )
            if resumed is not None:
                append_outcome(
                    replace(
                        record,
                        status=AssetStatus.COMPLETED,
                        failure=None,
                        partial_path=None,
                        final_path=str(resumed.path),
                    )
                )
                progress(
                    "asset_resumed",
                    {"asset_id": record.asset.asset_id, "path": str(resumed.path)},
                )
                continue
            fetcher = self._fetchers.get(record.asset.site_id)
            if hasattr(fetcher, "fetch_stream"):
                hash_obj = sha256()

                class FetchErrorWrap(Exception):
                    pass

                async def _chunk_generator():
                    import time

                    bytes_so_far = 0
                    last_report_time = 0.0
                    try:
                        async for chunk, total in fetcher.fetch_stream(legacy_asset):
                            if callable(wait_if_paused):
                                await wait_if_paused()
                            hash_obj.update(chunk)
                            bytes_so_far += len(chunk)
                            now = time.monotonic()
                            if (
                                total is None
                                or bytes_so_far == total
                                or now - last_report_time > 0.1
                            ):
                                progress(
                                    "byte_progress",
                                    {
                                        "asset_id": record.asset.asset_id,
                                        "bytes_received": bytes_so_far,
                                        "total_bytes": total,
                                    },
                                )
                                last_report_time = now
                            yield chunk
                    except Exception as e:
                        raise FetchErrorWrap() from e

                async def fetch_and_write() -> WrittenAsset:
                    return await self._file_writer.write_stream(
                        legacy_asset,
                        _chunk_generator(),
                        task_id=child.child_id,
                        folder_fields=_package_folder_fields(child.package),
                        folder_template=child.package.folder_template,
                    )

                try:
                    written = (
                        await self._request_limiter.request(
                            record.asset.site_id,
                            fetch_and_write,
                            report_retry=lambda payload: progress("request_rate_limited", payload),
                        )
                        if self._request_limiter is not None
                        else await fetch_and_write()
                    )
                    digest = hash_obj.hexdigest()
                except Exception as error:
                    _target, partial = self._file_writer.paths(
                        legacy_asset,
                        task_id=child.child_id,
                        folder_fields=_package_folder_fields(child.package),
                        folder_template=child.package.folder_template,
                    )
                    is_fetch_error = isinstance(error, FetchErrorWrap) or isinstance(
                        error.__cause__, FetchErrorWrap
                    )
                    actual_error = error.__cause__ if is_fetch_error and error.__cause__ else error
                    stage = FailureStage.FETCH if is_fetch_error else FailureStage.WRITE

                    append_outcome(
                        replace(
                            _failed_record(record, actual_error, stage),
                            partial_path=str(partial) if partial.exists() else None,
                            content_sha256=hash_obj.hexdigest(),
                        )
                    )
                    continue
            else:
                try:

                    async def fetch_asset() -> bytes:
                        return await self._fetch(record.asset)

                    content = (
                        await self._request_limiter.request(
                            record.asset.site_id,
                            fetch_asset,
                            report_retry=lambda payload: progress("request_rate_limited", payload),
                        )
                        if self._request_limiter is not None
                        else await fetch_asset()
                    )
                except Exception as error:
                    append_outcome(_failed_record(record, error, FailureStage.FETCH))
                    continue
                digest = sha256(content).hexdigest()
                try:
                    written = self._file_writer.write(
                        legacy_asset,
                        content,
                        task_id=child.child_id,
                        folder_fields=_package_folder_fields(child.package),
                        folder_template=child.package.folder_template,
                    )
                except Exception as error:
                    _target, partial = self._file_writer.paths(
                        legacy_asset,
                        task_id=child.child_id,
                        folder_fields=_package_folder_fields(child.package),
                        folder_template=child.package.folder_template,
                    )
                    append_outcome(
                        replace(
                            _failed_record(record, error, FailureStage.WRITE),
                            partial_path=str(partial) if partial.exists() else None,
                            content_sha256=digest,
                        )
                    )
                    continue

            append_outcome(
                replace(
                    record,
                    status=AssetStatus.COMPLETED,
                    partial_path=None,
                    final_path=str(written.path),
                    content_sha256=digest,
                )
            )
            progress(
                "asset_written",
                {"asset_id": record.asset.asset_id, "path": str(written.path)},
            )
        return tuple(outcomes)

    async def _fetch(self, asset: DownloadAsset) -> bytes:
        fetcher = self._fetchers.get(asset.site_id)
        if fetcher is not None:
            return await fetcher.fetch(_legacy_asset(asset))

        def fetch_url() -> bytes:
            request = Request(asset.url, headers=dict(asset.headers))
            with urlopen(request) as response:
                return response.read()

        return await asyncio.to_thread(fetch_url)

    async def _execute_torrent(
        self,
        child: DownloadChild,
        assets: tuple[AssetRecord, ...],
        cancel_event: BatchControlSignal,
        progress: ProgressReporter,
    ) -> tuple[AssetRecord, ...]:
        if self._torrent_client is None:
            raise TorrentClientError(
                "Torrent download requested but no torrent client is configured."
            )

        folder_fields = _package_folder_fields(child.package)
        folder_template = child.package.folder_template

        # Compute download directory from a dummy asset path.
        dummy = Asset(url="dummy://.torrent", kind=AssetKind.TORRENT, filename="dummy")
        target_path, _ = self._file_writer.paths(
            dummy,
            task_id=child.child_id,
            folder_fields=folder_fields,
            folder_template=folder_template,
        )
        download_dir = target_path.parent
        download_dir.mkdir(parents=True, exist_ok=True)

        outcomes: list[AssetRecord] = []
        pending: list[tuple[int, TorrentHandle, AssetRecord]] = []

        # Phase 1: download .torrent files and add to client.
        for record in assets:
            await _wait_if_paused(cancel_event)
            if cancel_event.is_set():
                outcomes.append(replace(record, status=AssetStatus.CANCELLED))
                continue

            checkpoint = record.metadata.get("torrent_checkpoint")
            reconciliation = (
                await reconcile_torrent_checkpoint(self._torrent_client, checkpoint)
                if isinstance(checkpoint, Mapping)
                else None
            )
            if reconciliation is not None and reconciliation.action == "error":
                outcome = reconciliation.outcome
                error = TorrentClientError(
                    (outcome.error if outcome is not None else None)
                    or "Could not reconcile persisted torrent with client"
                )
                outcomes.append(
                    replace(
                        record,
                        status=AssetStatus.FAILED,
                        failure=FailureData(
                            origin=FailureOrigin.SYSTEM,
                            stage=FailureStage.FETCH,
                            code="torrent_reconcile_failed",
                            message=str(error),
                            retryable=True,
                            details={"error_type": type(error).__name__},
                        ),
                    )
                )
                continue

            handle: TorrentHandle
            written: WrittenAsset | None = None
            if reconciliation is not None and reconciliation.action in {
                "completed",
                "externally_completed",
                "resume",
            }:
                handle = reconciliation.handle
                if handle is None:
                    outcomes.append(
                        replace(
                            record,
                            status=AssetStatus.FAILED,
                            failure=FailureData(
                                origin=FailureOrigin.SYSTEM,
                                stage=FailureStage.FETCH,
                                code="torrent_reconcile_failed",
                                message="Persisted torrent checkpoint has no client handle",
                                retryable=True,
                                details={},
                            ),
                        )
                    )
                    continue
                descriptor_path = checkpoint.get("descriptor_path") if isinstance(checkpoint, Mapping) else None
                if not isinstance(descriptor_path, str) or not descriptor_path:
                    descriptor_path = record.final_path or str(
                        self._file_writer.paths(
                            _legacy_asset(record.asset),
                            task_id=child.child_id,
                            folder_fields=folder_fields,
                            folder_template=folder_template,
                        )[0]
                    )
                written = WrittenAsset(
                    asset_url=record.asset.url,
                    path=Path(descriptor_path),
                    bytes_written=0,
                )
                if reconciliation.outcome is not None:
                    record = replace(
                        record,
                        metadata={
                            **record.metadata,
                            "torrent_checkpoint": _torrent_checkpoint_from_outcome(
                                record.metadata.get("torrent_checkpoint"),
                                reconciliation.outcome,
                            ),
                        },
                    )
                if reconciliation.action in {"completed", "externally_completed"}:
                    if self._store is not None and record.status is AssetStatus.QUEUED:
                        self._store.update_asset(
                            child.child_id,
                            replace(record, status=AssetStatus.DOWNLOADING),
                        )
                    completed_record = replace(record, status=AssetStatus.COMPLETED)
                    outcomes.append(completed_record)
                    if self._store is not None:
                        self._store.update_asset(child.child_id, completed_record)
                    continue
            else:
                try:
                    content = await self._fetch(record.asset)
                except Exception as error:
                    outcomes.append(_failed_record(record, error, FailureStage.FETCH))
                    continue

                digest = sha256(content).hexdigest()

                # Write .torrent to file so it appears in output_files.
                try:
                    legacy = _legacy_asset(record.asset, include_plugin_asset_key=False)
                    written = self._file_writer.write(
                        legacy,
                        content,
                        task_id=child.child_id,
                        folder_fields=folder_fields,
                        folder_template=folder_template,
                    )
                except Exception as error:
                    outcomes.append(_failed_record(record, error, FailureStage.WRITE))
                    continue

                torrent_asset = TorrentAsset(
                    name=record.asset.filename or "download.torrent",
                    torrent_data=content,
                    torrent_url=record.asset.url,
                    index=record.asset.index,
                )
                try:
                    handle = await self._torrent_client.add_torrent(torrent_asset, download_dir)
                except Exception as error:
                    outcomes.append(
                        replace(
                            record,
                            status=AssetStatus.FAILED,
                            final_path=str(written.path),
                            content_sha256=digest,
                            failure=FailureData(
                                origin=FailureOrigin.SYSTEM,
                                stage=FailureStage.FETCH,
                                code="torrent_add_failed",
                                message=str(error).strip() or type(error).__name__,
                                retryable=True,
                                details={"error_type": type(error).__name__},
                            ),
                        )
                    )
                    continue

                record = replace(
                    record,
                    status=AssetStatus.DOWNLOADING,
                    final_path=str(written.path),
                    content_sha256=digest,
                    metadata={
                        **record.metadata,
                        "torrent_checkpoint": _torrent_checkpoint(
                            handle,
                            client_name=_torrent_client_name(self._torrent_client),
                            download_dir=download_dir,
                            descriptor_path=written.path,
                            status="submitted",
                        ),
                    },
                )
                if self._store is not None:
                    self._store.update_asset(child.child_id, record)
                pending.append((len(outcomes), handle, record))
                outcomes.append(record)

                progress(
                    "torrent_added",
                    {"asset_id": record.asset.asset_id, "name": torrent_asset.name},
                )
                continue

            # A resumed checkpoint is polled below using its original handle.
            resumed_record = replace(record, status=AssetStatus.DOWNLOADING)
            if self._store is not None:
                self._store.update_asset(child.child_id, resumed_record)
            pending.append((len(outcomes), handle, resumed_record))
            outcomes.append(resumed_record)

        # Phase 2: poll all added torrents until completion.
        release_admission = getattr(cancel_event, "release_admission", None)
        if pending and callable(release_admission):
            await release_admission()
        for idx, handle, written_record in pending:

            def _cancel_requested() -> bool:
                return cancel_event.is_set()

            def _status_callback(outcome: TorrentOutcome) -> None:
                current = outcomes[idx]
                checkpoint = _torrent_checkpoint_from_outcome(
                    current.metadata.get("torrent_checkpoint"), outcome
                )
                updated = replace(
                    current,
                    metadata={**current.metadata, "torrent_checkpoint": checkpoint},
                )
                outcomes[idx] = updated
                if self._store is not None:
                    self._store.update_asset(child.child_id, updated)
                progress(
                    "torrent_progress",
                    {
                        "asset_id": current.asset.asset_id,
                        "name": handle.name,
                        "progress": outcome.progress,
                        "completed_bytes": outcome.completed_bytes,
                        "total_bytes": outcome.total_bytes,
                        "download_speed": outcome.download_speed,
                        "client_state": outcome.client_state,
                    },
                )

            poll_method = getattr(self._torrent_client, "poll_until_complete", None)
            try:
                if callable(poll_method):
                    poll_kwargs = {
                        "cancel_requested": _cancel_requested,
                        "status_callback": _status_callback,
                    }
                    if callable(getattr(cancel_event, "is_paused", None)) and poll_supports_argument(
                        poll_method, "pause_requested"
                    ):
                        poll_kwargs["pause_requested"] = cancel_event.is_paused
                        if poll_supports_argument(poll_method, "pause_waiter"):
                            poll_kwargs["pause_waiter"] = cancel_event.wait_if_paused
                    outcome = await poll_method(
                        handle,
                        **poll_kwargs,
                    )
                else:
                    outcome = await self._torrent_client.get_status(handle)
                    _status_callback(outcome)
            except Exception as error:
                outcomes[idx] = replace(
                    outcomes[idx],
                    status=AssetStatus.FAILED,
                    failure=FailureData(
                        origin=FailureOrigin.SYSTEM,
                        stage=FailureStage.FETCH,
                        code="torrent_poll_failed",
                        message=str(error).strip() or type(error).__name__,
                        retryable=True,
                        details={"error_type": type(error).__name__},
                    ),
                )
                if self._store is not None:
                    self._store.update_asset(child.child_id, outcomes[idx])
                continue

            if outcome.status == "completed":
                outcomes[idx] = replace(outcomes[idx], status=AssetStatus.COMPLETED)
                progress(
                    "torrent_completed",
                    {"asset_id": written_record.asset.asset_id, "name": handle.name},
                )
            elif outcome.status == "cancelled":
                outcomes[idx] = replace(outcomes[idx], status=AssetStatus.CANCELLED)
            elif outcome.status == "error":
                outcomes[idx] = replace(
                    outcomes[idx],
                    status=AssetStatus.FAILED,
                    failure=FailureData(
                        origin=FailureOrigin.SYSTEM,
                        stage=FailureStage.FETCH,
                        code="torrent_download_failed",
                        message=outcome.error or "Unknown error",
                        retryable=True,
                        details={"error": outcome.error} if outcome.error else {},
                    ),
                )
            if self._store is not None:
                self._store.update_asset(child.child_id, outcomes[idx])

        # Phase 3: auto-delete .torrent files.
        if self._auto_delete_torrent_file:
            for _idx, _handle, written_record in pending:
                if written_record.final_path:
                    try:
                        Path(written_record.final_path).unlink(missing_ok=True)
                    except OSError:
                        pass

        return tuple(outcomes)


def _gallery_id(metadata: Mapping[str, object], fallback: str) -> str:
    for key in ("gallery_id", "gid", "slug"):
        value = metadata.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return str(value)
    return fallback


def _torrent_checkpoint(
    handle: TorrentHandle,
    *,
    client_name: str,
    download_dir: Path,
    descriptor_path: Path,
    status: str,
) -> dict[str, object]:
    return {
        "client": client_name,
        "client_id": handle.client_id,
        "name": handle.name,
        "magnet_uri": handle.magnet_uri,
        "download_dir": str(download_dir),
        "descriptor_path": str(descriptor_path),
        "submission": "accepted",
        "status": status,
        "found": True,
        "client_state": "unknown",
        "progress": 0.0,
        "completed_bytes": 0,
        "total_bytes": None,
        "download_speed": 0,
    }


def _torrent_checkpoint_from_outcome(
    checkpoint: object,
    outcome: TorrentOutcome,
) -> dict[str, object]:
    values = dict(checkpoint) if isinstance(checkpoint, Mapping) else {}
    values.update(
        {
            "status": outcome.status,
            "found": outcome.found,
            "client_state": outcome.client_state,
            "progress": outcome.progress,
            "completed_bytes": outcome.completed_bytes,
            "total_bytes": outcome.total_bytes,
            "download_speed": outcome.download_speed,
            "output_dir": outcome.output_dir,
            "error": outcome.error,
        }
    )
    return values


def _torrent_client_name(client: object | None) -> str:
    if client is None:
        return "unknown"
    name = type(client).__name__.lower()
    return "qbittorrent" if "qbittorrent" in name else name


def _package_folder_fields(package: DownloadPackage) -> dict[str, str]:
    fields = {
        "site_id": package.site_id,
        "gallery_id": package.gallery_id,
        "title": package.title or package.gallery_id,
    }
    fields.update(
        (key, str(value))
        for key, value in package.folder_fields.items()
        if isinstance(value, (str, int, float, bool))
    )
    return fields


async def _wait_if_paused(signal: object) -> None:
    wait_if_paused = getattr(signal, "wait_if_paused", None)
    if callable(wait_if_paused):
        await wait_if_paused()


def _asset_key(asset: Asset, index: int) -> str:
    for key in ("asset_id", "image_id", "page_id", "index"):
        value = asset.metadata.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return str(value)
    return f"{index}:{asset.filename or 'asset'}"


def _legacy_asset(asset: DownloadAsset, *, include_plugin_asset_key: bool = True) -> Asset:
    kind_value = asset.metadata.get("asset_kind", AssetKind.FILE.value)
    try:
        kind = AssetKind(kind_value)
    except ValueError:
        kind = AssetKind.FILE
    metadata = dict(asset.metadata)
    if include_plugin_asset_key:
        metadata["plugin_asset_key"] = asset.plugin_asset_key
    return Asset(
        url=asset.url,
        kind=kind,
        filename=asset.filename,
        metadata=metadata,
    )


def _failed_record(
    record: AssetRecord,
    error: Exception,
    stage: FailureStage,
) -> AssetRecord:
    details: dict[str, object] = {"error_type": type(error).__name__}
    http_status = getattr(error, "code", None)
    if isinstance(http_status, int) and not isinstance(http_status, bool):
        details["http_status"] = http_status
    attempt_count = getattr(error, "request_attempt_count", None)
    if isinstance(attempt_count, int) and not isinstance(attempt_count, bool):
        details["request_attempt_count"] = attempt_count
    next_retry_at = getattr(error, "next_retry_at", None)
    if isinstance(next_retry_at, str):
        details["next_retry_at"] = next_retry_at
    return replace(
        record,
        status=AssetStatus.FAILED,
        failure=FailureData(
            origin=FailureOrigin.SYSTEM,
            stage=stage,
            code="asset_download_failed" if stage is FailureStage.FETCH else "asset_write_failed",
            message=str(error).strip() or type(error).__name__,
            retryable=True,
            details=details,
        ),
    )
