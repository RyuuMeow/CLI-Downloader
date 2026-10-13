from __future__ import annotations

import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from cli_downloader.application.download_limiter import (
    GalleryDownloadLimiter,
    suspend_current_request,
    resume_current_request,
)
from cli_downloader.application.execution_control import OwnerShutdownRequested
from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchSnapshot,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureOrigin,
    FailureStage,
    RetryPlan,
)
from cli_downloader.core.errors import BatchConflictError
from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.storage.completion_store import CompletionStore
from cli_downloader.storage.history_store import HistoryStore
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.task_store import TaskStatus


ProgressReporter = Callable[[str, dict[str, object]], None]
EventSink = Callable[[TaskEvent], None]
ChildIdFactory = Callable[[BatchEntry, DownloadPackage], str]


class BatchControlSignal(Protocol):
    def is_set(self) -> bool: ...

    def is_paused(self) -> bool: ...

    async def wait(self) -> None: ...

    async def wait_if_paused(self) -> None: ...


class BatchPackageResolver(Protocol):
    async def resolve(
        self,
        entry: BatchEntry,
        cancel_event: BatchControlSignal,
        progress: ProgressReporter,
    ) -> DownloadPackage: ...


class BatchDownloadExecutor(Protocol):
    async def execute(
        self,
        child: DownloadChild,
        assets: tuple[AssetRecord, ...],
        cancel_event: BatchControlSignal,
        progress: ProgressReporter,
    ) -> tuple[AssetRecord, ...]: ...


@dataclass(frozen=True)
class BatchQueueConfig:
    resolution_workers: int = 2
    download_workers: int = 2
    resolution_queue_size: int = 16
    download_queue_size: int = 8
    site_resolution_limits: Mapping[str, int] = field(default_factory=dict)
    site_download_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "resolution_workers",
            "download_workers",
            "resolution_queue_size",
            "download_queue_size",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("site_resolution_limits", "site_download_limits"):
            values = dict(getattr(self, name))
            if any(
                not isinstance(site_id, str)
                or not site_id
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit <= 0
                for site_id, limit in values.items()
            ):
                raise ValueError(f"{name} must map site ids to positive integers")
            object.__setattr__(self, name, MappingProxyType(values))


@dataclass(frozen=True)
class BatchRunResult:
    snapshot: BatchSnapshot
    events: tuple[TaskEvent, ...]
    resolution_queue_peak: int
    download_queue_peak: int
    task_status: TaskStatus | None = None
    stopped: bool = False


@dataclass
class _RunState:
    run_id: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    events: list[TaskEvent] = field(default_factory=list)
    resolution_waiting: int = 0
    download_waiting: int = 0
    resolution_peak: int = 0
    download_peak: int = 0
    worker_error: Exception | None = None
    retry_plans: dict[str, RetryPlan] = field(default_factory=dict)
    paused_ids: set[str] = field(default_factory=set)
    cancelled_ids: set[str] = field(default_factory=set)
    resume_events: dict[str, asyncio.Event] = field(default_factory=dict)
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)
    loop: asyncio.AbstractEventLoop | None = None
    control_acknowledgements: asyncio.Queue[tuple[str, str]] | None = None
    worker_boundaries: dict[str, bool] = field(default_factory=dict)
    pause_acknowledged: bool = False
    on_pause_acknowledged: Callable[[], None] | None = None


@dataclass(frozen=True)
class _TaskControl:
    state: _RunState
    parent_id: str
    task_id: str
    worker_id: str | None = None
    pause_boundary: Callable[[], Awaitable[None]] | None = None
    resume_boundary: Callable[[], Awaitable[None]] | None = None
    admission_release: Callable[[], Awaitable[None]] | None = None

    def is_set(self) -> bool:
        return self.state.cancel_event.is_set() or self.task_id in self.state.cancelled_ids

    def is_shutdown_requested(self) -> bool:
        return self.state.shutdown_event.is_set()

    def is_paused(self) -> bool:
        return self.parent_id in self.state.paused_ids or self.task_id in self.state.paused_ids

    async def wait(self) -> None:
        if self.is_set():
            return
        task_event = self.state.cancel_events.setdefault(self.task_id, asyncio.Event())
        waits = {
            asyncio.create_task(self.state.cancel_event.wait()),
            asyncio.create_task(task_event.wait()),
        }
        done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    async def wait_if_paused(self) -> None:
        if self.is_shutdown_requested():
            await suspend_current_request()
            if self.pause_boundary is not None:
                await self.pause_boundary()
            raise OwnerShutdownRequested
        paused_targets = tuple(
            target_id
            for target_id in (self.parent_id, self.task_id)
            if target_id in self.state.paused_ids
        )
        if not paused_targets or self.is_set():
            return

        await suspend_current_request()
        if self.pause_boundary is not None:
            await self.pause_boundary()
        for target_id in paused_targets:
            if target_id == self.parent_id and self.worker_id is not None:
                _mark_worker_boundary(self.state, self.parent_id, self.worker_id, True)
            event = self.state.resume_events.setdefault(target_id, asyncio.Event())
            await event.wait()
            if target_id == self.parent_id and self.worker_id is not None:
                _mark_worker_boundary(self.state, self.parent_id, self.worker_id, False)
            if self.is_set():
                break
            if self.is_shutdown_requested():
                raise OwnerShutdownRequested
        if not self.is_set():
            if self.resume_boundary is not None:
                await self.resume_boundary()
            await resume_current_request()

    async def release_admission(self) -> None:
        if self.admission_release is not None:
            await self.admission_release()


