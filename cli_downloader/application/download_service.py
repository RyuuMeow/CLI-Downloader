from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable, Mapping, Protocol
from uuid import uuid4

from cli_downloader.core.environment import EnvironmentContext, diagnose_environment
from cli_downloader.application.execution_control import (
    OwnerExecutionFenced,
    OwnerShutdownRequested,
)
from cli_downloader.core.errors import AdapterResolutionError
from cli_downloader.core.events import TaskEvent
from cli_downloader.core.fetch import AssetFetcher
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.core.models import (
    Asset,
    AssetKind,
    DownloadStrategy,
    DownloadStrategyPlan,
    ResolveRequest,
    ResolvedItem,
)
from cli_downloader.core.output import output_folder_fields
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.core.pipeline import PipelineContext, PipelineRunner
from cli_downloader.application.download_limiter import GalleryDownloadLimiter
from cli_downloader.integrations.torrent import (
    TorrentAsset,
    TorrentClient,
    TorrentClientError,
    TorrentHandle,
    TorrentOutcome,
    poll_supports_argument,
    reconcile_torrent_checkpoint,
)
from cli_downloader.sites.registry import SiteRegistry
from cli_downloader.storage.completion_store import CompletionStore
from cli_downloader.storage.file_writer import AssetFileWriter, WrittenAsset
from cli_downloader.storage.history_store import HistoryStore
from cli_downloader.storage.task_store import (
    TaskDesiredControl,
    TaskRecord,
    TaskStatus,
    TaskStore,
)
from cli_downloader.storage.state_history_store import StateHistoryStore


TaskIdFactory = Callable[[], str]
DownloadProgress = Callable[[int, int], None]
ByteProgress = Callable[[str, int, int | None], None]


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_checkpoint(
    metadata: Mapping[str, object],
    asset_url: str,
    fallback_path: str | None,
) -> tuple[Path, str, int | None] | None:
    checkpoints = metadata.get("completed_assets")
    if not isinstance(checkpoints, (list, tuple)):
        return None
    for value in checkpoints:
        if not isinstance(value, Mapping) or value.get("url") != asset_url:
            continue
        path_value = value.get("path") or fallback_path
        digest = value.get("sha256")
        bytes_value = value.get("bytes")
        if not isinstance(path_value, str) or not path_value:
            return None
        if not isinstance(digest, str) or len(digest) != 64:
            return None
        if not isinstance(bytes_value, int) or isinstance(bytes_value, bool) or bytes_value < 0:
            bytes_value = None
        return Path(path_value), digest, bytes_value
    return None


