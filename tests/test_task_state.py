from tests.state_store_support import StateBatchStore, StateTaskStore
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.core.batch import BatchEntry, BatchParent
from cli_downloader.core.task_tree import TaskKind, TaskState
from cli_downloader.storage.task_store import TaskDesiredControl, TaskRecord, TaskStatus


class TaskStateTests(TestCase):
    def test_running_pause_intent_projects_as_pausing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            tasks.upsert(
                TaskRecord(
                    "item-1",
                    TaskStatus.RUNNING,
                    desired_control=TaskDesiredControl.PAUSE,
                )
            )

            node = TaskHierarchyService(task_store=tasks, batch_store=batches).get("item-1")

            self.assertEqual(node.state, TaskState.PAUSING)

    def test_batch_root_state_comes_only_from_authoritative_task_record(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            entry = BatchEntry(
                entry_id="entry-1",
                parent_id="batch-1",
                plugin_id="example",
                site_id="example",
                candidate_id="gallery-1",
                source_operation_id="resolve_gallery",
                source_url="https://example.test/gallery/1",
            )
            batches.create_batch(BatchParent("batch-1", (entry.entry_id,)), (entry,))
            tasks.upsert(TaskRecord("batch-1", TaskStatus.COMPLETED, kind=TaskKind.BATCH))
            service = TaskHierarchyService(task_store=tasks, batch_store=batches)

            self.assertEqual(service.get("batch-1").state, TaskState.COMPLETED)

            tasks.upsert(TaskRecord("batch-1", TaskStatus.RUNNING, kind=TaskKind.BATCH))

            self.assertEqual(service.get("batch-1").state, TaskState.RUNNING)

    def test_batch_state_returns_paused_when_task_store_says_paused(self) -> None:
        """_batch_state reads Task DB and returns PAUSED when status is PAUSED."""
        with TemporaryDirectory() as d:
            base = Path(d)
            task_store = StateTaskStore(base / "state.sqlite3")
            batch_store = StateBatchStore(base / "state.sqlite3")

            service = TaskHierarchyService(
                task_store=task_store,
                batch_store=batch_store,
            )

            # Standalone task: PAUSED in task store → _batch_state returns PAUSED
            task_store.upsert(
                TaskRecord(
                    task_id="paused-task",
                    status=TaskStatus.PAUSED,
                    source_url="https://example.test/x",
                )
            )

            node = service.get("paused-task")
            self.assertEqual(node.state, TaskState.PAUSED)

    def test_batch_state_returns_normal_when_not_paused(self) -> None:
        """_batch_state returns the computed state when Task DB status is not PAUSED."""
        with TemporaryDirectory() as d:
            base = Path(d)
            task_store = StateTaskStore(base / "state.sqlite3")
            batch_store = StateBatchStore(base / "state.sqlite3")

            service = TaskHierarchyService(
                task_store=task_store,
                batch_store=batch_store,
            )

            task_store.upsert(
                TaskRecord(
                    task_id="running-task",
                    status=TaskStatus.RUNNING,
                    source_url="https://example.test/x",
                )
            )

            node = service.get("running-task")
            self.assertEqual(node.state, TaskState.RUNNING)

    def test_pause_persists_in_task_store(self) -> None:
        """Pause writes PAUSED status to Task DB via _batch_control."""
        with TemporaryDirectory() as d:
            base = Path(d)
            task_store = StateTaskStore(base / "state.sqlite3")
            batch_store = StateBatchStore(base / "state.sqlite3")

            from cli_downloader.core.batch import (
                BatchEntry,
                BatchParent,
            )

            parent = BatchParent("parent-1", ("entry-1",), title="Test")
            entry = BatchEntry(
                entry_id="entry-1",
                parent_id="parent-1",
                plugin_id="example",
                site_id="example",
                candidate_id="c1",
                source_operation_id="batch_download",
                source_url="https://example.test/x",
            )
            batch_store.create_batch(parent, (entry,))
            task_store.upsert(
                TaskRecord(
                    task_id="parent-1",
                    status=TaskStatus.RUNNING,
                    kind=TaskKind.BATCH,
                    source_url="https://example.test/x",
                )
            )

            service = TaskHierarchyService(
                task_store=task_store,
                batch_store=batch_store,
            )

            service.pause("parent-1")
            self.assertEqual(task_store.get("parent-1").status, TaskStatus.PAUSED)

            service.resume("parent-1")
            self.assertEqual(task_store.get("parent-1").status, TaskStatus.PLANNED)

            service.cancel("parent-1")
            self.assertEqual(task_store.get("parent-1").status, TaskStatus.CANCELED)

    def test_inactive_parent_control_preserves_execution_checkpoint_rows(self) -> None:
        with TemporaryDirectory() as d:
            base = Path(d)
            task_store = StateTaskStore(base / "state.sqlite3")
            batch_store = StateBatchStore(base / "state.sqlite3")

            from cli_downloader.core.batch import BatchEntry, BatchParent

            entry = BatchEntry(
                entry_id="entry-1",
                parent_id="parent-1",
                plugin_id="example",
                site_id="example",
                candidate_id="c1",
                source_operation_id="batch_download",
                source_url="https://example.test/x",
            )
            batch_store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            task_store.upsert(
                TaskRecord(
                    task_id="parent-1",
                    status=TaskStatus.RUNNING,
                    kind=TaskKind.BATCH,
                )
            )
            service = TaskHierarchyService(task_store=task_store, batch_store=batch_store)

            service.pause("parent-1")
            self.assertEqual(task_store.get("parent-1").status, TaskStatus.PAUSED)
            self.assertEqual(batch_store.get_entry("entry-1").status.value, "pending_resolution")
            service.cancel("parent-1")
            self.assertEqual(task_store.get("parent-1").status, TaskStatus.CANCELED)
            self.assertEqual(batch_store.get_entry("entry-1").status.value, "pending_resolution")

    def test_pause_persists_across_service_instances(self) -> None:
        """Task DB status survives closing and reopening the service."""
        with TemporaryDirectory() as d:
            base = Path(d)

            # First instance: pause a task
            task_store = StateTaskStore(base / "state.sqlite3")
            task_store.upsert(
                TaskRecord(
                    task_id="persist-test",
                    status=TaskStatus.RUNNING,
                    source_url="https://example.test/x",
                )
            )

            service = TaskHierarchyService(
                task_store=task_store,
                batch_store=StateBatchStore(base / "state.sqlite3"),
            )
            service.pause("persist-test")
            self.assertEqual(task_store.get("persist-test").status, TaskStatus.PAUSED)

            # Second instance (simulating restart): should see PAUSED
            task_store2 = StateTaskStore(base / "state.sqlite3")
            service2 = TaskHierarchyService(
                task_store=task_store2,
                batch_store=StateBatchStore(base / "state.sqlite3"),
            )

            node = service2.get("persist-test")
            self.assertEqual(node.state, TaskState.PAUSED)


if __name__ == "__main__":
    TestCase.main()
