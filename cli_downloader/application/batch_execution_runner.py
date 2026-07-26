"""Coordinator-owned Batch execution boundary.

The Pipeline remains responsible for execution records.  This runner owns the
Task run claim and turns the aggregate result into one Task terminal outcome.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from cli_downloader.application.batch_pipeline import BatchPipeline, BatchRunResult
from cli_downloader.application.task_execution_coordinator import (
    ExecutionHandle,
    TaskExecutionCoordinator,
    TaskRunReason,
)
from cli_downloader.core.batch import BatchCounts, RetryPlan
from cli_downloader.core.errors import InvalidTaskTransitionError, StaleTaskRevisionError
from cli_downloader.storage.task_store import TaskRecord, TaskStatus


class CoordinatedBatchRunner:
    """Run one Batch through a shared TaskExecutionCoordinator."""

    def __init__(
        self,
        *,
        pipeline: BatchPipeline,
        coordinator: TaskExecutionCoordinator,
    ) -> None:
        self._pipeline = pipeline
        self._coordinator = coordinator

    async def run_batch(
        self,
        parent_id: str,
        *,
        retry_plans: tuple[RetryPlan, ...] = (),
        reason: TaskRunReason = TaskRunReason.INITIAL,
    ) -> BatchRunResult:
        handle = await self._coordinator.claim(parent_id, reason=reason)
        control_acknowledgements: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        async def acknowledge_controls() -> None:
            while True:
                action, _task_id = await control_acknowledgements.get()
                try:
                    if action == "pause":
                        await self._coordinator.acknowledge_pause(
                            handle,
                            expected_revision=handle.revision,
                        )
                    else:
                        await self._coordinator.acknowledge_running(
                            handle,
                            expected_revision=handle.revision,
                        )
                except InvalidTaskTransitionError:
                    # A newer control command may win the race after a worker
                    # has already reached its safe boundary.
                    continue

        control_monitor = asyncio.create_task(acknowledge_controls())
        try:
            result = await self._pipeline.run_batch(
                parent_id,
                retry_plans=retry_plans,
                run_id=handle.run_id,
                control_acknowledgements=control_acknowledgements,
            )
        except Exception:
            await self._finish_task(
                handle,
                status=TaskStatus.FAILED,
            )
            raise
        finally:
            control_monitor.cancel()
            await asyncio.gather(control_monitor, return_exceptions=True)

        status = _terminal_task_status(result.snapshot.counts)
        committed = await self._finish_task(
            handle,
            status=status,
        )
        return replace(result, task_status=committed.status)

    async def _finish_task(
        self,
        handle: ExecutionHandle,
        *,
        status: TaskStatus,
    ) -> TaskRecord:
        """Commit the terminal Task outcome after any in-flight control CAS."""
        last_error: StaleTaskRevisionError | None = None
        for _attempt in range(4):
            try:
                return await self._coordinator.finish(
                    handle,
                    status=status,
                    expected_revision=handle.revision,
                )
            except StaleTaskRevisionError as error:
                last_error = error
                if self._coordinator.active_handle(handle.task_id) is not handle:
                    raise
                await asyncio.sleep(0)
        assert last_error is not None
        raise last_error

    async def request_retry(self, parent_id: str) -> TaskRecord:
        """Requeue a terminal Batch Task before constructing retry execution data."""
        return await self._coordinator.request_retry(parent_id)

    async def request_resume(self, parent_id: str) -> TaskRecord:
        """Restore run intent for a paused Batch Task before recovery starts."""
        return await self._coordinator.request_resume(parent_id)

    async def request_cancel(self, parent_id: str) -> TaskRecord:
        """Persist cancellation intent; the active run confirms it on termination."""
        return await self._coordinator.request_cancel(parent_id)

    async def run_recovery_batch(
        self,
        parent_id: str,
        *,
        retry_plans: tuple[RetryPlan, ...] = (),
    ) -> BatchRunResult:
        """Start a new generation from persisted operational checkpoints."""
        return await self.run_batch(
            parent_id,
            retry_plans=retry_plans,
            reason=TaskRunReason.RECOVERY,
        )


def _terminal_task_status(counts: BatchCounts) -> TaskStatus:
    active = counts.unresolved + counts.resolving + counts.queued + counts.downloading
    if active:
        raise RuntimeError("batch pipeline returned with active execution records")
    successful = counts.completed + counts.skipped
    problems = counts.rejected + counts.failed + counts.cancelled
    if successful == counts.total:
        return TaskStatus.COMPLETED
    if successful and problems:
        return TaskStatus.PARTIALLY_COMPLETED
    if counts.cancelled == counts.total:
        return TaskStatus.CANCELED
    if problems:
        return TaskStatus.FAILED
    raise RuntimeError("batch pipeline returned without a terminal outcome")