class _ItemLeaseController:
    """Release and reacquire one central Item lease around pause checkpoints."""

    def __init__(
        self,
        limiter: GalleryDownloadLimiter,
        *,
        root_id: str,
        site_id: str,
        sequence: int,
        cancelled: Callable[[], bool],
    ) -> None:
        self._limiter = limiter
        self._root_id = root_id
        self._site_id = site_id
        self._sequence = sequence
        self._cancelled = cancelled
        self._lease = None
        self._detached = False

    async def acquire(self) -> None:
        if self._lease is None and not self._detached and not self._cancelled():
            self._lease = await self._limiter.acquire_item(
                root_id=self._root_id,
                site_id=self._site_id,
                sequence=self._sequence,
            )

    async def release(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            await lease.release()

    async def pause_boundary(self) -> None:
        await self.release()

    async def resume_boundary(self) -> None:
        await self.acquire()

    async def detach(self) -> None:
        self._detached = True
        await self.release()


class _SemaphoreLeaseController:
    """Make pipeline backpressure permits pause-safe and idempotently releasable."""

    def __init__(self, *semaphores: asyncio.Semaphore) -> None:
        self._semaphores = semaphores
        self._acquired = 0
        self._detached = False

    async def acquire(self) -> None:
        while not self._detached and self._acquired < len(self._semaphores):
            try:
                await self._semaphores[self._acquired].acquire()
            except BaseException:
                await self.release()
                raise
            self._acquired += 1

    async def release(self) -> None:
        while self._acquired:
            self._acquired -= 1
            self._semaphores[self._acquired].release()

    async def pause_boundary(self) -> None:
        await self.release()

    async def resume_boundary(self) -> None:
        await self.acquire()

    async def detach(self) -> None:
        self._detached = True
        await self.release()


async def _acquire_with_control(controller, control: _TaskControl) -> None:
    """Queue waits are interruptible boundaries, not active download work."""
    while True:
        await control.wait_if_paused()
        if control.is_set():
            raise asyncio.CancelledError
        pending = asyncio.create_task(controller.acquire())
        try:
            while not pending.done():
                if control.is_paused() or control.is_set() or control.is_shutdown_requested():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    await controller.release()
                    break
                await asyncio.wait({pending}, timeout=0.05)
            else:
                await pending
                return
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await controller.release()
            raise


def _compose_pause_boundaries(
    controllers: tuple[object, ...],
) -> tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]:
    async def pause() -> None:
        for controller in controllers:
            await controller.pause_boundary()

    async def resume() -> None:
        for controller in reversed(controllers):
            await controller.resume_boundary()

    return pause, resume


def _compose_admission_release(controllers: tuple[object, ...]) -> Callable[[], Awaitable[None]]:
    async def release() -> None:
        for controller in controllers:
            await controller.detach()

    return release


def _mark_worker_boundary(state: _RunState, parent_id: str, worker_id: str, is_safe: bool) -> None:
    state.worker_boundaries[worker_id] = is_safe
    _maybe_acknowledge_parent_pause(state, parent_id)


def _remove_worker_boundary(state: _RunState, parent_id: str, worker_id: str) -> None:
    state.worker_boundaries.pop(worker_id, None)
    _maybe_acknowledge_parent_pause(state, parent_id)


def _maybe_acknowledge_parent_pause(state: _RunState, parent_id: str) -> None:
    if (
        parent_id not in state.paused_ids
        or state.pause_acknowledged
        or not all(state.worker_boundaries.values())
    ):
        return
    state.pause_acknowledged = True
    if state.on_pause_acknowledged is not None:
        state.on_pause_acknowledged()
    queue = state.control_acknowledgements
    loop = state.loop
    if queue is not None and loop is not None:
        loop.call_soon_threadsafe(queue.put_nowait, ("pause", parent_id))


_CHILD_EVENT_REASON = {
    "child_completed": "task_completed",
    "child_partially_completed": "task_partially_completed",
    "child_failed": "task_failed",
    "child_cancelled": "task_cancelled",
}
# Events that control task state (pause/resume/cancel) — not download
# records, so they are NOT persisted to the history store.
_STATE_CONTROL_REASONS = frozenset(
    {
        "task_pause_requested",
        "task_pause_acknowledged",
        "task_resume_requested",
        "task_cancel_requested",
    }
)
_NON_DURABLE_PROGRESS_REASONS = frozenset({"torrent_added", "torrent_progress"})


def _uses_shared_state_terminal_stores(
    store: object,
    completion_store: object,
    history_store: object,
) -> bool:
    return (
        isinstance(store, StateBatchStore)
        and isinstance(completion_store, SQLiteCompletionStore)
        and isinstance(history_store, StateHistoryStore)
        and store.database.path == completion_store.database.path == history_store.database.path
    )


def _terminal_history_events(
    parent_id: str,
    state: _RunState,
    reason: str,
    payload: dict[str, object],
) -> tuple[TaskEvent, ...]:
    parent_payload = dict(payload)
    if state.run_id is not None:
        parent_payload["run_id"] = state.run_id
    parent_event = TaskEvent(
        task_id=parent_id,
        name="batch_progress",
        reason=reason,
        payload=parent_payload,
    )
    child_reason = _CHILD_EVENT_REASON.get(reason)
    child_id = payload.get("child_id")
    if child_reason is None or not isinstance(child_id, str):
        return (parent_event,)
    return (
        parent_event,
        TaskEvent(
            task_id=child_id,
            name="task_transitioned",
            reason=child_reason,
            payload={"batch_id": parent_id, **payload},
        ),
    )


