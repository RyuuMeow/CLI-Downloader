from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from typing import Mapping

from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.batch_retry import BatchRetryService
from cli_downloader.application.task_execution_coordinator import TaskExecutionCoordinator
from cli_downloader.application.task_lifecycle_service import TaskLifecycleService
from cli_downloader.core.batch import (
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchSummary,
    DownloadChildStatus,
)
from cli_downloader.core.errors import InvalidTaskTransitionError, TaskNotFoundError
from cli_downloader.core.progress import checkpoint_byte_progress, normalise_byte_progress
from cli_downloader.core.task_tree import (
    BatchChildPage,
    BatchChildSummary,
    TaskFileNode,
    TaskKind,
    TaskNode,
    TaskProgress,
    TaskRootSummary,
    TaskRootPage,
    TaskState,
)
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import (
    TaskDesiredControl,
    TaskRecord,
    TaskStatus,
    TaskStore,
)


SchedulerProjection = dict[tuple[str, int | None], dict[str, object]]


_STANDALONE_STATES = {
    TaskStatus.PENDING: TaskState.PREPARING,
    TaskStatus.PLANNED: TaskState.QUEUED,
    TaskStatus.RUNNING: TaskState.RUNNING,
    TaskStatus.PAUSED: TaskState.PAUSED,
    TaskStatus.COMPLETED: TaskState.COMPLETED,
    TaskStatus.PARTIALLY_COMPLETED: TaskState.PARTIALLY_COMPLETED,
    TaskStatus.FAILED: TaskState.FAILED,
    TaskStatus.CANCELED: TaskState.CANCELLED,
    TaskStatus.SKIPPED: TaskState.SKIPPED,
}

_CHILD_STATES = {
    DownloadChildStatus.QUEUED: TaskState.QUEUED,
    DownloadChildStatus.DOWNLOADING: TaskState.RUNNING,
    DownloadChildStatus.COMPLETED: TaskState.COMPLETED,
    DownloadChildStatus.PARTIALLY_COMPLETED: TaskState.PARTIALLY_COMPLETED,
    DownloadChildStatus.FAILED: TaskState.FAILED,
    DownloadChildStatus.CANCELLED: TaskState.CANCELLED,
}

_TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELED,
        TaskStatus.SKIPPED,
    }
)


@dataclass(frozen=True)
class TaskControlReceipt:
    """Typed distinction between a requested control and its applied state."""

    task_id: str
    action: str
    requested: bool
    applied: bool
    actual_status: TaskStatus
    desired_control: TaskDesiredControl
    revision: int
    quiescent: bool
    phase: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "action": self.action,
            "requested": self.requested,
            "applied": self.applied,
            "actual_status": self.actual_status.value,
            "desired_control": self.desired_control.value,
            "revision": self.revision,
            "quiescent": self.quiescent,
            "phase": self.phase,
        }


@dataclass(frozen=True)
class _BatchRuntime:
    pipeline: BatchPipeline
    retry_service: BatchRetryService | None
    coordinator: TaskExecutionCoordinator | None


