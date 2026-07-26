from tests.state_store_support import StateTaskStore
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.core.events import TaskEvent
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.task_store import (
    InMemoryTaskStore,
    TaskDesiredControl,
    TaskRecord,
    TaskStatus,
)


class InMemoryTaskStoreTest(TestCase):
    def test_upsert_get_and_list_records(self) -> None:
        store = InMemoryTaskStore()
        record = TaskRecord(
            task_id="task-1",
            status=TaskStatus.PENDING,
            source_url="https://example.com/item",
        )

        store.upsert(record)

        self.assertEqual(store.get("task-1"), record)
        self.assertEqual(store.list(), (record,))

    def test_compare_and_swap_rejects_stale_revision(self) -> None:
        store = InMemoryTaskStore()
        original = TaskRecord(task_id="task-1", status=TaskStatus.PLANNED)
        store.upsert(original)

        updated = original.transition(status=TaskStatus.RUNNING)

        self.assertTrue(store.compare_and_swap(updated, expected_revision=0))
        self.assertFalse(store.compare_and_swap(original, expected_revision=0))


class StateTaskStorePersistenceTest(TestCase):
    def test_upsert_get_and_list_records(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateTaskStore(Path(directory) / "state" / "state.sqlite3")
            record = TaskRecord(
                task_id="task-1",
                status=TaskStatus.COMPLETED,
                kind=TaskKind.BATCH,
                desired_control=TaskDesiredControl.PAUSE,
                revision=7,
                active_run_id="run-7",
                run_generation=3,
                created_at=datetime(2026, 7, 10, 1, 2, tzinfo=UTC),
                updated_at=datetime(2026, 7, 10, 3, 4, tzinfo=UTC),
                site_id="example",
                source_url="example://gallery/welcome",
                title="Example Item",
                metadata={"asset_count": 2, "output_files": ["a.txt"]},
            )

            store.upsert(record)

            self.assertEqual(store.get("task-1"), record)
            self.assertEqual(store.list(), (record,))

    def test_records_persist_across_store_instances(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state" / "state.sqlite3"
            first = StateTaskStore(db_path)
            record = TaskRecord(
                task_id="task-1",
                status=TaskStatus.PLANNED,
                metadata={"profile": "manga"},
            )

            first.upsert(record)
            second = StateTaskStore(db_path)

            self.assertEqual(second.get("task-1"), record)

    def test_compare_and_swap_rejects_stale_revision(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateTaskStore(Path(directory) / "state.sqlite3")
            original = TaskRecord(task_id="task-1", status=TaskStatus.PLANNED)
            store.upsert(original)
            updated = original.transition(status=TaskStatus.RUNNING)

            self.assertTrue(store.compare_and_swap(updated, expected_revision=0))
            self.assertFalse(store.compare_and_swap(original, expected_revision=0))
            self.assertEqual(store.get("task-1"), updated)


class StateTaskStoreTest(TestCase):
    def test_persists_records_and_rejects_stale_compare_and_swap(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            first = StateTaskStore(database)
            record = TaskRecord(task_id="task-1", status=TaskStatus.PLANNED)
            first.upsert(record)
            updated = record.transition(status=TaskStatus.RUNNING)

            self.assertTrue(first.compare_and_swap(updated, expected_revision=record.revision))
            self.assertFalse(first.compare_and_swap(record, expected_revision=record.revision))
            self.assertEqual(StateTaskStore(database).list(), (updated,))

    def test_delete_removes_task_but_history_is_not_cascaded(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            store = StateTaskStore(database)
            history = StateHistoryStore(database)
            store.upsert(TaskRecord(task_id="task-1", status=TaskStatus.COMPLETED))
            history.append(TaskEvent("task-1", "task_completed", "completed"))

            store.delete("task-1")

            self.assertIsNone(store.get("task-1"))
            self.assertEqual(len(history.list(task_id="task-1")), 1)
            with self.assertRaises(KeyError):
                store.delete("task-1")