class CancelSignal(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class PlannedTaskResult:
    task_id: str
    item: ResolvedItem
    plan: DownloadStrategyPlan
    record: TaskRecord
    events: tuple[TaskEvent, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "site_id": self.item.site_id,
            "title": self.item.title,
            "strategy": self.plan.strategy.value,
            "asset_count": len(self.plan.assets.assets),
            "status": self.record.status.value,
            "environment_mode": self.record.metadata.get("environment_mode"),
            "terminal_interactive": self.record.metadata.get("terminal_interactive"),
            "events": [event.reason for event in self.events],
        }


@dataclass(frozen=True)
class CompletedTaskResult:
    task_id: str
    item: ResolvedItem
    plan: DownloadStrategyPlan
    record: TaskRecord
    written_assets: tuple[WrittenAsset, ...]
    events: tuple[TaskEvent, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "site_id": self.item.site_id,
            "title": self.item.title,
            "strategy": self.plan.strategy.value,
            "asset_count": len(self.plan.assets.assets),
            "status": self.record.status.value,
            "environment_mode": self.record.metadata.get("environment_mode"),
            "terminal_interactive": self.record.metadata.get("terminal_interactive"),
            "output_files": [str(asset.path) for asset in self.written_assets],
            "events": [event.reason for event in self.events],
        }


class DownloadService:
    def __init__(
        self,
        *,
        registry: SiteRegistry,
        task_store: TaskStore,
        history_store: HistoryStore | None = None,
        completion_store: CompletionStore | None = None,
        environment: EnvironmentContext | None = None,
        task_id_factory: TaskIdFactory | None = None,
        output_folder_template: str | None = None,
        output_field_names: tuple[str, ...] = (),
        auto_delete_completed: bool = False,
        torrent_client: TorrentClient | None = None,
        auto_delete_torrent_file: bool = True,
    ) -> None:
        self._registry = registry
        self._task_store = task_store
        self._history_store = history_store
        self._completion_store = completion_store
        self._environment = environment or diagnose_environment()
        self._task_id_factory = task_id_factory or (lambda: uuid4().hex)
        self._output_folder_template = output_folder_template
        self._output_field_names = output_field_names
        self._auto_delete_completed = auto_delete_completed
        self._torrent_client = torrent_client
        self._auto_delete_torrent_file = auto_delete_torrent_file

    async def plan_task(
        self,
        url: str,
        *,
        profile: str | None = None,
        strategy: DownloadStrategy = DownloadStrategy.AUTO,
        record_metadata: dict[str, object] | None = None,
        task_id: str | None = None,
        force: bool = False,
    ) -> PlannedTaskResult:
        item, plan, record = await self._build_planned_record(
            url,
            profile=profile,
            strategy=strategy,
            record_metadata=record_metadata,
            task_id=task_id,
            force=force,
        )

        context = PipelineContext(
            task_id=record.task_id,
            data={
                "item": item,
                "plan": plan,
                "record": record,
                "environment": self._environment,
            },
        )
        result_context = await PipelineRunner((PersistPlannedTaskStage(self._task_store),)).run(
            context
        )
        self._append_history(result_context.events)
        self._upsert_history_record(record)
        stored_record = self._task_store.get(record.task_id)

        return PlannedTaskResult(
            task_id=record.task_id,
            item=item,
            plan=plan,
            record=stored_record or record,
            events=result_context.events,
        )

    async def download_task(
        self,
        url: str,
        *,
        fetcher: AssetFetcher,
        file_writer: AssetFileWriter,
        profile: str | None = None,
        strategy: DownloadStrategy = DownloadStrategy.AUTO,
        record_metadata: dict[str, object] | None = None,
        cancel_signal: CancelSignal | None = None,
        progress: DownloadProgress | None = None,
        byte_progress: ByteProgress | None = None,
        download_limiter: GalleryDownloadLimiter | None = None,
        task_id: str | None = None,
        force: bool = False,
    ) -> CompletedTaskResult:
        item, plan, record = await self._build_planned_record(
            url,
            profile=profile,
            strategy=strategy,
            record_metadata=record_metadata,
            task_id=task_id,
            force=force,
        )
        context = PipelineContext(
            task_id=record.task_id,
            data={
                "item": item,
                "plan": plan,
                "record": record,
                "environment": self._environment,
            },
        )

        if record.status is TaskStatus.SKIPPED:
            self._task_store.upsert(record)
            self._upsert_history_record(record, finished=True)
            return CompletedTaskResult(
                task_id=record.task_id,
                item=item,
                plan=plan,
                record=record,
                written_assets=(),
                events=(),
            )

        execution_fence = record.metadata.get("execution_fence")
        execution_fence = execution_fence if isinstance(execution_fence, str) else None
        try:
            result_context = await PipelineRunner(
                (
                    PersistPlannedTaskStage(self._task_store),
                    WriteAssetsStage(
                        fetcher,
                        file_writer,
                        task_store=self._task_store,
                        cancel_signal=cancel_signal,
                        progress=progress,
                        byte_progress=byte_progress,
                        download_limiter=download_limiter,
                        torrent_client=self._torrent_client,
                        auto_delete_torrent_file=self._auto_delete_torrent_file,
                        execution_fence=execution_fence,
                    ),
                    CompleteTaskStage(self._task_store),
                )
            ).run(context)
        except OwnerShutdownRequested:
            current = self._task_store.get(record.task_id) or record
            paused = current.status is TaskStatus.PAUSED or current.desired_control.value == "pause"
            checkpoint = current.transition(
                status=TaskStatus.PAUSED if paused else TaskStatus.PLANNED,
                desired_control=TaskDesiredControl.PAUSE if paused else TaskDesiredControl.RUN,
                active_run_id=None,
                metadata={**current.metadata, "owner_shutdown_checkpoint": True},
            )
            self._task_store.upsert(checkpoint)
            self._upsert_history_record(checkpoint, finished=False)
            raise
        except OwnerExecutionFenced:
            # A newer owner has already rotated the durable execution fence.
            # Do not overwrite its state with a failure/cancellation outcome.
            raise
        except asyncio.CancelledError:
            cancelled = (self._task_store.get(record.task_id) or record).transition(
                status=TaskStatus.CANCELED,
            )
            self._task_store.upsert(cancelled)
            event = TaskEvent(
                task_id=record.task_id,
                name="task_cancelled",
                reason="download_cancelled",
                payload={"status": cancelled.status.value},
            )
            self._append_history((event,))
            self._upsert_history_record(cancelled, finished=True)
            raise
        except Exception:
            current = self._task_store.get(record.task_id) or record
            failed = current.transition(
                status=(
                    TaskStatus.PARTIALLY_COMPLETED
                    if current.metadata.get("completed_files", 0)
                    else TaskStatus.FAILED
                ),
            )
            self._task_store.upsert(failed)
            self._append_history(
                (
                    TaskEvent(
                        task_id=record.task_id,
                        name="task_transitioned",
                        reason="task_failed",
                        payload={"status": failed.status.value},
                    ),
                )
            )
            self._upsert_history_record(failed, finished=True)
            raise
        self._append_history(result_context.events)
        completed_record = result_context.data["record"]

        if (
            isinstance(completed_record, TaskRecord)
            and completed_record.status is TaskStatus.COMPLETED
        ):
            if self._completion_store is not None:
                self._completion_store.record_completed(
                    completed_record.site_id,
                    completed_record.source_url,
                )

        written_assets = tuple(result_context.data.get("written_assets", ()))
        if isinstance(completed_record, TaskRecord):
            self._upsert_history_record(
                completed_record,
                output_files=tuple(str(asset.path) for asset in written_assets),
                finished=True,
            )
        result = CompletedTaskResult(
            task_id=record.task_id,
            item=item,
            plan=plan,
            record=completed_record if isinstance(completed_record, TaskRecord) else record,
            written_assets=written_assets,
            events=result_context.events,
        )

        # Delete after all bookkeeping so history and result are correct.
        if self._auto_delete_completed and isinstance(completed_record, TaskRecord):
            self._task_store.delete(completed_record.task_id)

        return result

    async def _build_planned_record(
        self,
        url: str,
        *,
        profile: str | None = None,
        strategy: DownloadStrategy = DownloadStrategy.AUTO,
        record_metadata: dict[str, object] | None = None,
        task_id: str | None = None,
        force: bool = False,
    ) -> tuple[ResolvedItem, DownloadStrategyPlan, TaskRecord]:
        adapter = self._registry.adapter_for_url(url)
        if adapter is None:
            raise AdapterResolutionError(f"no registered site adapter can handle URL: {url}")

        item = await adapter.resolve(ResolveRequest(url=url, profile=profile, strategy=strategy))
        plan = await adapter.choose_strategy(item)
        task_id = task_id or self._task_id_factory()
        existing = self._task_store.get(task_id)

        status = TaskStatus.PLANNED
        if not force and self._completion_store is not None:
            if self._completion_store.is_completed(item.site_id, item.source_url):
                status = TaskStatus.SKIPPED

        output_fields = _item_folder_fields(item, self._output_field_names)
        record = TaskRecord(
            task_id=task_id,
            status=status,
            site_id=item.site_id,
            source_url=item.source_url,
            title=item.title,
            kind=existing.kind if existing is not None else TaskKind.ITEM,
            desired_control=(
                existing.desired_control if existing is not None else TaskDesiredControl.RUN
            ),
            revision=existing.revision if existing is not None else 0,
            run_generation=existing.run_generation if existing is not None else 0,
            created_at=existing.created_at if existing is not None else datetime.now(UTC),
            metadata={
                "strategy": plan.strategy.value,
                "scheduler": {
                    "root_id": task_id,
                    "enqueue_sequence": 0,
                    "site_id": item.site_id,
                },
                "asset_count": len(plan.assets.assets),
                "completed_files": 0,
                "total_files": len(plan.assets.assets),
                "assets": [
                    {"url": a.url, "filename": a.filename or a.asset_id or str(i)}
                    for i, a in enumerate(plan.assets.assets)
                ],
                "profile": profile or "default",
                "environment_mode": self._environment.paths.mode,
                "platform": self._environment.platform,
                "terminal_interactive": self._environment.terminal.interactive,
                "terminal_hyperlink": self._environment.terminal.hyperlink,
                "execution_fence": (
                    existing.metadata.get("execution_fence")
                    if existing is not None
                    and isinstance(existing.metadata.get("execution_fence"), str)
                    else uuid4().hex
                ),
                **(
                    {
                        "output_folder_template": self._output_folder_template,
                        "output_folder_fields": output_fields,
                    }
                    if self._output_folder_template is not None
                    else {}
                ),
                **(existing.metadata if existing is not None else {}),
                **(record_metadata or {}),
            },
        )

        return item, plan, record

    def _append_history(self, events: tuple[TaskEvent, ...]) -> None:
        if self._history_store is not None:
            self._history_store.append_many(events)

    def _upsert_history_record(
        self,
        record: TaskRecord,
        *,
        output_files: tuple[str, ...] = (),
        finished: bool = False,
    ) -> None:
        if not isinstance(self._history_store, StateHistoryStore):
            return
        current = self._history_store.get_record(record.task_id)
        history_record = current or DownloadHistoryRecord(
            record_id=record.task_id,
            task_id=record.task_id,
            kind=HistoryKind.ITEM,
            site_id=record.site_id,
            title=record.title,
            source_url=record.source_url,
            status=record.status.value,
            created_at=record.created_at,
            run_generation=record.run_generation,
        )
        metadata_files = record.metadata.get("completed_files", 0)
        metadata_total = record.metadata.get("total_files")
        completed_files = metadata_files if isinstance(metadata_files, int) else 0
        total_files = metadata_total if isinstance(metadata_total, int) else None
        files = output_files or tuple(
            path for path in record.metadata.get("output_files", ()) if isinstance(path, str)
        )
        self._history_store.upsert_record(
            replace(
                history_record,
                task_id=record.task_id,
                kind=HistoryKind.ITEM,
                site_id=record.site_id,
                title=record.title,
                source_url=record.source_url,
                status=record.status.value,
                completed_files=completed_files,
                total_files=total_files,
                output_files=files,
                finished_at=(
                    datetime.now(UTC)
                    if finished
                    else None
                    if record.status in {TaskStatus.PENDING, TaskStatus.PLANNED, TaskStatus.PAUSED}
                    else history_record.finished_at
                ),
                run_generation=record.run_generation,
            )
        )


class PersistPlannedTaskStage:
    name = "persist_planned_task"

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store

    async def run(self, context: PipelineContext) -> PipelineContext:
        record = context.data["record"]
        if not isinstance(record, TaskRecord):
            raise TypeError("pipeline context record must be a TaskRecord")

        self._task_store.upsert(record)
        return context.emit(
            name="task_persisted",
            reason="planned_task_persisted",
            status=record.status.value,
            site_id=record.site_id,
            title=record.title,
            source_url=record.source_url,
        )


class _StandaloneLeaseController:
    """Pause-safe central Item lease for a standalone task."""

    def __init__(
        self,
        limiter: GalleryDownloadLimiter,
        *,
        task_id: str,
        site_id: str,
        cancelled: Callable[[], bool],
    ) -> None:
        self._limiter = limiter
        self._task_id = task_id
        self._site_id = site_id
        self._cancelled = cancelled
        self._lease = None
        self._detached = False

    async def acquire(self) -> None:
        if self._lease is None and not self._detached and not self._cancelled():
            self._lease = await self._limiter.acquire_item(
                root_id=self._task_id,
                site_id=self._site_id,
            )

    async def release(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            await lease.release()

    async def detach(self) -> None:
        self._detached = True
        await self.release()


class WriteAssetsStage:
    name = "write_assets"

    def __init__(
        self,
        fetcher: AssetFetcher,
        file_writer: AssetFileWriter,
        *,
        task_store: TaskStore,
        cancel_signal: CancelSignal | None = None,
        progress: DownloadProgress | None = None,
        byte_progress: ByteProgress | None = None,
        download_limiter: GalleryDownloadLimiter | None = None,
        torrent_client: TorrentClient | None = None,
        auto_delete_torrent_file: bool = True,
        execution_fence: str | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._file_writer = file_writer
        self._task_store = task_store
        self._cancel_signal = cancel_signal
        self._progress = progress
        self._byte_progress = byte_progress
        self._download_limiter = download_limiter
        self._torrent_client = torrent_client
        self._auto_delete_torrent_file = auto_delete_torrent_file
        self._execution_fence = execution_fence
        self._active_asset: Asset | None = None
        self._active_partial_path: Path | None = None

    async def run(self, context: PipelineContext) -> PipelineContext:
        controller = None
        if self._download_limiter is not None:
            item = context.data.get("item")
            site_id = item.site_id if isinstance(item, ResolvedItem) else "unknown"
            controller = _StandaloneLeaseController(
                self._download_limiter,
                task_id=context.task_id,
                site_id=site_id,
                cancelled=lambda: self._torrent_cancel_requested(context.task_id),
            )
            record = self._task_store.get(context.task_id)
            if record is not None and record.status is TaskStatus.PAUSED:
                self._download_limiter.suspend_root_sync(context.task_id)
            await controller.acquire()
        try:
            return await self._run(context, lease_controller=controller)
        except OwnerShutdownRequested:
            self._record_partial_checkpoint(context.task_id)
            raise
        finally:
            if controller is not None:
                await controller.release()

    async def _run(
        self,
        context: PipelineContext,
        *,
        lease_controller: _StandaloneLeaseController | None = None,
    ) -> PipelineContext:
        plan = context.data["plan"]
        if not isinstance(plan, DownloadStrategyPlan):
            raise TypeError("pipeline context plan must be a DownloadStrategyPlan")
        item = context.data["item"]
        if not isinstance(item, ResolvedItem):
            raise TypeError("pipeline context item must be a ResolvedItem")

        self._assert_execution_fence(context.task_id)

        if plan.strategy is DownloadStrategy.TORRENT:
            return await self._run_torrent(
                context,
                item,
                plan,
                lease_controller=lease_controller,
            )

        written_assets: list[WrittenAsset] = []
        total = len(plan.assets.assets)
        record = self._task_store.get(context.task_id)
        if record is None:
            raise RuntimeError(f"task disappeared before download: {context.task_id}")
        folder_fields = record.metadata.get("output_folder_fields")
        if not isinstance(folder_fields, Mapping):
            folder_fields = _item_folder_fields(context.data["item"])
        folder_template = record.metadata.get("output_folder_template")
        if not isinstance(folder_template, str):
            folder_template = None
        await self._wait_until_runnable(
            context.task_id,
            lease_controller=lease_controller,
        )
        if self._cancel_signal is not None and self._cancel_signal.is_set():
            raise asyncio.CancelledError
        if self._progress is not None:
            self._progress(0, total)
        existing_outputs = record.metadata.get("output_files")
        existing_outputs = (
            tuple(value for value in existing_outputs if isinstance(value, str))
            if isinstance(existing_outputs, (list, tuple))
            else ()
        )
        completed_count = record.metadata.get("completed_files", 0)
        completed_count = (
            min(max(completed_count, 0), total)
            if isinstance(completed_count, int) and not isinstance(completed_count, bool)
            else 0
        )
        for index, asset in enumerate(plan.assets.assets):
            self._active_asset = asset
            self._active_partial_path = self._file_writer.paths(
                asset,
                task_id=context.task_id,
                folder_fields=folder_fields,
                folder_template=folder_template,
            )[1]
            await self._wait_until_runnable(
                context.task_id,
                lease_controller=lease_controller,
            )
            self._assert_execution_fence(context.task_id)
            if self._cancel_signal is not None and self._cancel_signal.is_set():
                raise asyncio.CancelledError

            if index < completed_count and index < len(existing_outputs):
                checkpoint = _verified_checkpoint(
                    record.metadata,
                    asset.url,
                    existing_outputs[index],
                )
                if checkpoint is not None:
                    existing_path, expected_digest, expected_bytes = checkpoint
                    if existing_path.is_file():
                        actual_bytes = existing_path.stat().st_size
                        if (expected_bytes is None or actual_bytes == expected_bytes) and (
                            await asyncio.to_thread(_sha256_file, existing_path) == expected_digest
                        ):
                            written_assets.append(
                                WrittenAsset(
                                    asset_url=asset.url,
                                    path=existing_path,
                                    bytes_written=actual_bytes,
                                )
                            )
                            if self._progress is not None:
                                self._progress(len(written_assets), total)
                            self._active_asset = None
                            self._active_partial_path = None
                            continue

            content_sha256: str
            if hasattr(self._fetcher, "fetch_stream"):
                hash_obj = sha256()

                async def _chunk_generator():
                    import time

                    bytes_so_far = 0
                    last_report_time = 0.0
                    async for chunk, total in self._fetcher.fetch_stream(asset):
                        await self._wait_until_runnable(
                            context.task_id,
                            lease_controller=lease_controller,
                        )
                        bytes_so_far += len(chunk)
                        hash_obj.update(chunk)
                        if self._byte_progress is not None:
                            now = time.monotonic()
                            if (
                                total is None
                                or bytes_so_far == total
                                or now - last_report_time > 0.1
                            ):
                                self._byte_progress(
                                    f"{context.task_id}:{asset.url}", bytes_so_far, total
                                )
                                last_report_time = now
                        yield chunk

                async def fetch_and_write() -> WrittenAsset:
                    return await self._file_writer.write_stream(
                        asset,
                        _chunk_generator(),
                        task_id=context.task_id,
                        folder_fields=folder_fields,
                        folder_template=folder_template,
                    )

                self._assert_execution_fence(context.task_id)
                written_asset = (
                    await self._download_limiter.request(item.site_id, fetch_and_write)
                    if self._download_limiter is not None
                    else await fetch_and_write()
                )
                written_assets.append(written_asset)
                content_sha256 = hash_obj.hexdigest()
            else:

                async def fetch_asset() -> bytes:
                    return await self._fetcher.fetch(asset)

                content = (
                    await self._download_limiter.request(
                        item.site_id,
                        fetch_asset,
                    )
                    if self._download_limiter is not None
                    else await fetch_asset()
                )
                if self._cancel_signal is not None and self._cancel_signal.is_set():
                    raise asyncio.CancelledError
                self._assert_execution_fence(context.task_id)
                written_assets.append(
                    self._file_writer.write(
                        asset,
                        content,
                        task_id=context.task_id,
                        folder_fields=folder_fields,
                        folder_template=folder_template,
                    )
                )
                content_sha256 = sha256(content).hexdigest()
            self._assert_execution_fence(context.task_id)
            current = self._task_store.get(context.task_id)
            if current is None:
                raise RuntimeError(f"task disappeared during download: {context.task_id}")
            completed_assets = [
                value
                for value in (
                    current.metadata.get("completed_assets", [])
                    if isinstance(current.metadata.get("completed_assets", []), (list, tuple))
                    else []
                )
                if not isinstance(value, Mapping) or value.get("url") != asset.url
            ]
            completed_asset = written_assets[-1]
            completed_assets.append(
                {
                    "url": asset.url,
                    "path": str(completed_asset.path),
                    "bytes": completed_asset.bytes_written,
                    "sha256": content_sha256,
                }
            )
            self._task_store.upsert(
                current.transition(
                    metadata={
                        **current.metadata,
                        "completed_files": len(written_assets),
                        "total_files": total,
                        "output_files": [str(item.path) for item in written_assets],
                        "completed_assets": completed_assets,
                        "partial_files": {
                            key: value
                            for key, value in (
                                current.metadata.get("partial_files", {})
                                if isinstance(current.metadata.get("partial_files", {}), Mapping)
                                else {}
                            ).items()
                            if key != asset.url
                        },
                    },
                )
            )
            self._active_asset = None
            self._active_partial_path = None
            if self._progress is not None:
                self._progress(len(written_assets), total)

        if self._shutdown_requested():
            raise OwnerShutdownRequested

        return context.with_data(written_assets=tuple(written_assets)).emit(
            name="assets_written",
            reason="direct_assets_written",
            asset_count=len(written_assets),
            output_files=[str(item.path) for item in written_assets],
        )

    async def _run_torrent(
        self,
        context: PipelineContext,
        item: ResolvedItem,
        plan: DownloadStrategyPlan,
        *,
        lease_controller: _StandaloneLeaseController | None = None,
    ) -> PipelineContext:
        if self._torrent_client is None:
            raise TorrentClientError(
                "Torrent download requested but no torrent client is configured."
            )

        record = self._task_store.get(context.task_id)
        if record is None:
            raise RuntimeError(f"task disappeared before torrent download: {context.task_id}")
        self._assert_execution_fence(context.task_id)
        folder_fields = record.metadata.get("output_folder_fields")
        if not isinstance(folder_fields, Mapping):
            folder_fields = _item_folder_fields(item)
        folder_template = record.metadata.get("output_folder_template")
        if not isinstance(folder_template, str):
            folder_template = None

        dummy = Asset(url="dummy://.torrent", kind=AssetKind.TORRENT, filename="dummy.torrent")
        target_path, _ = self._file_writer.paths(
            dummy,
            task_id=context.task_id,
            folder_fields=folder_fields,
            folder_template=folder_template,
        )
        download_dir = target_path.parent
        download_dir.mkdir(parents=True, exist_ok=True)

        written_assets: list[WrittenAsset] = []
        total = len(plan.assets.assets)
        if self._progress is not None:
            self._progress(0, total)
        for asset in plan.assets.assets:
            await self._wait_until_runnable(
                context.task_id,
                lease_controller=lease_controller,
            )
            self._assert_execution_fence(context.task_id)
            if self._cancel_signal is not None and self._cancel_signal.is_set():
                raise asyncio.CancelledError

            checkpoint = self._torrent_checkpoint(context.task_id, asset)
            reconciliation = (
                await reconcile_torrent_checkpoint(self._torrent_client, checkpoint)
                if checkpoint is not None
                else None
            )
            if reconciliation is not None and reconciliation.action == "error":
                outcome = reconciliation.outcome
                if outcome is not None:
                    self._persist_torrent_outcome(context.task_id, asset, outcome)
                raise TorrentClientError(
                    (outcome.error if outcome is not None else None)
                    or "Could not reconcile persisted torrent with client"
                )

            handle: TorrentHandle
            written: WrittenAsset
            if reconciliation is not None and reconciliation.action in {
                "completed",
                "externally_completed",
                "resume",
            }:
                handle = reconciliation.handle
                if handle is None:
                    raise TorrentClientError("Persisted torrent checkpoint has no client handle")
                descriptor_path = (
                    checkpoint.get("descriptor_path") if checkpoint is not None else None
                )
                if not isinstance(descriptor_path, str) or not descriptor_path:
                    descriptor_path, _ = self._file_writer.paths(
                        asset,
                        task_id=context.task_id,
                        folder_fields=folder_fields,
                        folder_template=folder_template,
                    )
                written = WrittenAsset(
                    asset_url=asset.url,
                    path=Path(descriptor_path),
                    bytes_written=0,
                )
                if reconciliation.outcome is not None:
                    self._persist_torrent_outcome(context.task_id, asset, reconciliation.outcome)
            else:
                content = await self._fetcher.fetch(asset)
                written = self._file_writer.write(
                    asset,
                    content,
                    task_id=context.task_id,
                    folder_fields=folder_fields,
                    folder_template=folder_template,
                )
                torrent = TorrentAsset(
                    name=asset.filename or "download.torrent",
                    torrent_url=asset.url,
                    torrent_data=content,
                    index=_asset_index(asset),
                )
                handle = await self._torrent_client.add_torrent(torrent, download_dir)

                self._persist_torrent_checkpoint(
                    context.task_id,
                    asset,
                    handle,
                    download_dir=download_dir,
                    descriptor_path=written.path,
                    status="submitted",
                )

            # qBittorrent owns the long-running transfer after the handle is
            # known. Core no longer consumes an Item admission slot while it
            # polls that external task.
            if lease_controller is not None:
                await lease_controller.detach()

            def _status_callback(outcome: TorrentOutcome) -> None:
                self._persist_torrent_outcome(context.task_id, asset, outcome)
                if self._byte_progress is not None:
                    self._byte_progress(
                        f"{context.task_id}:torrent:{handle.client_id}",
                        outcome.completed_bytes,
                        outcome.total_bytes,
                    )

            if reconciliation is not None and reconciliation.action in {
                "completed",
                "externally_completed",
            }:
                outcome = reconciliation.outcome
                if outcome is None:
                    raise TorrentClientError("Torrent reconciliation returned no completion status")
            else:
                poll_method = getattr(self._torrent_client, "poll_until_complete", None)
                if callable(poll_method):
                    poll_kwargs = {
                        "cancel_requested": lambda: self._torrent_cancel_requested(context.task_id),
                        "status_callback": _status_callback,
                    }
                    if poll_supports_argument(poll_method, "shutdown_requested"):
                        poll_kwargs["shutdown_requested"] = self._shutdown_requested
                    if poll_supports_argument(poll_method, "pause_requested"):
                        poll_kwargs["pause_requested"] = lambda: self._torrent_pause_requested(
                            context.task_id
                        )
                        if poll_supports_argument(poll_method, "pause_waiter"):
                            poll_kwargs["pause_waiter"] = lambda: self._wait_for_torrent_resume(
                                context.task_id
                            )
                    outcome = await poll_method(
                        handle,
                        **poll_kwargs,
                    )
                    # The shutdown path returns a final observed client
                    # state without invoking the polling callback. Persist it
                    # before the owner checkpoint is written.
                    _status_callback(outcome)
                else:
                    outcome = await self._torrent_client.get_status(handle)
                    _status_callback(outcome)

            self._persist_torrent_outcome(context.task_id, asset, outcome)

            if self._shutdown_requested():
                raise OwnerShutdownRequested

            if outcome.status == "cancelled":
                raise asyncio.CancelledError
            if outcome.status != "completed":
                raise TorrentClientError(outcome.error or "Torrent download did not complete")

            if self._auto_delete_torrent_file:
                written.path.unlink(missing_ok=True)
            written_assets.append(written)
            current = self._task_store.get(context.task_id)
            if current is None:
                raise RuntimeError(f"task disappeared during torrent download: {context.task_id}")
            self._task_store.upsert(
                current.transition(
                    metadata={
                        **current.metadata,
                        "completed_files": len(written_assets),
                        "total_files": total,
                        "output_files": [str(asset.path) for asset in written_assets],
                    },
                )
            )
            if self._progress is not None:
                self._progress(len(written_assets), total)

        if self._shutdown_requested():
            raise OwnerShutdownRequested

        return context.with_data(written_assets=tuple(written_assets)).emit(
            name="torrent_assets_written",
            reason="torrent_assets_written",
            asset_count=len(written_assets),
            output_files=[str(asset.path) for asset in written_assets],
        )

    def _persist_torrent_checkpoint(
        self,
        task_id: str,
        asset: Asset,
        handle: TorrentHandle,
        *,
        download_dir: Path,
        descriptor_path: Path,
        status: str,
    ) -> None:
        self._assert_execution_fence(task_id)
        current = self._task_store.get(task_id)
        if current is None:
            raise RuntimeError(f"task disappeared during torrent download: {task_id}")
        checkpoints = current.metadata.get("torrent_checkpoints", {})
        checkpoints = dict(checkpoints) if isinstance(checkpoints, Mapping) else {}
        checkpoints[_torrent_checkpoint_key(asset)] = {
            "client": _torrent_client_name(self._torrent_client),
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
        self._task_store.upsert(
            current.transition(metadata={**current.metadata, "torrent_checkpoints": checkpoints})
        )

    def _torrent_checkpoint(self, task_id: str, asset: Asset) -> Mapping[str, object] | None:
        current = self._task_store.get(task_id)
        if current is None:
            return None
        checkpoints = current.metadata.get("torrent_checkpoints", {})
        if not isinstance(checkpoints, Mapping):
            return None
        checkpoint = checkpoints.get(_torrent_checkpoint_key(asset))
        return checkpoint if isinstance(checkpoint, Mapping) else None

    def _persist_torrent_outcome(self, task_id: str, asset: Asset, outcome: TorrentOutcome) -> None:
        self._assert_execution_fence(task_id)
        current = self._task_store.get(task_id)
        if current is None:
            raise RuntimeError(f"task disappeared during torrent download: {task_id}")
        checkpoints = current.metadata.get("torrent_checkpoints", {})
        checkpoints = dict(checkpoints) if isinstance(checkpoints, Mapping) else {}
        checkpoint = checkpoints.get(_torrent_checkpoint_key(asset), {})
        checkpoint = dict(checkpoint) if isinstance(checkpoint, Mapping) else {}
        checkpoint.update(
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
        checkpoints[_torrent_checkpoint_key(asset)] = checkpoint
        self._task_store.upsert(
            current.transition(metadata={**current.metadata, "torrent_checkpoints": checkpoints})
        )

    async def _wait_until_runnable(
        self,
        task_id: str,
        *,
        lease_controller: _StandaloneLeaseController | None = None,
    ) -> None:
        suspended = False
        while True:
            record = self._task_store.get(task_id)
            if record is None:
                raise RuntimeError(f"task disappeared during download: {task_id}")
            self._assert_execution_fence(task_id)
            if self._shutdown_requested():
                if self._download_limiter is not None:
                    self._download_limiter.suspend_root_sync(task_id)
                if lease_controller is not None:
                    await lease_controller.release()
                raise OwnerShutdownRequested
            if record.status is TaskStatus.CANCELED or (
                self._cancel_signal is not None and self._cancel_signal.is_set()
            ):
                if lease_controller is not None:
                    await lease_controller.release()
                    self._download_limiter.cancel_root_sync(task_id)
                raise asyncio.CancelledError
            if record.status is TaskStatus.PAUSED:
                if not suspended:
                    if self._download_limiter is not None:
                        self._download_limiter.suspend_root_sync(task_id)
                    if lease_controller is not None:
                        await lease_controller.release()
                    suspended = True
                await asyncio.sleep(0.05)
                continue
            if suspended:
                if self._download_limiter is not None:
                    self._download_limiter.resume_root_sync(task_id)
                if lease_controller is not None:
                    await lease_controller.acquire()
                suspended = False
            if record.status is TaskStatus.PLANNED:
                self._task_store.upsert(record.transition(status=TaskStatus.RUNNING))
            return

    def _shutdown_requested(self) -> bool:
        signal = self._cancel_signal
        checker = getattr(signal, "is_owner_shutdown", None)
        return callable(checker) and bool(checker())

    def _assert_execution_fence(self, task_id: str) -> None:
        if self._execution_fence is None:
            return
        current = self._task_store.get(task_id)
        if current is None:
            raise OwnerExecutionFenced(f"task disappeared during fenced execution: {task_id}")
        actual = current.metadata.get("execution_fence")
        if actual != self._execution_fence:
            raise OwnerExecutionFenced(f"execution fence changed for task: {task_id}")

    def _record_partial_checkpoint(self, task_id: str) -> None:
        asset = self._active_asset
        partial = self._active_partial_path
        if asset is None or partial is None or not partial.is_file():
            return
        self._assert_execution_fence(task_id)
        current = self._task_store.get(task_id)
        if current is None:
            return
        existing = current.metadata.get("partial_files", {})
        partial_files = dict(existing) if isinstance(existing, Mapping) else {}
        partial_files[asset.url] = {
            "path": str(partial),
            "bytes": partial.stat().st_size,
            "sha256": _sha256_file(partial),
        }
        self._task_store.upsert(
            current.transition(metadata={**current.metadata, "partial_files": partial_files})
        )

    def _torrent_cancel_requested(self, task_id: str) -> bool:
        if self._shutdown_requested():
            return False
        if self._cancel_signal is not None and self._cancel_signal.is_set():
            return True
        record = self._task_store.get(task_id)
        return record is None or record.status is TaskStatus.CANCELED

    def _torrent_pause_requested(self, task_id: str) -> bool:
        record = self._task_store.get(task_id)
        return record is not None and record.status is TaskStatus.PAUSED

    async def _wait_for_torrent_resume(self, task_id: str) -> None:
        while self._torrent_pause_requested(task_id):
            await asyncio.sleep(0.05)


class CompleteTaskStage:
    name = "complete_task"

    def __init__(self, task_store: TaskStore) -> None:
        self._task_store = task_store

    async def run(self, context: PipelineContext) -> PipelineContext:
        record = context.data["record"]
        written_assets = context.data.get("written_assets", ())
        if not isinstance(record, TaskRecord):
            raise TypeError("pipeline context record must be a TaskRecord")

        current = self._task_store.get(context.task_id) or record
        completed = current.transition(
            status=TaskStatus.COMPLETED,
            metadata={
                **current.metadata,
                "output_files": [str(asset.path) for asset in written_assets],
            },
        )
        self._task_store.upsert(completed)
        return context.with_data(record=completed).emit(
            name="task_transitioned",
            reason="task_completed",
            status=completed.status.value,
        )


def _asset_index(asset: Asset) -> int:
    value = asset.metadata.get("index", 1)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 1


def _torrent_checkpoint_key(asset: Asset) -> str:
    return f"{_asset_index(asset)}:{asset.url}"


def _torrent_client_name(client: object | None) -> str:
    if client is None:
        return "unknown"
    name = type(client).__name__.lower()
    return "qbittorrent" if "qbittorrent" in name else name


def _item_folder_fields(
    value: object,
    declared_fields: tuple[str, ...] = (),
) -> dict[str, str]:
    if not isinstance(value, ResolvedItem):
        raise TypeError("pipeline context item must be a ResolvedItem")
    gallery_id = next(
        (
            str(value.metadata[key])
            for key in ("gallery_id", "gid", "slug")
            if isinstance(value.metadata.get(key), (str, int))
            and not isinstance(value.metadata.get(key), bool)
        ),
        "unknown",
    )
    return output_folder_fields(
        site_id=value.site_id,
        gallery_id=gallery_id,
        title=value.title,
        metadata=value.metadata,
        declared_fields=declared_fields,
    )