class BatchPipeline:
    def __init__(
        self,
        *,
        store: StateBatchStore,
        resolver: BatchPackageResolver,
        downloader: BatchDownloadExecutor,
        config: BatchQueueConfig | None = None,
        history_store: HistoryStore | None = None,
        completion_store: CompletionStore | None = None,
        event_sink: EventSink | None = None,
        child_id_factory: ChildIdFactory | None = None,
        download_limiter: GalleryDownloadLimiter | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._downloader = downloader
        self._config = config or BatchQueueConfig(
            download_workers=download_limiter.max_concurrent_downloads if download_limiter else 2
        )
        self._history_store = history_store
        self._completion_store = completion_store
        self._event_sink = event_sink
        self._child_id_factory = child_id_factory or (lambda entry, package: uuid4().hex)
        self._download_limiter = download_limiter
        self._resolution_slots = asyncio.Semaphore(self._config.resolution_workers)
        self._download_slots = asyncio.Semaphore(self._config.download_workers)
        self._resolution_capacity = asyncio.Semaphore(self._config.resolution_queue_size)
        self._download_capacity = asyncio.Semaphore(self._config.download_queue_size)
        self._site_resolution_slots = {
            site_id: asyncio.Semaphore(limit)
            for site_id, limit in self._config.site_resolution_limits.items()
        }
        self._site_download_slots = {
            site_id: asyncio.Semaphore(limit)
            for site_id, limit in self._config.site_download_limits.items()
        }
        self._active: dict[str, _RunState] = {}

    def cancel(self, parent_id: str) -> bool:
        return self._dispatch_control(parent_id, self._cancel_on_owner_loop)

    def _cancel_on_owner_loop(self, parent_id: str) -> bool:
        active = self._active_task(parent_id)
        if active is None:
            return False
        root_id, state = active
        if parent_id == root_id:
            state.cancel_event.set()
            if self._download_limiter is not None:
                self._download_limiter.cancel_root_sync(root_id)
        else:
            state.cancelled_ids.add(parent_id)
            state.cancel_events.setdefault(parent_id, asyncio.Event()).set()
        self._release_pause(state, parent_id)
        self._emit(state, root_id, "task_cancel_requested", task_id=parent_id)
        return True

    def shutdown(self, parent_id: str) -> bool:
        """Stop an owner without converting active work into user cancellation."""
        return self._dispatch_control(parent_id, self._shutdown_on_owner_loop)

    def _shutdown_on_owner_loop(self, parent_id: str) -> bool:
        active = self._active_task(parent_id)
        if active is None:
            return False
        root_id, state = active
        state.shutdown_event.set()
        if self._download_limiter is not None:
            self._download_limiter.suspend_root_sync(root_id)
        self._release_pause(state, parent_id)
        self._emit(state, root_id, "task_shutdown_requested", task_id=parent_id)
        return True

    def pause(self, task_id: str) -> bool:
        return self._dispatch_control(task_id, self._pause_on_owner_loop)

    def _pause_on_owner_loop(self, task_id: str) -> bool:
        active = self._active_task(task_id)
        if active is None:
            return False
        root_id, state = active
        state.paused_ids.add(task_id)
        state.resume_events.setdefault(task_id, asyncio.Event()).clear()
        if task_id == root_id and self._download_limiter is not None:
            self._download_limiter.suspend_root_sync(root_id)
        self._emit(state, root_id, "task_pause_requested", task_id=task_id)
        if task_id == root_id:
            state.pause_acknowledged = False
            _maybe_acknowledge_parent_pause(state, root_id)
        return True

    def resume(self, task_id: str) -> bool:
        return self._dispatch_control(task_id, self._resume_on_owner_loop)

    def _resume_on_owner_loop(self, task_id: str) -> bool:
        active = self._active_task(task_id)
        if active is None:
            return False
        root_id, state = active
        if task_id not in state.paused_ids:
            return False
        self._release_pause(state, task_id)
        if task_id == root_id and self._download_limiter is not None:
            self._download_limiter.resume_root_sync(root_id)
        self._emit(state, root_id, "task_resume_requested", task_id=task_id)
        if task_id == root_id:
            queue = state.control_acknowledgements
            loop = state.loop
            if queue is not None and loop is not None:
                loop.call_soon_threadsafe(queue.put_nowait, ("resume", root_id))
        return True

    def is_paused(self, task_id: str) -> bool:
        active = self._active_task(task_id)
        if active is None:
            return False
        root_id, state = active
        return root_id in state.paused_ids or task_id in state.paused_ids

    def is_running(self, task_id: str) -> bool:
        """Return True if the task is currently active in the pipeline."""
        return self._active_task(task_id) is not None

    def _dispatch_control(
        self,
        task_id: str,
        operation: Callable[[str], bool],
    ) -> bool:
        """Run a control mutation on the Batch owner's asyncio loop.

        Textual callbacks and IPC handlers execute outside the loop that owns
        the Batch workers.  Mutating asyncio Events or the run-state sets from
        those threads is undefined and can leave a paused worker holding its
        admission lease.  Route the mutation through ``call_soon_threadsafe``
        and keep a bounded wait for the synchronous service contract.
        """

        active = self._active_task(task_id)
        if active is None:
            return False
        _root_id, state = active
        loop = state.loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if loop is None or not loop.is_running() or current_loop is loop:
            return operation(task_id)

        result: Future[bool] = Future()

        def invoke() -> None:
            if result.cancelled():
                return
            try:
                result.set_result(operation(task_id))
            except BaseException as error:
                result.set_exception(error)

        loop.call_soon_threadsafe(invoke)
        try:
            return bool(result.result(timeout=5.0))
        except FutureTimeoutError:
            result.cancel()
            return False

    def _active_task(self, task_id: str) -> tuple[str, _RunState] | None:
        state = self._active.get(task_id)
        if state is None:
            entry = self._store.get_entry(task_id)
            if entry is None:
                return None
            state = self._active.get(entry.parent_id)
            if state is None:
                return None
            return entry.parent_id, state
        return task_id, state

    @staticmethod
    def _release_pause(state: _RunState, task_id: str) -> None:
        state.paused_ids.discard(task_id)
        state.resume_events.setdefault(task_id, asyncio.Event()).set()

    async def run_batch(
        self,
        parent_id: str,
        *,
        retry_plans: tuple[RetryPlan, ...] = (),
        run_id: str | None = None,
        control_acknowledgements: asyncio.Queue[tuple[str, str]] | None = None,
    ) -> BatchRunResult:
        if parent_id in self._active:
            raise BatchConflictError(f"batch is already running: {parent_id}")
        state = _RunState(
            run_id=run_id,
            retry_plans={target_id: plan for plan in retry_plans for target_id in plan.target_ids},
            loop=asyncio.get_running_loop(),
            control_acknowledgements=control_acknowledgements,
        )
        state.on_pause_acknowledged = lambda: self._emit(
            state, parent_id, "task_pause_acknowledged", task_id=parent_id
        )
        self._active[parent_id] = state
        resolution_queue: asyncio.Queue[BatchEntry | None] = asyncio.Queue(
            maxsize=self._config.resolution_queue_size
        )
        download_queue: asyncio.Queue[DownloadChild | None] = asyncio.Queue(
            maxsize=self._config.download_queue_size
        )
        resolution_workers = [
            asyncio.create_task(
                self._resolution_worker(
                    parent_id,
                    state,
                    resolution_queue,
                    download_queue,
                    worker_id=f"resolution-{index}",
                ),
                name=f"resolution-{index}",
            )
            for index in range(self._config.resolution_workers)
        ]
        download_workers = [
            asyncio.create_task(
                self._download_worker(
                    parent_id, state, download_queue, worker_id=f"download-{index}"
                ),
                name=f"download-{index}",
            )
            for index in range(self._config.download_workers)
        ]
        workers = (*resolution_workers, *download_workers)

        try:
            self._mark_history_started(parent_id, run_id)
            self._emit(state, parent_id, "batch_started")
            for child in self._store.list_children(parent_id):
                if child.status is DownloadChildStatus.QUEUED:
                    await self._put_download(state, download_queue, child)
            for entry in self._store.list_entries(parent_id):
                if entry.status in {
                    BatchEntryStatus.PENDING_RESOLUTION,
                    BatchEntryStatus.RESOLVING,
                }:
                    await self._put_resolution(state, resolution_queue, entry)

            await resolution_queue.join()
            for _ in resolution_workers:
                await resolution_queue.put(None)
            await asyncio.gather(*resolution_workers)
            if state.worker_error is not None:
                raise state.worker_error

            await download_queue.join()
            for _ in download_workers:
                await download_queue.put(None)
            await asyncio.gather(*download_workers)
            if state.worker_error is not None:
                raise state.worker_error

            snapshot = self._store.get_snapshot(parent_id)
            if state.shutdown_event.is_set():
                return BatchRunResult(
                    snapshot=snapshot,
                    events=tuple(state.events),
                    resolution_queue_peak=state.resolution_peak,
                    download_queue_peak=state.download_peak,
                    stopped=True,
                )
            self._emit(
                state,
                parent_id,
                "batch_finished",
                status=snapshot.execution_status.value,
                counts=snapshot.counts.to_dict(),
            )
            self._finish_history_parent(parent_id, snapshot.execution_status.value)
            return BatchRunResult(
                snapshot=snapshot,
                events=tuple(state.events),
                resolution_queue_peak=state.resolution_peak,
                download_queue_peak=state.download_peak,
            )
        finally:
            self._active.pop(parent_id, None)
            pending_workers = [worker for worker in workers if not worker.done()]
            for worker in pending_workers:
                worker.cancel()
            if pending_workers:
                await asyncio.gather(*pending_workers, return_exceptions=True)
            self._release_waiting(state, resolution_queue, self._resolution_capacity, "resolution")
            self._release_waiting(state, download_queue, self._download_capacity, "download")

    async def _put_resolution(
        self,
        state: _RunState,
        queue: asyncio.Queue[BatchEntry | None],
        entry: BatchEntry,
    ) -> None:
        await self._resolution_capacity.acquire()
        state.resolution_waiting += 1
        state.resolution_peak = max(state.resolution_peak, state.resolution_waiting)
        try:
            await queue.put(entry)
        except BaseException:
            state.resolution_waiting -= 1
            self._resolution_capacity.release()
            raise
        self._emit(state, entry.parent_id, "entry_queued", entry_id=entry.entry_id)

    async def _put_download(
        self,
        state: _RunState,
        queue: asyncio.Queue[DownloadChild | None],
        child: DownloadChild,
    ) -> None:
        await self._download_capacity.acquire()
        state.download_waiting += 1
        state.download_peak = max(state.download_peak, state.download_waiting)
        try:
            await queue.put(child)
        except BaseException:
            state.download_waiting -= 1
            self._download_capacity.release()
            raise
        self._emit(state, child.parent_id, "child_queued", child_id=child.child_id)

    async def _resolution_worker(
        self,
        parent_id: str,
        state: _RunState,
        resolution_queue: asyncio.Queue[BatchEntry | None],
        download_queue: asyncio.Queue[DownloadChild | None],
        *,
        worker_id: str,
    ) -> None:
        _mark_worker_boundary(state, parent_id, worker_id, True)
        try:
            while True:
                entry = await resolution_queue.get()
                if entry is None:
                    resolution_queue.task_done()
                    return
                _mark_worker_boundary(state, parent_id, worker_id, False)
                state.resolution_waiting -= 1
                self._resolution_capacity.release()
                try:
                    if state.shutdown_event.is_set():
                        continue
                    if state.worker_error is not None:
                        continue
                    control = _TaskControl(state, parent_id, entry.entry_id, worker_id)
                    await control.wait_if_paused()
                    if control.is_set():
                        self._cancel_entry(state, entry)
                    else:
                        await self._resolve_entry(
                            parent_id,
                            state,
                            entry,
                            download_queue,
                            worker_id=worker_id,
                            control=control,
                        )
                except OwnerShutdownRequested:
                    continue
                except Exception as error:
                    state.worker_error = state.worker_error or error
                    state.cancel_event.set()
                finally:
                    resolution_queue.task_done()
                    _mark_worker_boundary(state, parent_id, worker_id, True)
        finally:
            _remove_worker_boundary(state, parent_id, worker_id)

    async def _resolve_entry(
        self,
        parent_id: str,
        state: _RunState,
        entry: BatchEntry,
        download_queue: asyncio.Queue[DownloadChild | None],
        *,
        worker_id: str,
        control: _TaskControl,
    ) -> None:
        resolving = entry
        if entry.status is BatchEntryStatus.PENDING_RESOLUTION:
            resolving = replace(entry, status=BatchEntryStatus.RESOLVING)
            self._store.update_entry(resolving)
        self._emit(state, parent_id, "entry_resolving", entry_id=entry.entry_id)
        reporter = self._reporter(state, parent_id, entry_id=entry.entry_id)
        site_slots = self._site_resolution_slots.get(entry.site_id)
        resolution_slots = (
            (site_slots, self._resolution_slots)
            if site_slots is not None
            else (self._resolution_slots,)
        )
        slot_controller = _SemaphoreLeaseController(*resolution_slots)
        try:
            await _acquire_with_control(slot_controller, control)
            pause_boundary, resume_boundary = _compose_pause_boundaries((slot_controller,))
            control = replace(
                control,
                pause_boundary=pause_boundary,
                resume_boundary=resume_boundary,
            )
            await control.wait_if_paused()
            if control.is_set():
                raise asyncio.CancelledError
            package = await self._resolve_package(resolving, state, control, reporter)
        except OwnerShutdownRequested:
            return
        except asyncio.CancelledError:
            if not control.is_set():
                raise
            self._cancel_entry(state, resolving)
            return
        except Exception as error:
            if control.is_set():
                self._cancel_entry(state, resolving)
                return
            failure = _failure(
                FailureOrigin.PLUGIN,
                FailureStage.RESOLVE,
                "plugin_resolution_failed",
                error,
            )
            self._store.update_entry(
                replace(
                    resolving,
                    status=BatchEntryStatus.RESOLUTION_FAILED,
                    failure=failure,
                )
            )
            self._emit(
                state,
                parent_id,
                "entry_resolution_failed",
                entry_id=entry.entry_id,
                failure=failure.to_dict(),
            )
            return
        finally:
            await slot_controller.release()

        if control.is_shutdown_requested():
            return
        if control.is_set():
            self._cancel_entry(state, resolving)
            return
        if type(package) is not DownloadPackage:
            failure = FailureData(
                origin=FailureOrigin.PLUGIN,
                stage=FailureStage.BUILD_PACKAGE,
                code="invalid_download_package",
                message="Plugin did not return a core DownloadPackage",
                retryable=False,
            )
            self._store.update_entry(
                replace(
                    resolving,
                    status=BatchEntryStatus.RESOLUTION_FAILED,
                    failure=failure,
                )
            )
            self._emit(
                state,
                parent_id,
                "entry_resolution_failed",
                entry_id=entry.entry_id,
                failure=failure.to_dict(),
            )
            return

        try:
            child = DownloadChild(
                child_id=self._child_id_factory(resolving, package),
                parent_id=parent_id,
                entry_id=entry.entry_id,
                package=package,
            )
            history_record = None
            if (
                isinstance(self._history_store, StateHistoryStore)
                and self._history_store.get_record(parent_id) is not None
            ):
                history_record = DownloadHistoryRecord(
                    record_id=child.child_id,
                    task_id=child.child_id,
                    kind=HistoryKind.CHILD,
                    parent_record_id=parent_id,
                    entry_id=entry.entry_id,
                    plugin_id=package.plugin_id,
                    site_id=package.site_id,
                    title=package.title or entry.title or entry.candidate_id,
                    source_url=package.source_url,
                    status=child.status.value,
                    total_files=len(package.assets),
                )
            self._store.attach_child(
                entry.entry_id,
                child,
                history_record=history_record,
            )
        except Exception as error:
            failure = _failure(
                FailureOrigin.SYSTEM,
                FailureStage.CREATE_CHILD,
                "package_handoff_failed",
                error,
            )
            self._store.update_entry(
                replace(
                    resolving,
                    status=BatchEntryStatus.PACKAGE_REJECTED,
                    failure=failure,
                )
            )
            self._emit(
                state,
                parent_id,
                "package_handoff_failed",
                entry_id=entry.entry_id,
                failure=failure.to_dict(),
            )
            return

        self._emit(
            state,
            parent_id,
            "package_persisted",
            entry_id=entry.entry_id,
            child_id=child.child_id,
            asset_count=len(package.assets),
        )
        # The package is durable and all parsing/request permits are released.
        # Backpressure can keep this worker waiting for a paused download queue;
        # it is already safe and must not block the parent's pause acknowledgement.
        _mark_worker_boundary(state, parent_id, worker_id, True)
        try:
            await self._put_download(state, download_queue, child)
        finally:
            _mark_worker_boundary(state, parent_id, worker_id, False)

    async def _resolve_package(
        self,
        entry: BatchEntry,
        state: _RunState,
        control: BatchControlSignal,
        reporter: ProgressReporter,
    ) -> DownloadPackage:
        plan = state.retry_plans.get(entry.entry_id)
        retry = getattr(self._resolver, "retry", None)
        if plan is not None and callable(retry):
            return await retry(entry, plan, control, reporter)
        return await self._resolver.resolve(entry, control, reporter)

    async def _download_worker(
        self,
        parent_id: str,
        state: _RunState,
        download_queue: asyncio.Queue[DownloadChild | None],
        *,
        worker_id: str,
    ) -> None:
        _mark_worker_boundary(state, parent_id, worker_id, True)
        try:
            while True:
                child = await download_queue.get()
                if child is None:
                    download_queue.task_done()
                    return
                _mark_worker_boundary(state, parent_id, worker_id, False)
                state.download_waiting -= 1
                self._download_capacity.release()
                try:
                    if state.shutdown_event.is_set():
                        continue
                    if state.worker_error is not None:
                        continue
                    control = _TaskControl(state, parent_id, child.entry_id, worker_id)
                    if control.is_set():
                        self._cancel_child(state, child)
                    else:
                        await self._download_child(
                            parent_id,
                            state,
                            child,
                            worker_id=worker_id,
                            control=control,
                        )
                except OwnerShutdownRequested:
                    continue
                except Exception as error:
                    state.worker_error = state.worker_error or error
                    state.cancel_event.set()
                finally:
                    download_queue.task_done()
                    _mark_worker_boundary(state, parent_id, worker_id, True)
        finally:
            _remove_worker_boundary(state, parent_id, worker_id)

    async def _download_child(
        self,
        parent_id: str,
        state: _RunState,
        child: DownloadChild,
        *,
        worker_id: str,
        control: _TaskControl,
    ) -> None:
        await control.wait_if_paused()
        if control.is_shutdown_requested():
            return
        if control.is_set():
            self._cancel_child(state, child)
            return

        controllers: list[object] = []
        lease_controller = None
        if self._download_limiter is not None:
            lease_controller = _ItemLeaseController(
                self._download_limiter,
                root_id=parent_id,
                site_id=child.package.site_id,
                sequence=self._child_sequence(parent_id, child.child_id),
                cancelled=control.is_set,
            )
            try:
                await _acquire_with_control(lease_controller, control)
            except asyncio.CancelledError:
                if control.is_shutdown_requested():
                    return
                if control.is_set():
                    self._cancel_child(state, child)
                    return
                raise
            controllers.append(lease_controller)
        # With a central scheduler, only its Item lease controls global/site
        # admission. Local workers and bounded queues provide backpressure.
        site_slots = self._site_download_slots.get(child.package.site_id)
        slot_controller = _SemaphoreLeaseController(
            *(
                ()
                if self._download_limiter is not None
                else (
                    (site_slots, self._download_slots)
                    if site_slots is not None
                    else (self._download_slots,)
                )
            )
        )
        try:
            await _acquire_with_control(slot_controller, control)
        except BaseException:
            for controller in reversed(controllers):
                await controller.release()
            raise
        controllers.append(slot_controller)
        if control.is_shutdown_requested():
            for controller in reversed(controllers):
                await controller.release()
            return
        pause_boundary, resume_boundary = _compose_pause_boundaries(tuple(controllers))
        control = replace(
            control,
            pause_boundary=pause_boundary,
            resume_boundary=resume_boundary,
            admission_release=_compose_admission_release(tuple(controllers)),
        )
        try:
            await self._execute_download_child(
                parent_id,
                state,
                child,
                worker_id=worker_id,
                control=control,
            )
        finally:
            for controller in reversed(controllers):
                await controller.release()

    def _child_sequence(self, parent_id: str, child_id: str) -> int:
        for sequence, child in enumerate(self._store.list_children(parent_id)):
            if child.child_id == child_id:
                return sequence
        return 0

    async def _execute_download_child(
        self,
        parent_id: str,
        state: _RunState,
        child: DownloadChild,
        *,
        worker_id: str,
        control: _TaskControl,
    ) -> None:
        downloading_child = replace(child, status=DownloadChildStatus.DOWNLOADING)
        self._store.update_child(downloading_child)
        assets = tuple(
            replace(
                asset,
                status=AssetStatus.DOWNLOADING,
                attempt_count=asset.attempt_count + 1,
            )
            for asset in self._store.list_assets(child.child_id)
            if asset.status is AssetStatus.QUEUED
        )
        if not assets:
            raise BatchConflictError(f"queued child has no queued assets: {child.child_id}")
        for asset in assets:
            self._store.update_asset(child.child_id, asset)
        self._emit(state, parent_id, "child_downloading", child_id=child.child_id)
        reporter = self._reporter(state, parent_id, child_id=child.child_id)
        failure: FailureData | None = None
        try:
            await control.wait_if_paused()
            if control.is_set():
                raise asyncio.CancelledError
            outcomes = await self._downloader.execute(downloading_child, assets, control, reporter)
            if control.is_shutdown_requested():
                return
            outcomes = _validate_outcomes(assets, outcomes)
        except OwnerShutdownRequested:
            return
        except asyncio.CancelledError:
            if not control.is_set():
                raise
            self._cancel_downloading_child(state, downloading_child, assets)
            return
        except Exception as error:
            if control.is_set():
                self._cancel_downloading_child(state, downloading_child, assets)
                return
            failure = _failure(
                FailureOrigin.SYSTEM,
                FailureStage.FETCH,
                "child_download_failed",
                error,
            )
            outcomes = tuple(
                replace(asset, status=AssetStatus.FAILED, failure=failure) for asset in assets
            )

        if control.is_shutdown_requested():
            return
        for outcome in outcomes:
            self._store.update_asset(child.child_id, outcome)
        all_outcomes = self._store.list_assets(child.child_id)
        completed = sum(outcome.status is AssetStatus.COMPLETED for outcome in all_outcomes)
        if completed == len(all_outcomes):
            final_child = replace(
                downloading_child,
                status=DownloadChildStatus.COMPLETED,
            )
            reason = "child_completed"
        elif completed:
            final_child = replace(
                downloading_child,
                status=DownloadChildStatus.PARTIALLY_COMPLETED,
                failure=_outcome_failure(all_outcomes),
            )
            reason = "child_partially_completed"
        elif all(outcome.status is AssetStatus.CANCELLED for outcome in all_outcomes):
            final_child = replace(
                downloading_child,
                status=DownloadChildStatus.CANCELLED,
            )
            reason = "child_cancelled"
        else:
            final_child = replace(
                downloading_child,
                status=DownloadChildStatus.FAILED,
                failure=failure or _outcome_failure(all_outcomes),
            )
            reason = "child_failed"
        output_files = []
        from pathlib import Path

        for outcome in all_outcomes:
            if outcome.status is AssetStatus.COMPLETED:
                final_path = outcome.final_path or outcome.partial_path
                if final_path:
                    output_files.append(str(Path(final_path)))

        event_payload = {
            "child_id": child.child_id,
            "completed_assets": completed,
            "total_assets": len(all_outcomes),
            "output_files": output_files,
        }
        if _uses_shared_state_terminal_stores(
            self._store, self._completion_store, self._history_store
        ):
            history_events = _terminal_history_events(parent_id, state, reason, event_payload)
            history_record = self._finished_child_history_record(
                child.child_id,
                status=final_child.status.value,
                completed_files=completed,
                total_files=len(all_outcomes),
                output_files=tuple(output_files),
            )
            self._store.finalize_child(
                final_child,
                completion_identity=(child.package.site_id, child.package.source_url)
                if reason == "child_completed"
                else None,
                history_events=history_events,
                history_record=history_record,
            )
            self._emit(state, parent_id, reason, persist_history=False, **event_payload)
            return
        self._store.update_child(final_child)
        if reason == "child_completed" and self._completion_store is not None:
            self._completion_store.record_completed(
                child.package.site_id,
                child.package.source_url,
            )
        if isinstance(self._history_store, StateHistoryStore):
            history_record = self._finished_child_history_record(
                child.child_id,
                status=final_child.status.value,
                completed_files=completed,
                total_files=len(all_outcomes),
                output_files=tuple(output_files),
            )
            if history_record is not None:
                self._history_store.upsert_record(history_record)
        self._emit(state, parent_id, reason, **event_payload)

    def _mark_history_started(self, parent_id: str, run_id: str | None) -> None:
        if not isinstance(self._history_store, StateHistoryStore):
            return
        record = self._history_store.get_latest_record(parent_id)
        if record is None:
            return
        self._history_store.upsert_record(
            replace(
                record,
                status="running",
                started_at=record.started_at or datetime.now(UTC),
                run_generation=record.run_generation,
            )
        )

    def _finished_child_history_record(
        self,
        child_id: str,
        *,
        status: str,
        completed_files: int,
        total_files: int,
        output_files: tuple[str, ...],
    ) -> DownloadHistoryRecord | None:
        if not isinstance(self._history_store, StateHistoryStore):
            return None
        record = self._history_store.get_record(child_id)
        if record is None:
            return None
        return replace(
            record,
            status=status,
            completed_files=completed_files,
            total_files=total_files,
            output_files=output_files,
            finished_at=datetime.now(UTC),
        )

    def _finish_history_parent(self, parent_id: str, status: str) -> None:
        if not isinstance(self._history_store, StateHistoryStore):
            return
        record = self._history_store.get_latest_record(parent_id)
        if record is None:
            return
        children = self._history_store.list_records(parent_record_id=record.record_id)
        output_files = tuple(path for child in children for path in child.output_files)
        total_files = sum(child.total_files or 0 for child in children)
        completed_files = sum(child.completed_files for child in children)
        self._history_store.upsert_record(
            replace(
                record,
                status=status,
                completed_files=completed_files,
                total_files=total_files or None,
                output_files=output_files,
                finished_at=datetime.now(UTC),
            )
        )

    def _cancel_entry(self, state: _RunState, entry: BatchEntry) -> None:
        self._store.update_entry(replace(entry, status=BatchEntryStatus.CANCELLED))
        self._emit(state, entry.parent_id, "entry_cancelled", entry_id=entry.entry_id)

    def _cancel_child(self, state: _RunState, child: DownloadChild) -> None:
        for asset in self._store.list_assets(child.child_id):
            if asset.status in {AssetStatus.QUEUED, AssetStatus.DOWNLOADING}:
                self._store.update_asset(
                    child.child_id,
                    replace(asset, status=AssetStatus.CANCELLED),
                )
        self._store.update_child(replace(child, status=DownloadChildStatus.CANCELLED))
        self._emit(state, child.parent_id, "child_cancelled", child_id=child.child_id)

    def _cancel_downloading_child(
        self,
        state: _RunState,
        child: DownloadChild,
        assets: tuple[AssetRecord, ...],
    ) -> None:
        for asset in assets:
            self._store.update_asset(
                child.child_id,
                replace(asset, status=AssetStatus.CANCELLED),
            )
        self._store.update_child(replace(child, status=DownloadChildStatus.CANCELLED))
        self._emit(state, child.parent_id, "child_cancelled", child_id=child.child_id)

    def _reporter(
        self,
        state: _RunState,
        parent_id: str,
        **identity: object,
    ) -> ProgressReporter:
        def report(reason: str, payload: dict[str, object]) -> None:
            self._emit(
                state,
                parent_id,
                "batch_progress",
                progress_reason=reason,
                **identity,
                **payload,
            )

        return report

    def _emit(
        self,
        state: _RunState,
        parent_id: str,
        reason: str,
        persist_history: bool = True,
        **payload: object,
    ) -> None:
        event_payload = dict(payload)
        if state.run_id is not None:
            event_payload["run_id"] = state.run_id
        event = TaskEvent(
            task_id=parent_id,
            name="batch_progress",
            reason=reason,
            payload=event_payload,
        )
        state.events.append(event)
        if (
            persist_history
            and self._history_store is not None
            and reason not in _STATE_CONTROL_REASONS
            and reason not in _NON_DURABLE_PROGRESS_REASONS
        ):
            self._history_store.append(event)
            if "child_id" in payload and isinstance(payload["child_id"], str):
                child_reason = _CHILD_EVENT_REASON.get(reason)
                if child_reason is not None:
                    self._history_store.append(
                        TaskEvent(
                            task_id=payload["child_id"],
                            name="task_transitioned",
                            reason=child_reason,
                            payload={"batch_id": parent_id, **payload},
                        )
                    )
        if self._event_sink is not None and reason not in _STATE_CONTROL_REASONS:
            self._event_sink(event)

    @staticmethod
    def _release_waiting(
        state: _RunState,
        queue: asyncio.Queue[object],
        capacity: asyncio.Semaphore,
        kind: str,
    ) -> None:
        while not queue.empty():
            item = queue.get_nowait()
            queue.task_done()
            if item is None:
                continue
            capacity.release()
            if kind == "resolution":
                state.resolution_waiting -= 1
            else:
                state.download_waiting -= 1


def _failure(
    origin: FailureOrigin,
    stage: FailureStage,
    code: str,
    error: Exception,
) -> FailureData:
    message = str(error).strip() or type(error).__name__
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
    return FailureData(
        origin=origin,
        stage=stage,
        code=code,
        message=message,
        retryable=True,
        details=details,
    )


def _validate_outcomes(
    expected: tuple[AssetRecord, ...],
    outcomes: object,
) -> tuple[AssetRecord, ...]:
    if not isinstance(outcomes, (tuple, list)):
        raise TypeError("download executor must return asset records")
    values = tuple(outcomes)
    expected_by_id = {record.asset.asset_id: record for record in expected}
    if len(values) != len(expected) or any(type(record) is not AssetRecord for record in values):
        raise ValueError("download executor must return every asset exactly once")
    values_by_id = {record.asset.asset_id: record for record in values}
    if set(values_by_id) != set(expected_by_id) or len(values_by_id) != len(values):
        raise ValueError("download executor returned mismatched asset ids")
    for asset_id, record in values_by_id.items():
        if record.asset != expected_by_id[asset_id].asset:
            raise ValueError("download executor cannot change asset request data")
        if record.status not in {AssetStatus.COMPLETED, AssetStatus.FAILED, AssetStatus.CANCELLED}:
            raise ValueError("download executor must return terminal asset states")
    return tuple(values_by_id[record.asset.asset_id] for record in expected)


def _outcome_failure(outcomes: tuple[AssetRecord, ...]) -> FailureData:
    for outcome in outcomes:
        if outcome.failure is not None:
            return outcome.failure
    return FailureData(
        origin=FailureOrigin.SYSTEM,
        stage=FailureStage.FETCH,
        code="asset_incomplete",
        message="One or more assets did not complete",
        retryable=True,
    )