class TaskHierarchyService:
    def __init__(
        self,
        *,
        task_store: TaskStore,
        batch_store: StateBatchStore,
        history_store: object | None = None,
        batch_pipeline: BatchPipeline | None = None,
        batch_retry_service: BatchRetryService | None = None,
        execution_coordinator: TaskExecutionCoordinator | None = None,
        scheduler: object | None = None,
    ) -> None:
        self._task_store = task_store
        self._batch_store = batch_store
        self._batch_pipeline = batch_pipeline
        self._batch_retry_service = batch_retry_service
        self._execution_coordinator = execution_coordinator
        self._runtime_lock = RLock()
        self._batch_runtimes: dict[str | BatchPipeline, _BatchRuntime] = {}
        self._standalone_runtimes: set[str] = set()
        self._scheduler = scheduler
        self._byte_progress_by_asset: dict[str, tuple[int, int | None]] = {}
        self._byte_progress_by_task: dict[str, dict[str, tuple[int, int | None]]] = {}
        self._standalone_lifecycle = TaskLifecycleService(
            task_store=task_store,
        )

    def configure_batch_runtime(
        self,
        *,
        pipeline: BatchPipeline,
        retry_service: BatchRetryService | None,
        execution_coordinator: TaskExecutionCoordinator | None = None,
        task_id: str | None = None,
    ) -> None:
        with self._runtime_lock:
            key = task_id if task_id is not None else pipeline
            current = self._batch_runtimes.get(key)
            if current is not None and current.pipeline is not pipeline:
                raise InvalidTaskTransitionError(
                    f"batch already has an execution runtime: {task_id}"
                )
            self._batch_runtimes[key] = _BatchRuntime(
                pipeline, retry_service, execution_coordinator
            )

    def release_batch_runtime(self, task_id: str, pipeline: BatchPipeline) -> None:
        """A finishing worker cannot unregister a newer execution of the same task."""
        with self._runtime_lock:
            for key in (task_id, pipeline):
                current = self._batch_runtimes.get(key)
                if current is not None and current.pipeline is pipeline:
                    del self._batch_runtimes[key]

    def _batch_runtime(self, task_id: str) -> _BatchRuntime | None:
        with self._runtime_lock:
            runtime = self._batch_runtimes.get(task_id)
            candidates = tuple(self._batch_runtimes.values())
        if runtime is not None:
            return runtime
        for candidate in candidates:
            if candidate.pipeline.is_running(task_id):
                return candidate
        if self._batch_pipeline is not None:
            return _BatchRuntime(
                self._batch_pipeline, self._batch_retry_service, self._execution_coordinator
            )
        return None

    def register_standalone_runtime(self, task_id: str) -> None:
        with self._runtime_lock:
            if task_id in self._standalone_runtimes:
                raise InvalidTaskTransitionError(f"task already running: {task_id}")
            self._standalone_runtimes.add(task_id)

    def release_standalone_runtime(self, task_id: str) -> None:
        with self._runtime_lock:
            self._standalone_runtimes.discard(task_id)

    def is_running(self, task_id: str) -> bool:
        with self._runtime_lock:
            if task_id in self._standalone_runtimes or task_id in self._batch_runtimes:
                return True
        return self.is_batch_running(task_id)

    def list_roots(self) -> tuple[TaskNode, ...]:
        return tuple(summary.to_task_node() for summary in self.list_root_summaries())

    def get_root_summary(self, task_id: str) -> TaskNode:
        """Read control identity and state without expanding a Batch graph."""
        self._reject_batch_item_control(task_id)
        record = self._task_store.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        if record.kind is TaskKind.BATCH:
            return self._batch_root_summary(task_id, record=record).to_task_node()
        return self._standalone_root_summary(record).to_task_node()

    def list_root_summaries(self) -> tuple[TaskRootSummary, ...]:
        """Build the root projection without materialising Batch children."""

        records = self._task_store.list()
        parent_ids = self._batch_store.list_parent_ids()
        item_ids = tuple(record.task_id for record in records if record.kind is TaskKind.ITEM)
        internal_entry_ids = self._batch_store.find_entry_ids(item_ids)
        scheduler = self._scheduler_projection()
        standalone = tuple(
            self._standalone_root_summary(record, scheduler=scheduler)
            for record in records
            if record.kind is TaskKind.ITEM
            and record.task_id not in internal_entry_ids
            and record.task_id not in parent_ids
        )
        batches = tuple(
            self._batch_root_summary(parent_id, scheduler=scheduler) for parent_id in parent_ids
        )
        return (*standalone, *batches)

    def list_root_page(self, *, offset: int = 0, limit: int = 100) -> TaskRootPage:
        """Newest roots first, with bounded row decoding and bulk aggregates."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 500)
        if not _uses_shared_state_stores(self._task_store, self._batch_store):
            items = tuple(reversed(self.list_root_summaries()))
            return TaskRootPage(items[offset : offset + limit], len(items), offset, limit)
        records, total = self._task_store.list_root_page(offset=offset, limit=limit)
        summaries = self._batch_store.get_summaries(
            tuple(record.task_id for record in records if record.kind is TaskKind.BATCH)
        )
        scheduler = self._scheduler_projection()
        items = tuple(
            self._batch_root_summary(
                record.task_id,
                record=record,
                summary=summaries[record.task_id],
                scheduler=scheduler,
            )
            if record.kind is TaskKind.BATCH
            else self._standalone_root_summary(record, scheduler=scheduler)
            for record in records
        )
        return TaskRootPage(items, total, offset, limit)

    def list_children(self, parent_id: str) -> tuple[TaskNode, ...]:
        if parent_id not in self._batch_store.list_parent_ids():
            raise TaskNotFoundError(f"task not found: {parent_id}")
        entries = self._batch_store.list_entries(parent_id)
        return tuple(self._entry_node(entry) for entry in entries)

    def list_child_summaries(
        self,
        parent_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> BatchChildPage:
        """Return a bounded child page suitable for the Tasks table."""

        page = self._batch_store.list_child_summaries(
            parent_id,
            offset=offset,
            limit=limit,
        )
        scheduler = self._scheduler_projection()
        items = tuple(
            self._with_child_scheduler_detail(summary, sequence=offset + index, scheduler=scheduler)
            for index, summary in enumerate(page.items)
        )
        return BatchChildPage(
            items=items,
            total_count=page.total_count,
            offset=page.offset,
            limit=page.limit,
        )

    def get(self, task_id: str) -> TaskNode:
        entry = self._batch_store.get_entry(task_id)
        if entry is not None:
            return self._entry_node(entry)
        if task_id in self._batch_store.list_parent_ids():
            return self._batch_node(task_id)
        record = self._task_store.get(task_id)
        if record is not None:
            if record.kind is TaskKind.ITEM:
                return self._standalone_node(record)
            raise TaskNotFoundError(f"batch execution not found: {task_id}")
        raise TaskNotFoundError(f"task not found: {task_id}")

    def get_available_folder(self, task_id: str) -> str | None:
        """Resolve a persisted folder for one selected Task using bounded reads."""

        record = self._task_store.get(task_id)
        if record is not None and record.kind is TaskKind.ITEM:
            folder = _folder_from_task_metadata(record.metadata)
            if folder is not None:
                return folder
        return self._batch_store.find_available_folder(task_id)

    def list_files(self, task_id: str) -> tuple[TaskFileNode, ...]:
        entry = self._batch_store.get_entry(task_id)
        if entry is not None:
            if entry.child_id is None:
                return ()
            return tuple(
                self._apply_file_bytes(
                    TaskFileNode(
                        file_id=record.asset.asset_id,
                        name=record.asset.filename or record.asset.plugin_asset_key,
                        state=record.status.value,
                        attempts=record.attempt_count,
                        location=record.final_path or record.partial_path,
                        failure=record.failure.to_dict() if record.failure is not None else None,
                    )
                )
                for record in self._batch_store.list_assets(entry.child_id)
            )
        record = self._task_store.get(task_id)
        if record is not None and record.kind is TaskKind.ITEM:
            assets = record.metadata.get("assets")
            if isinstance(assets, list) and assets:
                output_files = record.metadata.get("output_files", [])
                completed_count = len(output_files)
                return tuple(
                    self._apply_file_bytes(
                        TaskFileNode(
                            file_id=f"{task_id}:{a.get('url')}",
                            name=a.get("filename") or str(i),
                            state=(
                                AssetStatus.COMPLETED.value
                                if i < completed_count
                                else (
                                    AssetStatus.DOWNLOADING.value
                                    if i == completed_count and record.status.value == "running"
                                    else AssetStatus.QUEUED.value
                                )
                            ),
                            attempts=0,
                            location=output_files[i] if i < completed_count else None,
                        )
                    )
                    for i, a in enumerate(assets)
                )

            # Fallback if no assets were saved (older tasks)
            return tuple(
                TaskFileNode(
                    file_id=f"{task_id}:{index}",
                    name=path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
                    state=AssetStatus.COMPLETED.value,
                    location=path,
                )
                for index, path in enumerate(
                    self._standalone_node(record).output_files,
                    start=1,
                )
            )
        raise TaskNotFoundError(f"task not found: {task_id}")

    def _apply_file_bytes(self, node: TaskFileNode) -> TaskFileNode:
        if node.file_id in self._byte_progress_by_asset:
            completed, total = self._byte_progress_by_asset[node.file_id]
            # Replace frozen dataclass instance
            return TaskFileNode(
                file_id=node.file_id,
                name=node.name,
                state=node.state,
                attempts=node.attempts,
                location=node.location,
                failure=node.failure,
                completed_bytes=completed,
                total_bytes=total,
            )
        return node

    def delete(self, task_id: str) -> None:
        if self._batch_store.get_entry(task_id) is not None:
            raise InvalidTaskTransitionError(
                "Batch item deletion is unavailable; delete the Batch task instead"
            )
        if task_id in self._batch_store.list_parent_ids():
            if _uses_shared_state_stores(self._task_store, self._batch_store):
                self._batch_store.delete_task_and_batch(task_id)
            else:
                self._batch_store.delete_batch(task_id)
                self._task_store.delete(task_id)
            return
        record = self._task_store.get(task_id)
        if record is not None and record.kind is TaskKind.ITEM:
            self._task_store.delete(task_id)
        elif record is not None and record.kind is TaskKind.BATCH:
            self._task_store.delete(task_id)
        else:
            raise TaskNotFoundError(f"task not found: {task_id}")

    def pause(self, task_id: str) -> object:
        self._reject_batch_item_control(task_id)
        if task_id in self._batch_store.list_parent_ids():
            return self._batch_control("pause", task_id)
        record = self._task_store.get(task_id)
        if record is not None and record.kind is TaskKind.ITEM:
            self._scheduler_root_control("suspend", task_id)
            waiting = getattr(self._scheduler, "waiting_snapshot_sync", lambda: ())()
            queued = any(row.get("root_id") == task_id for row in waiting)
            try:
                result = self._standalone_lifecycle.pause(
                    task_id, request_only=self.is_running(task_id) and not queued
                )
            except InvalidTaskTransitionError:
                current = self._task_store.get(task_id)
                if (
                    current is None
                    or current.status in _TERMINAL_TASK_STATUSES
                    or current.desired_control is not TaskDesiredControl.PAUSE
                ):
                    self._scheduler_root_control("resume", task_id)
                raise
            return result
        raise TaskNotFoundError(f"task not found: {task_id}")

    def resume(self, task_id: str) -> object:
        self._reject_batch_item_control(task_id)
        if task_id in self._batch_store.list_parent_ids():
            return self._batch_control("resume", task_id)
        record = self._task_store.get(task_id)
        if record is not None and record.kind is TaskKind.ITEM:
            result = self._standalone_lifecycle.resume(task_id)
            self._scheduler_root_control("resume", task_id)
            return result
        raise TaskNotFoundError(f"task not found: {task_id}")

    def cancel(self, task_id: str) -> object:
        self._reject_batch_item_control(task_id)
        if task_id in self._batch_store.list_parent_ids():
            return self._batch_control("cancel", task_id)
        record = self._task_store.get(task_id)
        if record is not None and record.kind is TaskKind.ITEM:
            result = self._standalone_lifecycle.cancel(task_id)
            self._scheduler_root_control("cancel", task_id)
            return result
        raise TaskNotFoundError(f"task not found: {task_id}")

    def wait_for_control_applied(
        self,
        task_id: str,
        action: str,
        *,
        timeout: float = 2.0,
    ) -> TaskControlReceipt:
        """Wait for a bounded, durable Task state transition after a control request."""

        if action not in {"pause", "resume", "cancel"}:
            raise ValueError(f"unsupported task control action: {action}")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        expected = {
            "pause": TaskDesiredControl.PAUSE,
            "resume": TaskDesiredControl.RUN,
            "cancel": TaskDesiredControl.CANCEL,
        }[action]
        deadline = monotonic() + timeout
        while True:
            record = self._task_store.get(task_id)
            if record is None:
                raise TaskNotFoundError(f"task not found: {task_id}")
            applied = _control_is_applied(record, action)
            if applied or monotonic() >= deadline:
                return TaskControlReceipt(
                    task_id=task_id,
                    action=action,
                    requested=record.desired_control is expected,
                    applied=applied,
                    actual_status=record.status,
                    desired_control=record.desired_control,
                    revision=record.revision,
                    quiescent=action == "pause" and record.status is TaskStatus.PAUSED,
                    phase="applied" if applied else "pending",
                )
            sleep(0.02)

    async def retry(self, task_id: str) -> object:
        self._reject_batch_item_control(task_id)
        if task_id in self._batch_store.list_parent_ids():
            runtime = self._batch_runtime(task_id)
            if runtime is None or runtime.retry_service is None:
                raise InvalidTaskTransitionError("batch retry is unavailable")
            return await runtime.retry_service.retry_parent(
                task_id,
                pipeline=runtime.pipeline,
            )
        record = self._task_store.get(task_id)
        if record is not None and record.kind is TaskKind.ITEM:
            return self._standalone_lifecycle.retry(task_id)
        raise TaskNotFoundError(f"task not found: {task_id}")

    def is_batch_running(self, task_id: str) -> bool:
        """Whether this service currently owns the Batch's active Pipeline run."""
        runtime = self._batch_runtime(task_id)
        return runtime is not None and runtime.pipeline.is_running(task_id)

    def _batch_control(self, action: str, task_id: str) -> bool:
        if task_id not in self._batch_store.list_parent_ids():
            raise TaskNotFoundError(f"batch not found: {task_id}")

        runtime = self._batch_runtime(task_id)
        pipeline = runtime.pipeline if runtime is not None else None
        coordinator = runtime.coordinator if runtime is not None else None
        if coordinator is not None and pipeline is not None and pipeline.is_running(task_id):
            desired_control = {
                "pause": TaskDesiredControl.PAUSE,
                "resume": TaskDesiredControl.RUN,
                "cancel": TaskDesiredControl.CANCEL,
            }[action]
            try:
                coordinator.request_control_sync(desired_control, task_id)
            except InvalidTaskTransitionError:
                # The pipeline may finish between is_running() and the
                # coordinator CAS. A late UI click must not turn a completed
                # Batch into a shell/background crash.
                record = self._task_store.get(task_id)
                if record is not None and record.status in _TERMINAL_TASK_STATUSES:
                    return record
                raise
            if getattr(pipeline, action)(task_id):
                return True
            record = self._task_store.get(task_id)
            if record is not None and record.status in _TERMINAL_TASK_STATUSES:
                return record
            raise InvalidTaskTransitionError(f"cannot {action} active batch task {task_id}")

        # Always update Task DB (authoritative pause/resume/cancel source).
        record = self._task_store.get(task_id)
        if record is not None and record.active_run_id is not None:
            raise InvalidTaskTransitionError("active batch controls require its execution owner")
        if action == "pause":
            if record is not None:
                self._task_store.upsert(
                    record.transition(
                        status=TaskStatus.PAUSED,
                        kind=TaskKind.BATCH,
                        desired_control=TaskDesiredControl.PAUSE,
                    )
                )
            else:
                self._task_store.upsert(
                    TaskRecord(
                        task_id=task_id,
                        status=TaskStatus.PAUSED,
                        kind=TaskKind.BATCH,
                        desired_control=TaskDesiredControl.PAUSE,
                    )
                )
        elif record is not None and action == "resume":
            self._task_store.upsert(
                record.transition(
                    status=TaskStatus.PLANNED,
                    kind=TaskKind.BATCH,
                    desired_control=TaskDesiredControl.RUN,
                )
            )
        elif record is not None and action == "cancel":
            self._task_store.upsert(
                record.transition(
                    status=TaskStatus.CANCELED,
                    kind=TaskKind.BATCH,
                    desired_control=TaskDesiredControl.CANCEL,
                )
            )

        if pipeline is not None and getattr(pipeline, action)(task_id):
            return True

        # Inactive parents preserve the execution graph. Only a worker may
        # record entry/child/asset outcomes; resume/retry reads these rows.
        return True

    def _reject_batch_item_control(self, task_id: str) -> None:
        if self._batch_store.get_entry(task_id) is not None:
            raise InvalidTaskTransitionError(
                "Batch item controls are unavailable; control the Batch task instead"
            )

    def _scheduler_root_control(self, action: str, task_id: str) -> None:
        method = getattr(self._scheduler, f"{action}_root_sync", None)
        if callable(method):
            method(task_id)

    def _batch_root_summary(
        self,
        parent_id: str,
        *,
        record: TaskRecord | None = None,
        summary: BatchSummary | None = None,
        scheduler: SchedulerProjection | None = None,
    ) -> TaskRootSummary:
        summary = summary if summary is not None else self._batch_store.get_summary(parent_id)
        record = record if record is not None else self._task_store.get(parent_id)
        if record is None or record.kind is not TaskKind.BATCH:
            raise TaskNotFoundError(f"batch task record not found: {parent_id}")
        return TaskRootSummary(
            task_id=parent_id,
            kind=TaskKind.BATCH,
            state=_task_state_for_record(record),
            title=summary.title or parent_id[:8],
            progress=TaskProgress(
                completed_files=summary.completed_files,
                total_files=summary.total_files,
                preparing_items=(
                    summary.counts.unresolved + summary.counts.resolving + summary.counts.queued
                ),
                completed_bytes=summary.completed_bytes,
                total_bytes=summary.total_bytes,
            ),
            site_id=summary.site_id,
            source_url=summary.source_url,
            details={
                "counts": summary.counts.to_dict(),
                "created_at": record.created_at.astimezone().isoformat(),
                "revision": record.revision,
                **self._scheduler_detail(parent_id, projection=scheduler),
            },
        )

    def _standalone_root_summary(
        self, record: TaskRecord, *, scheduler: SchedulerProjection | None = None
    ) -> TaskRootSummary:
        node = self._standalone_node(record, scheduler=scheduler)
        compact_details: dict[str, object] = {
            "created_at": record.created_at.astimezone().isoformat(),
            "revision": record.revision,
        }
        for key in (
            "failure",
            "scheduler_active",
            "scheduler_waiting_reason",
            "scheduler_queue_position",
            "scheduler_site_id",
        ):
            if key in node.details:
                compact_details[key] = node.details[key]
        return TaskRootSummary(
            task_id=node.task_id,
            kind=node.kind,
            state=node.state,
            title=node.title,
            progress=node.progress,
            site_id=node.site_id,
            source_url=node.source_url,
            details=compact_details,
        )

    def _batch_node(self, parent_id: str) -> TaskNode:
        """Build a full Batch detail node for selected-task views only."""

        snapshot = self._batch_store.get_snapshot(parent_id)
        entries = self._batch_store.list_entries(parent_id)
        child_nodes = tuple(self._entry_node(entry) for entry in entries)
        known_totals = tuple(
            child.progress.total_files
            for child in child_nodes
            if child.progress.total_files is not None
        )
        child_byte_totals = tuple(child.progress.total_bytes for child in child_nodes)

        progress = TaskProgress(
            completed_files=sum(child.progress.completed_files for child in child_nodes),
            total_files=sum(known_totals),
            preparing_items=snapshot.counts.unresolved
            + snapshot.counts.resolving
            + snapshot.counts.queued,
            completed_bytes=sum(child.progress.completed_bytes for child in child_nodes),
            total_bytes=(
                sum(total for total in child_byte_totals if total is not None)
                if child_byte_totals and all(total is not None for total in child_byte_totals)
                else None
            ),
        )

        site_id = None
        source_url = None
        output_files: tuple[str, ...] = ()

        if entries:
            first = entries[0]
            site_id = first.site_id
            source_url = first.source_url
            if first.child_id is not None:
                child = self._batch_store.get_child(first.child_id)
                if child is not None:
                    assets = self._batch_store.list_assets(child.child_id)
                    output_files = tuple(
                        asset.final_path
                        for asset in assets
                        if asset.status is AssetStatus.COMPLETED and asset.final_path is not None
                    )

        return TaskNode(
            task_id=parent_id,
            kind=TaskKind.BATCH,
            state=self._batch_task_state(parent_id),
            title=snapshot.parent.title or parent_id[:8],
            progress=progress,
            site_id=site_id,
            source_url=source_url,
            output_files=output_files,
            details={
                "counts": snapshot.counts.to_dict(),
                "created_at": self._task_created_at(parent_id),
                **self._scheduler_detail(parent_id),
            },
        )

    def _with_child_scheduler_detail(
        self,
        summary: BatchChildSummary,
        *,
        sequence: int,
        scheduler: SchedulerProjection | None = None,
    ) -> BatchChildSummary:
        details = dict(summary.details)
        details.update(
            self._scheduler_detail(summary.parent_id, sequence=sequence, projection=scheduler)
        )
        return BatchChildSummary(
            task_id=summary.task_id,
            parent_id=summary.parent_id,
            state=summary.state,
            title=summary.title,
            progress=summary.progress,
            site_id=summary.site_id,
            source_url=summary.source_url,
            details=details,
        )

    def _entry_node(self, entry: BatchEntry) -> TaskNode:
        child = self._batch_store.get_child(entry.child_id) if entry.child_id is not None else None
        if child is None:
            state = {
                BatchEntryStatus.PENDING_RESOLUTION: TaskState.PREPARING,
                BatchEntryStatus.RESOLVING: TaskState.PREPARING,
                BatchEntryStatus.RESOLUTION_FAILED: TaskState.FAILED,
                BatchEntryStatus.PACKAGE_REJECTED: TaskState.FAILED,
                BatchEntryStatus.DUPLICATE_SKIPPED: TaskState.SKIPPED,
                BatchEntryStatus.CANCELLED: TaskState.CANCELLED,
            }[entry.status]
            progress = TaskProgress(preparing_items=int(state is TaskState.PREPARING))
            output_files: tuple[str, ...] = ()
            details = {"preparation_status": entry.status.value}
        else:
            assets = self._batch_store.list_assets(child.child_id)
            completed = tuple(asset for asset in assets if asset.status is AssetStatus.COMPLETED)
            state = _CHILD_STATES[child.status]
            progress = TaskProgress(completed_files=len(completed), total_files=len(assets))
            output_files = tuple(
                asset.final_path for asset in completed if asset.final_path is not None
            )
            details = {
                "preparation_status": entry.status.value,
                "child_id": child.child_id,
                "package_id": child.package.package_id,
                "package_version": child.package.version,
                "attempt_count": len(self._batch_store.list_attempts(child.child_id)),
                **self._scheduler_detail(entry.parent_id, sequence=self._entry_sequence(entry)),
            }
        failure = entry.failure or (child.failure if child is not None else None)
        if failure is not None:
            details["failure"] = failure.to_dict()
        node = TaskNode(
            task_id=entry.entry_id,
            kind=TaskKind.ITEM,
            state=state,
            title=entry.title or entry.candidate_id,
            progress=progress,
            parent_id=entry.parent_id,
            site_id=entry.site_id,
            output_files=output_files,
            details=details,
        )
        return self._apply_node_bytes(node, entry.child_id)

    def _apply_node_bytes(self, node: TaskNode, child_id: str | None) -> TaskNode:
        if not child_id:
            return node

        assets = self._batch_store.list_assets(child_id)
        total_b = 0
        completed_b = 0
        has_total = True

        import os

        has_any = False
        for record in assets:
            checkpoint = record.metadata.get("torrent_checkpoint")
            live_key = record.asset.asset_id

            if live_key in self._byte_progress_by_asset:
                has_any = True
                c, t = self._byte_progress_by_asset[live_key]
                completed_b += c
                if t is not None:
                    total_b += t
                else:
                    has_total = False
            elif isinstance(checkpoint, Mapping):
                c, t = checkpoint_byte_progress(checkpoint)
                has_any = True
                completed_b += c
                if t is not None:
                    total_b += t
                else:
                    has_total = False
            elif record.status is AssetStatus.COMPLETED and record.final_path is not None:
                try:
                    size = os.path.getsize(record.final_path)
                    has_any = True
                    completed_b += size
                    total_b += size
                except OSError:
                    has_total = False
            else:
                has_total = False

        if has_any:
            return TaskNode(
                task_id=node.task_id,
                kind=node.kind,
                state=node.state,
                title=node.title,
                progress=TaskProgress(
                    completed_files=node.progress.completed_files,
                    total_files=node.progress.total_files,
                    preparing_items=node.progress.preparing_items,
                    completed_bytes=completed_b,
                    total_bytes=total_b if has_total else None,
                ),
                parent_id=node.parent_id,
                site_id=node.site_id,
                source_url=node.source_url,
                output_files=node.output_files,
                details=node.details,
            )
        return node

    def _batch_task_state(self, task_id: str) -> TaskState:
        record = self._task_store.get(task_id)
        if record is None or record.kind is not TaskKind.BATCH:
            raise TaskNotFoundError(f"batch task record not found: {task_id}")
        return _task_state_for_record(record)

    def _standalone_node(
        self, record: TaskRecord, *, scheduler: SchedulerProjection | None = None
    ) -> TaskNode:
        output_files = tuple(
            value for value in record.metadata.get("output_files", ()) if isinstance(value, str)
        )
        total = _non_negative_int(record.metadata.get("total_files"))
        if total is None:
            total = _non_negative_int(record.metadata.get("asset_count"))
        completed = _non_negative_int(record.metadata.get("completed_files"))
        if completed is None:
            completed = len(output_files)
        if total is not None:
            completed = min(completed, total)

        total_b = 0
        completed_b = 0
        has_total = True
        has_any = False
        with self._runtime_lock:
            live_progress = dict(self._byte_progress_by_task.get(record.task_id, {}))
        checkpoints = record.metadata.get("torrent_checkpoints", {})
        if isinstance(checkpoints, Mapping):
            for checkpoint in checkpoints.values():
                if not isinstance(checkpoint, Mapping):
                    continue
                client_id = checkpoint.get("client_id")
                live_key = (
                    f"{record.task_id}:torrent:{client_id}" if isinstance(client_id, str) else None
                )
                if live_key is not None and live_key in live_progress:
                    c, t = live_progress[live_key]
                    has_any = True
                    completed_b += c
                    if t is not None:
                        total_b += t
                    else:
                        has_total = False
                elif isinstance(checkpoint, Mapping):
                    c, t = checkpoint_byte_progress(checkpoint)
                    has_any = True
                    completed_b += c
                    if t is not None:
                        total_b += t
                    else:
                        has_total = False

        for key, (c, t) in live_progress.items():
            if ":torrent:" not in key:
                has_any = True
                completed_b += c
                if t is not None:
                    total_b += t
                else:
                    has_total = False

        return TaskNode(
            task_id=record.task_id,
            kind=TaskKind.ITEM,
            state=_task_state_for_record(record),
            title=record.title or record.task_id,
            progress=TaskProgress(
                completed_files=completed,
                total_files=total,
                completed_bytes=completed_b if has_any else 0,
                total_bytes=total_b if has_any and has_total else None,
            ),
            site_id=record.site_id,
            source_url=record.source_url,
            output_files=output_files,
            details={
                **record.metadata,
                "created_at": record.created_at.astimezone().isoformat(),
                **self._scheduler_detail(record.task_id, projection=scheduler),
            },
        )

    def _entry_sequence(self, entry: object) -> int:
        parent_id = getattr(entry, "parent_id", None)
        entry_id = getattr(entry, "entry_id", None)
        if not isinstance(parent_id, str) or not isinstance(entry_id, str):
            return 0
        for sequence, current in enumerate(self._batch_store.list_entries(parent_id)):
            if current.entry_id == entry_id:
                return sequence
        return 0

    def _scheduler_projection(self) -> SchedulerProjection:
        projection = {}
        # Active rows win over queued siblings for a Batch root.
        for active, name in ((True, "active_snapshot_sync"), (False, "waiting_snapshot_sync")):
            method = getattr(self._scheduler, name, None)
            if not callable(method):
                continue
            try:
                rows = method()
            except Exception:
                continue
            for row in rows:
                if not isinstance(row, Mapping) or not isinstance(row.get("root_id"), str):
                    continue
                details = {"scheduler_active": active, "scheduler_site_id": row.get("site_id")}
                if not active:
                    details.update(
                        scheduler_waiting_reason=row.get("reason"),
                        scheduler_queue_position=row.get("position"),
                    )
                projection.setdefault((row["root_id"], None), details)
                projection.setdefault((row["root_id"], row.get("sequence")), details)
        return projection

    def _scheduler_detail(
        self,
        root_id: str,
        *,
        sequence: int | None = None,
        projection: SchedulerProjection | None = None,
    ) -> dict[str, object]:
        if projection is None:
            projection = self._scheduler_projection()
        return projection.get((root_id, sequence), {})

    def _task_created_at(self, task_id: str) -> str | None:
        record = self._task_store.get(task_id)
        return record.created_at.astimezone().isoformat() if record is not None else None

    def handle_progress_event(self, reason: str, payload: Mapping[str, object]) -> None:
        actual_reason = reason
        if reason == "batch_progress":
            actual_reason = str(payload.get("progress_reason", ""))

        if actual_reason in {"byte_progress", "torrent_progress"}:
            asset_id = payload.get("asset_id")
            bytes_received = payload.get("bytes_received", payload.get("completed_bytes"))
            total_bytes = payload.get("total_bytes")
            if isinstance(asset_id, str):
                completed, total = normalise_byte_progress(bytes_received, total_bytes)
                with self._runtime_lock:
                    self._byte_progress_by_asset[asset_id] = (completed, total)
                    task_id = asset_id.split(":", 1)[0]
                    self._byte_progress_by_task.setdefault(task_id, {})[asset_id] = (
                        completed,
                        total,
                    )


