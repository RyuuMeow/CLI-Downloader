"""Single-process ownership of Task execution and worker control tokens.

This is the P3-18 foundation layer.  It coordinates Task lifecycle revisions
before BatchPipeline is rewired to report its outcomes through it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable
from uuid import uuid4

from cli_downloader.core.errors import (
    InvalidTaskTransitionError,
    StaleTaskRevisionError,
    TaskExecutionConflictError,
    TaskNotFoundError,
)
from cli_downloader.storage.task_store import (
    TaskDesiredControl,
    TaskRecord,
    TaskStatus,
    TaskStore,
)


class TaskRunReason(StrEnum):
    INITIAL = "initial"
    RECOVERY = "recovery"
    RETRY = "retry"
    HARD_RETRY = "hard_retry"


_CLAIMABLE_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.PLANNED, TaskStatus.PAUSED})
_PAUSABLE_WITHOUT_RUN = frozenset({TaskStatus.PENDING, TaskStatus.PLANNED, TaskStatus.PAUSED})
_CANCELLABLE_WITHOUT_RUN = frozenset(
    {TaskStatus.PENDING, TaskStatus.PLANNED, TaskStatus.PAUSED, TaskStatus.FAILED}
)
_TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELED,
        TaskStatus.SKIPPED,
    }
)


@dataclass
class ExecutionHandle:
    """Control token held by the worker that owns an active Task run."""

    task_id: str
    run_id: str
    run_generation: int
    reason: TaskRunReason
    _revision: int
    _resume_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self) -> None:
        self._resume_event.set()

    @property
    def revision(self) -> int:
        """Current Task revision authorized for the next worker callback."""
        return self._revision

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def pause_requested(self) -> bool:
        return not self._resume_event.is_set()

    async def wait_if_paused(self) -> None:
        """Cooperatively wait until the Coordinator releases a pause request."""
        await self._resume_event.wait()

    async def wait_for_cancel(self) -> None:
        """Wait until the Coordinator signals cancellation."""
        await self._cancel_event.wait()

    def _set_revision(self, revision: int) -> None:
        self._revision = revision

    def _request_pause(self) -> None:
        self._resume_event.clear()

    def _resume(self) -> None:
        self._resume_event.set()

    def _request_cancel(self) -> None:
        self._cancel_event.set()
        self._resume_event.set()


class TaskExecutionCoordinator:
    """Serialize commands and run ownership for each persisted Task.

    SQLite compare-and-swap protects against another process changing a Task
    between the read and write.  The in-memory lock protects concurrent callers
    that share this Coordinator instance and is intentionally per Task so
    unrelated downloads remain concurrent.
    """

    def __init__(
        self,
        *,
        task_store: TaskStore,
        run_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._task_store = task_store
        self._run_id_factory = run_id_factory or (lambda: uuid4().hex)
        self._locks: dict[str, asyncio.Lock] = {}
        self._handles: dict[str, ExecutionHandle] = {}
        self._control_loop: asyncio.AbstractEventLoop | None = None

    async def claim(
        self,
        task_id: str,
        *,
        reason: TaskRunReason = TaskRunReason.INITIAL,
        expected_revision: int | None = None,
    ) -> ExecutionHandle:
        """Claim exclusive execution ownership and create a new run generation."""
        self._control_loop = asyncio.get_running_loop()
        async with self._lock_for(task_id):
            record = self._require_record(task_id)
            self._require_revision(record, expected_revision)
            if task_id in self._handles or record.active_run_id is not None:
                raise TaskExecutionConflictError(f"task is already running: {task_id}")
            if record.status not in _CLAIMABLE_STATUSES:
                raise InvalidTaskTransitionError(
                    f"cannot claim task {task_id} from status {record.status.value}"
                )
            if record.desired_control is not TaskDesiredControl.RUN:
                raise InvalidTaskTransitionError(f"task is not scheduled to run: {task_id}")

            run_id = self._run_id_factory()
            updated = record.transition(
                status=TaskStatus.RUNNING,
                active_run_id=run_id,
                run_generation=record.run_generation + 1,
                metadata={**record.metadata, "last_run_reason": reason.value},
            )
            self._commit(record, updated)
            handle = ExecutionHandle(
                task_id=task_id,
                run_id=run_id,
                run_generation=updated.run_generation,
                reason=reason,
                _revision=updated.revision,
            )
            self._handles[task_id] = handle
            return handle

    async def request_pause(
        self, task_id: str, *, expected_revision: int | None = None
    ) -> TaskRecord:
        """Persist pause intent and signal the active worker after the CAS succeeds."""
        async with self._lock_for(task_id):
            record = self._require_record(task_id)
            self._require_revision(record, expected_revision)
            handle = self._handles.get(task_id)
            if handle is None:
                if record.active_run_id is not None:
                    raise TaskExecutionConflictError(f"task run is owned elsewhere: {task_id}")
                if record.status not in _PAUSABLE_WITHOUT_RUN:
                    raise InvalidTaskTransitionError(
                        f"cannot pause task {task_id} from status {record.status.value}"
                    )
                updated = record.transition(
                    status=TaskStatus.PAUSED,
                    desired_control=TaskDesiredControl.PAUSE,
                )
            else:
                if record.desired_control is TaskDesiredControl.CANCEL:
                    raise InvalidTaskTransitionError(f"task is cancelling: {task_id}")
                updated = record.transition(desired_control=TaskDesiredControl.PAUSE)
            self._commit(record, updated)
            if handle is not None:
                handle._set_revision(updated.revision)
                handle._request_pause()
            return updated

    async def acknowledge_pause(
        self,
        handle: ExecutionHandle,
        *,
        expected_revision: int,
    ) -> TaskRecord:
        """Record the safe boundary after all workers honour a pause request."""
        async with self._lock_for(handle.task_id):
            record = self._current_handle_record(handle, expected_revision)
            if record.desired_control is not TaskDesiredControl.PAUSE:
                raise InvalidTaskTransitionError(f"task has no pending pause: {handle.task_id}")
            if record.status is not TaskStatus.RUNNING:
                raise InvalidTaskTransitionError(f"task is not running: {handle.task_id}")
            updated = record.transition(status=TaskStatus.PAUSED)
            self._commit(record, updated)
            handle._set_revision(updated.revision)
            return updated

    async def request_resume(
        self, task_id: str, *, expected_revision: int | None = None
    ) -> TaskRecord:
        """Persist run intent; an active handle resumes without creating another run."""
        async with self._lock_for(task_id):
            record = self._require_record(task_id)
            self._require_revision(record, expected_revision)
            handle = self._handles.get(task_id)
            if handle is None:
                if record.active_run_id is not None:
                    raise TaskExecutionConflictError(f"task run is owned elsewhere: {task_id}")
                if record.status is not TaskStatus.PAUSED:
                    raise InvalidTaskTransitionError(f"task is not paused: {task_id}")
                updated = record.transition(
                    status=TaskStatus.PLANNED,
                    desired_control=TaskDesiredControl.RUN,
                )
            else:
                if record.desired_control is not TaskDesiredControl.PAUSE:
                    raise InvalidTaskTransitionError(f"task is not paused: {task_id}")
                updated = record.transition(desired_control=TaskDesiredControl.RUN)
            self._commit(record, updated)
            if handle is not None:
                handle._set_revision(updated.revision)
                handle._resume()
            return updated

    async def acknowledge_running(
        self,
        handle: ExecutionHandle,
        *,
        expected_revision: int,
    ) -> TaskRecord:
        """Record that a paused worker has resumed at a safe boundary."""
        async with self._lock_for(handle.task_id):
            record = self._current_handle_record(handle, expected_revision)
            if record.desired_control is not TaskDesiredControl.RUN:
                raise InvalidTaskTransitionError(f"task has no pending resume: {handle.task_id}")
            if record.status is not TaskStatus.PAUSED:
                raise InvalidTaskTransitionError(f"task is not paused: {handle.task_id}")
            updated = record.transition(status=TaskStatus.RUNNING)
            self._commit(record, updated)
            handle._set_revision(updated.revision)
            return updated

    async def request_cancel(
        self, task_id: str, *, expected_revision: int | None = None
    ) -> TaskRecord:
        """Persist cancel intent and release any worker waiting on a pause token."""
        async with self._lock_for(task_id):
            record = self._require_record(task_id)
            self._require_revision(record, expected_revision)
            handle = self._handles.get(task_id)
            if handle is None:
                if record.active_run_id is not None:
                    raise TaskExecutionConflictError(f"task run is owned elsewhere: {task_id}")
                if record.status not in _CANCELLABLE_WITHOUT_RUN:
                    raise InvalidTaskTransitionError(
                        f"cannot cancel task {task_id} from status {record.status.value}"
                    )
                updated = record.transition(
                    status=TaskStatus.CANCELED,
                    desired_control=TaskDesiredControl.CANCEL,
                )
            else:
                updated = record.transition(desired_control=TaskDesiredControl.CANCEL)
            self._commit(record, updated)
            if handle is not None:
                handle._set_revision(updated.revision)
                handle._request_cancel()
            return updated

    async def request_retry(
        self, task_id: str, *, expected_revision: int | None = None
    ) -> TaskRecord:
        """Requeue a terminal Task after confirming no execution still owns it."""
        async with self._lock_for(task_id):
            record = self._require_record(task_id)
            self._require_revision(record, expected_revision)
            if task_id in self._handles or record.active_run_id is not None:
                raise TaskExecutionConflictError(f"task is already running: {task_id}")
            if record.status not in _TERMINAL_STATUSES:
                raise InvalidTaskTransitionError(
                    f"cannot retry task {task_id} from status {record.status.value}"
                )
            updated = record.transition(
                status=TaskStatus.PLANNED,
                desired_control=TaskDesiredControl.RUN,
            )
            self._commit(record, updated)
            return updated

    async def finish(
        self,
        handle: ExecutionHandle,
        *,
        status: TaskStatus,
        expected_revision: int,
    ) -> TaskRecord:
        """Accept a current worker terminal outcome and release its run ownership."""
        if status not in _TERMINAL_STATUSES:
            raise ValueError(f"finish requires a terminal Task status, got {status.value}")
        async with self._lock_for(handle.task_id):
            record = self._current_handle_record(handle, expected_revision)
            # A cancel command may win the CAS immediately after the pipeline
            # has produced a terminal snapshot. Preserve that user intent as
            # the committed Task outcome instead of surfacing a late-race
            # exception to the shell.
            if record.desired_control is TaskDesiredControl.CANCEL:
                status = TaskStatus.CANCELED
            desired_control = (
                TaskDesiredControl.CANCEL
                if status is TaskStatus.CANCELED
                else TaskDesiredControl.RUN
            )
            updated = record.transition(
                status=status,
                desired_control=desired_control,
                active_run_id=None,
            )
            self._commit(record, updated)
            del self._handles[handle.task_id]
            return updated

    async def checkpoint_for_shutdown(
        self,
        handle: ExecutionHandle,
        *,
        expected_revision: int,
    ) -> TaskRecord:
        """Persist a resumable checkpoint and release the active run lease."""

        async with self._lock_for(handle.task_id):
            record = self._current_handle_record(handle, expected_revision)
            paused = (
                record.desired_control is TaskDesiredControl.PAUSE
                or record.status is TaskStatus.PAUSED
            )
            updated = record.transition(
                status=TaskStatus.PAUSED if paused else TaskStatus.PLANNED,
                desired_control=TaskDesiredControl.PAUSE if paused else TaskDesiredControl.RUN,
                active_run_id=None,
                metadata={**record.metadata, "owner_shutdown_checkpoint": True},
            )
            self._commit(record, updated)
            del self._handles[handle.task_id]
            return updated

    def active_handle(self, task_id: str) -> ExecutionHandle | None:
        """Return the local control token without exposing another mutation path."""
        return self._handles.get(task_id)

    def request_control_sync(self, action: TaskDesiredControl, task_id: str) -> TaskRecord:
        """Submit a UI-thread control command onto the active execution loop.

        Textual calls TaskHierarchyService synchronously, while a Batch runs in
        a background event loop.  This keeps the CAS and handle mutation on
        that execution loop instead of performing another direct Task write.
        """
        loop = self._control_loop
        if loop is None or not loop.is_running():
            raise InvalidTaskTransitionError(f"task execution is not active: {task_id}")
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            raise RuntimeError("synchronous task control cannot run on its execution loop")
        operation = {
            TaskDesiredControl.PAUSE: self.request_pause,
            TaskDesiredControl.RUN: self.request_resume,
            TaskDesiredControl.CANCEL: self.request_cancel,
        }[action]
        return asyncio.run_coroutine_threadsafe(operation(task_id), loop).result()

    def _lock_for(self, task_id: str) -> asyncio.Lock:
        return self._locks.setdefault(task_id, asyncio.Lock())

    def _require_record(self, task_id: str) -> TaskRecord:
        record = self._task_store.get(task_id)
        if record is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return record

    @staticmethod
    def _require_revision(record: TaskRecord, expected_revision: int | None) -> None:
        if expected_revision is not None and record.revision != expected_revision:
            raise StaleTaskRevisionError(
                f"stale task revision for {record.task_id}: "
                f"expected {expected_revision}, current {record.revision}"
            )

    def _current_handle_record(
        self,
        handle: ExecutionHandle,
        expected_revision: int,
    ) -> TaskRecord:
        current_handle = self._handles.get(handle.task_id)
        if current_handle is not handle:
            raise StaleTaskRevisionError(f"stale execution handle: {handle.task_id}")
        record = self._require_record(handle.task_id)
        if (
            record.active_run_id != handle.run_id
            or record.run_generation != handle.run_generation
            or expected_revision != handle.revision
            or record.revision != expected_revision
        ):
            raise StaleTaskRevisionError(f"stale worker callback: {handle.task_id}")
        return record

    def _commit(self, previous: TaskRecord, updated: TaskRecord) -> None:
        if not self._task_store.compare_and_swap(updated, expected_revision=previous.revision):
            raise StaleTaskRevisionError(f"concurrent task update: {previous.task_id}")
