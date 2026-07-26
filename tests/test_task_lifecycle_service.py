from unittest import TestCase

from cli_downloader.application.task_lifecycle_service import TaskLifecycleService
from cli_downloader.core.errors import InvalidTaskTransitionError, TaskNotFoundError
from cli_downloader.storage.task_store import InMemoryTaskStore, TaskRecord, TaskStatus
from cli_downloader.storage.task_store import TaskDesiredControl


class TaskLifecycleServiceTest(TestCase):
    def test_retry_moves_failed_task_to_planned_and_records_history(self) -> None:
        task_store = InMemoryTaskStore()
        task_store.upsert(TaskRecord(task_id="task-1", status=TaskStatus.FAILED))
        service = TaskLifecycleService(task_store=task_store)

        result = service.retry("task-1")

        self.assertEqual(result.record.status, TaskStatus.PLANNED)
        self.assertEqual(result.record.metadata["last_transition"], "retry")
        self.assertEqual(result.record.desired_control, TaskDesiredControl.RUN)
        self.assertEqual(result.record.revision, 1)
        self.assertEqual(result.event.reason, "task_retry_requested")
        self.assertEqual(task_store.get("task-1").status, TaskStatus.PLANNED)

    def test_resume_moves_running_task_to_planned(self) -> None:
        task_store = InMemoryTaskStore()
        task_store.upsert(TaskRecord(task_id="task-1", status=TaskStatus.RUNNING))
        service = TaskLifecycleService(task_store=task_store)

        result = service.resume("task-1")

        self.assertEqual(result.record.status, TaskStatus.PLANNED)
        self.assertEqual(result.record.metadata["previous_status"], "running")
        self.assertEqual(result.event.reason, "task_resume_requested")

    def test_resume_rejects_completed_task(self) -> None:
        task_store = InMemoryTaskStore()
        task_store.upsert(TaskRecord(task_id="task-1", status=TaskStatus.COMPLETED))
        service = TaskLifecycleService(task_store=task_store)

        with self.assertRaises(InvalidTaskTransitionError):
            service.resume("task-1")

    def test_retry_rejects_missing_task(self) -> None:
        service = TaskLifecycleService(task_store=InMemoryTaskStore())

        with self.assertRaises(TaskNotFoundError):
            service.retry("missing")

    def test_pause_resume_cancel_and_delete_lifecycle(self) -> None:
        task_store = InMemoryTaskStore()
        task_store.upsert(TaskRecord(task_id="task-1", status=TaskStatus.RUNNING))
        service = TaskLifecycleService(task_store=task_store)

        self.assertEqual(service.pause("task-1").record.status, TaskStatus.PAUSED)
        self.assertEqual(service.resume("task-1").record.status, TaskStatus.PLANNED)
        self.assertEqual(service.cancel("task-1").record.status, TaskStatus.CANCELED)
        self.assertEqual(task_store.get("task-1").desired_control, TaskDesiredControl.CANCEL)
        self.assertEqual(task_store.get("task-1").revision, 3)
        service.delete("task-1")

        self.assertIsNone(task_store.get("task-1"))