def _uses_shared_state_stores(task_store: object, batch_store: object) -> bool:
    return (
        isinstance(task_store, StateTaskStore)
        and isinstance(batch_store, StateBatchStore)
        and task_store.database.path == batch_store.database.path
    )


def _control_is_applied(record: TaskRecord, action: str) -> bool:
    if action == "pause":
        return (
            record.status is TaskStatus.PAUSED
            and record.desired_control is TaskDesiredControl.PAUSE
        )
    if action == "resume":
        return record.desired_control is TaskDesiredControl.RUN and record.status in {
            TaskStatus.PLANNED,
            TaskStatus.RUNNING,
        }
    return (
        record.desired_control is TaskDesiredControl.CANCEL
        and record.status in _TERMINAL_TASK_STATUSES
    )


def _task_state_for_record(record: TaskRecord) -> TaskState:
    if record.status is TaskStatus.RUNNING and record.desired_control is TaskDesiredControl.PAUSE:
        return TaskState.PAUSING
    return _STANDALONE_STATES[record.status]


def _folder_from_task_metadata(metadata: Mapping[str, object]) -> str | None:
    output_files = metadata.get("output_files")
    if isinstance(output_files, (list, tuple)):
        path = next(
            (value for value in output_files if isinstance(value, str) and value),
            None,
        )
        if path is not None:
            return str(Path(path).parent)
    checkpoints = metadata.get("torrent_checkpoints")
    if isinstance(checkpoints, Mapping):
        for checkpoint in checkpoints.values():
            if not isinstance(checkpoint, Mapping):
                continue
            output_dir = checkpoint.get("output_dir")
            if isinstance(output_dir, str) and output_dir:
                return output_dir
    return None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
