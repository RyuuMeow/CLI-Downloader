from __future__ import annotations

from dataclasses import dataclass

from cli_downloader.core.errors import InvalidTaskTransitionError, TaskNotFoundError
from cli_downloader.core.events import TaskEvent
from cli_downloader.storage.task_store import (
    TaskDesiredControl,
    TaskRecord,
    TaskStatus,
    TaskStore,
)


RETRYABLE_STATUSES = frozenset(
    {
        TaskStatus.FAILED,
        TaskStatus.PARTIALLY_COMPLETED,
        TaskStatus.CANCELED,
        TaskStatus.COMPLETED,
    }
)
RESUMABLE_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.PLANNED,
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
    }
)
PAUSABLE_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.PLANNED, TaskStatus.RUNNING})
CANCELLABLE_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.PLANNED,
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
    }
)


@dataclass(frozen=True)
class TaskTransitionResult:
    record: TaskRecord
    event: TaskEvent

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.record.task_id,
            "status": self.record.status.value,
            "event": self.event.reason,
        }


class TaskLifecycleService:
    def __init__(
        self,
        *,
        task_store: TaskStore,
    ) -> None:
        self._task_store = task_store

    def retry(self, task_id: str) -> TaskTransitionResult:
        return self._transition(
            task_id=task_id,
            action="retry",
            allowed_statuses=RETRYABLE_STATUSES,
            reason="task_retry_requested",
        )

    def resume(self, task_id: str) -> TaskTransitionResult:
        return self._transition(
            task_id=task_id,
            action="resume",
            allowed_statuses=RESUMABLE_STATUSES,
            reason="task_resume_requested",
        )

    def pause(self, task_id: str, *, request_only: bool = False) -> TaskTransitionResult:
        return self._transition(
            task_id=task_id,
            action="pause",
            allowed_statuses=PAUSABLE_STATUSES,
            reason="task_pause_requested",
            target_status=None if request_only else TaskStatus.PAUSED,
        )

    def cancel(self, task_id: str) -> TaskTransitionResult:
        return self._transition(
            task_id=task_id,
            action="cancel",
            allowed_statuses=CANCELLABLE_STATUSES,
            reason="task_cancel_requested",
            target_status=TaskStatus.CANCELED,
        )

    def delete(self, task_id: str) -> None:
        if self._task_store.get(task_id) is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        self._task_store.delete(task_id)

    def _transition(
        self,
        *,
        task_id: str,
        action: str,
        allowed_statuses: frozenset[TaskStatus],
        reason: str,
        target_status: TaskStatus | None = TaskStatus.PLANNED,
    ) -> TaskTransitionResult:
        for _ in range(8):
            record = self._task_store.get(task_id)
            if record is None:
                raise TaskNotFoundError(f"task not found: {task_id}")

            if record.status not in allowed_statuses:
                raise InvalidTaskTransitionError(
                    f"cannot {action} task {task_id} from status {record.status.value}"
                )

            desired_control = {
                "pause": TaskDesiredControl.PAUSE,
                "cancel": TaskDesiredControl.CANCEL,
            }.get(action, TaskDesiredControl.RUN)
            updated = record.transition(
                status=target_status if target_status is not None else record.status,
                desired_control=desired_control,
                metadata={
                    **record.metadata,
                    "last_transition": action,
                    "previous_status": record.status.value,
                },
            )
            if self._task_store.compare_and_swap(updated, expected_revision=record.revision):
                break
        else:
            raise InvalidTaskTransitionError(f"task changed while requesting {action}: {task_id}")
        event = TaskEvent(
            task_id=task_id,
            name="task_transitioned",
            reason=reason,
            payload={
                "from_status": record.status.value,
                "to_status": updated.status.value,
            },
        )
        return TaskTransitionResult(record=updated, event=event)
