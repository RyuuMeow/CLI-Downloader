from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.application.task_lifecycle_service import TaskLifecycleService
from cli_downloader.storage.completion_store import InMemoryCompletionStore
from cli_downloader.storage.history_store import InMemoryHistoryStore
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus


class InMemoryHistoryStoreTest(TestCase):
    def test_append_many_and_list_by_task(self) -> None:
        store = InMemoryHistoryStore()
        first = _event("task-1", "first")
        second = _event("task-2", "second")

        store.append_many((first, second))

        self.assertEqual(store.list(), (first, second))
        self.assertEqual(store.list(task_id="task-1"), (first,))


class StateHistoryStoreTest(TestCase):
    def test_transient_progress_is_not_persisted_but_semantic_events_are(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateHistoryStore(StateDatabase(Path(directory) / "state.sqlite3"))
            store.append_many(
                (
                    TaskEvent("task-1", "batch_progress", "byte_progress"),
                    TaskEvent("task-1", "batch_progress", "child_downloading"),
                    TaskEvent("task-1", "batch_progress", "child_completed"),
                )
            )

            self.assertEqual(tuple(event.reason for event in store.list()), ("child_completed",))

    def test_append_many_and_list_by_task(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateHistoryStore(StateDatabase(Path(directory) / "state.sqlite3"))
            first = _event("task-1", "first")
            second = _event("task-2", "second")

            store.append_many((first, second))

            self.assertEqual(store.list(), (first, second))
            self.assertEqual(store.list(task_id="task-1"), (first,))

    def test_events_persist_across_store_instances(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            first_store = StateHistoryStore(StateDatabase(db_path))
            event = _event("task-1", "first")

            first_store.append(event)
            second_store = StateHistoryStore(StateDatabase(db_path))

            self.assertEqual(second_store.list(), (event,))

    def test_delete_task_removes_only_matching_events(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateHistoryStore(StateDatabase(Path(directory) / "state.sqlite3"))
            first = _event("task-1", "first")
            second = _event("task-2", "second")
            store.append_many((first, second))

            store.delete_task("task-1")

            self.assertEqual(store.list(), (second,))

    def test_delete_parent_removes_history_aggregate_and_linked_events(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateHistoryStore(StateDatabase(Path(directory) / "state.sqlite3"))
            store.upsert_record(
                DownloadHistoryRecord("batch-1", "batch-1", HistoryKind.BATCH, title="Batch")
            )
            store.upsert_record(
                DownloadHistoryRecord(
                    "child-1",
                    "child-1",
                    HistoryKind.CHILD,
                    parent_record_id="batch-1",
                    entry_id="entry-1",
                    title="One",
                )
            )
            store.append_many(
                (
                    _event("batch-1", "batch_created"),
                    _event("entry-1", "task_persisted"),
                    _event("child-1", "task_completed"),
                )
            )

            store.delete_task("batch-1")

            self.assertEqual(store.list_records(), ())
            self.assertEqual(store.list(), ())

    def test_delete_child_preserves_parent_and_siblings(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateHistoryStore(StateDatabase(Path(directory) / "state.sqlite3"))
            for record in (
                DownloadHistoryRecord("batch-1", "batch-1", HistoryKind.BATCH),
                DownloadHistoryRecord(
                    "child-1", "child-1", HistoryKind.CHILD, parent_record_id="batch-1"
                ),
                DownloadHistoryRecord(
                    "child-2", "child-2", HistoryKind.CHILD, parent_record_id="batch-1"
                ),
            ):
                store.upsert_record(record)
            store.append_many(
                (
                    _event("batch-1", "batch_created"),
                    _event("child-1", "task_completed"),
                    _event("child-2", "task_completed"),
                )
            )

            store.delete_task("child-1")

            self.assertIsNotNone(store.get_record("batch-1"))
            self.assertIsNone(store.get_record("child-1"))
            self.assertIsNotNone(store.get_record("child-2"))
            self.assertEqual(tuple(event.task_id for event in store.list()), ("batch-1", "child-2"))

    def test_retention_deletes_complete_aggregate_without_fragmenting_it(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateHistoryStore(StateDatabase(Path(directory) / "state.sqlite3"))
            old = datetime(2026, 1, 1, tzinfo=UTC)
            store.upsert_record(
                DownloadHistoryRecord(
                    "batch-1",
                    "batch-1",
                    HistoryKind.BATCH,
                    status="completed",
                    created_at=old,
                    finished_at=old,
                )
            )
            store.upsert_record(
                DownloadHistoryRecord(
                    "child-1",
                    "child-1",
                    HistoryKind.CHILD,
                    parent_record_id="batch-1",
                    status="completed",
                    created_at=old,
                    finished_at=old,
                )
            )
            store.upsert_record(
                DownloadHistoryRecord("pending", "pending", HistoryKind.ITEM, created_at=old)
            )

            store.delete_older_than(30)

            self.assertEqual(
                tuple(record.record_id for record in store.list_records()), ("pending",)
            )

    def test_clear_preserves_completion_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "state.sqlite3")
            store = StateHistoryStore(database)
            completion = SQLiteCompletionStore(database)
            completion.record_completed("example", "https://example.test/gallery/1")

            store.clear()

            self.assertTrue(completion.is_completed("example", "https://example.test/gallery/1"))

    def test_clear_removes_records_and_preserves_completion_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "state.sqlite3")
            store = StateHistoryStore(database)
            completion = SQLiteCompletionStore(database)
            store.upsert_record(
                DownloadHistoryRecord("history-1", "task-1", HistoryKind.ITEM, title="Saved")
            )
            completion.record_completed("example", "gallery-1")

            store.clear()

            self.assertIsNone(store.get_record("history-1"))
            self.assertTrue(completion.is_completed("example", "gallery-1"))

    def test_in_memory_history_and_completion_have_independent_clear_controls(self) -> None:
        history = InMemoryHistoryStore()
        completion = InMemoryCompletionStore()
        history.append(_event("task-1", "completed"))
        completion.record_completed("example", "https://example.test/gallery/1")

        history.clear()

        self.assertEqual(history.list(), ())
        self.assertTrue(completion.is_completed("example", "https://example.test/gallery/1"))

    def test_events_persist_and_history_deletion_keeps_completion(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            history = StateHistoryStore(database)
            completion = SQLiteCompletionStore(database)
            first = _event("task-1", "first")
            second = _event("task-2", "second")

            history.append_many((first, second))
            completion.record_completed("example", "gallery-1")
            history.delete_task("task-1")

            self.assertEqual(history.list(), (second,))
            self.assertTrue(completion.is_completed("example", "gallery-1"))

    def test_task_deletion_keeps_history_and_completion_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            history = StateHistoryStore(database)
            completion = SQLiteCompletionStore(database)
            tasks.upsert(
                TaskRecord(
                    task_id="task-1",
                    status=TaskStatus.COMPLETED,
                    kind=TaskKind.ITEM,
                    site_id="example",
                    source_url="example://gallery/one",
                    title="One",
                )
            )
            history.upsert_record(
                DownloadHistoryRecord(
                    "history-1",
                    "task-1",
                    HistoryKind.ITEM,
                    site_id="example",
                    source_url="example://gallery/one",
                    title="One",
                    status="completed",
                )
            )
            completion.record_completed("example", "example://gallery/one")

            TaskLifecycleService(task_store=tasks).delete("task-1")

            self.assertIsNone(tasks.get("task-1"))
            self.assertIsNotNone(history.get_record("history-1"))
            self.assertTrue(completion.is_completed("example", "example://gallery/one"))

    def test_history_deletion_keeps_task_and_completion_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            history = StateHistoryStore(database)
            completion = SQLiteCompletionStore(database)
            tasks.upsert(
                TaskRecord(
                    task_id="task-1",
                    status=TaskStatus.COMPLETED,
                    kind=TaskKind.ITEM,
                    site_id="example",
                    source_url="example://gallery/one",
                )
            )
            history.upsert_record(
                DownloadHistoryRecord(
                    "history-1",
                    "task-1",
                    HistoryKind.ITEM,
                    site_id="example",
                    source_url="example://gallery/one",
                    status="completed",
                )
            )
            completion.record_completed("example", "example://gallery/one")

            history.delete_task("task-1")

            self.assertIsNotNone(tasks.get("task-1"))
            self.assertIsNone(history.get_record("history-1"))
            self.assertTrue(completion.is_completed("example", "example://gallery/one"))

    def test_history_retention_keeps_task_and_completion_ledger(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            history = StateHistoryStore(database)
            completion = SQLiteCompletionStore(database)
            tasks.upsert(
                TaskRecord(
                    task_id="task-1",
                    status=TaskStatus.COMPLETED,
                    site_id="example",
                    source_url="example://gallery/one",
                )
            )
            finished_at = datetime.now(UTC) - timedelta(days=31)
            history.upsert_record(
                DownloadHistoryRecord(
                    "history-1",
                    "task-1",
                    HistoryKind.ITEM,
                    site_id="example",
                    source_url="example://gallery/one",
                    status="completed",
                    created_at=finished_at,
                    finished_at=finished_at,
                )
            )
            completion.record_completed("example", "example://gallery/one")

            history.delete_older_than(30)

            self.assertIsNone(history.get_record("history-1"))
            self.assertIsNotNone(tasks.get("task-1"))
            self.assertTrue(completion.is_completed("example", "example://gallery/one"))


def _event(task_id: str, reason: str) -> TaskEvent:
    return TaskEvent(
        task_id=task_id,
        name="example_event",
        reason=reason,
        payload={"status": "ok"},
        created_at=datetime(2026, 6, 6, tzinfo=UTC),
    )
