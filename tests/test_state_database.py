from contextlib import closing
from datetime import UTC, datetime
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.storage.task_store import TaskRecord, TaskStatus

from cli_downloader.storage.state_database import (
    CompletionRepository,
    ExecutionRepository,
    HistoryRepository,
    HistoryRecordRepository,
    StateDatabase,
    StateSchemaVersionError,
    StateUnitOfWork,
    StateUnitOfWorkError,
    TaskRepository,
)


class StateDatabaseTest(TestCase):
    def test_task_repository_supports_transactional_cas(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            with StateUnitOfWork(database) as unit_of_work:
                record = TaskRecord("task-1", TaskStatus.PENDING)
                unit_of_work.tasks.upsert(record)
                updated = record.transition(status=TaskStatus.RUNNING)
                self.assertTrue(unit_of_work.tasks.compare_and_swap(updated, record.revision))
                self.assertEqual(unit_of_work.tasks.get("task-1").status, TaskStatus.RUNNING)

    def test_history_events_are_ordered_and_do_not_require_a_task_row(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            first = TaskEvent("deleted-task", "task_created", "request", {"title": "Saved title"})
            second = TaskEvent("deleted-task", "task_finished", "completed")

            with StateUnitOfWork(database) as unit_of_work:
                first_record = unit_of_work.history.append(first, run_id="run-1", task_revision=3)
                second_record = unit_of_work.history.append(second, run_id="run-1", task_revision=4)
                unit_of_work.completion.record_completed("example", "content-1")

            with StateUnitOfWork(database) as unit_of_work:
                records = unit_of_work.history.list_records(task_id="deleted-task")
                self.assertEqual((first_record.task_sequence, second_record.task_sequence), (1, 2))
                self.assertEqual(records, (first_record, second_record))
                unit_of_work.history.delete_task("deleted-task")
                self.assertFalse(unit_of_work.history.list(task_id="deleted-task"))
                self.assertTrue(unit_of_work.completion.is_completed("example", "content-1"))

    def test_bootstraps_versioned_database_and_migration_journal(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state" / "state.sqlite3")

            self.assertTrue(database.path.exists())
            self.assertEqual(database.schema_version, 12)
            self.assertEqual(
                tuple((record.version, record.name) for record in database.migration_records()),
                (
                    (1, "bootstrap_state_database"),
                    (2, "create_completion_ledger"),
                    (4, "create_tasks"),
                    (5, "create_execution_roots"),
                    (6, "create_execution_details"),
                    (8, "create_history_events"),
                    (9, "add_execution_identity_columns"),
                    (10, "create_download_history"),
                    (11, "backfill_download_history"),
                    (12, "create_site_cooldowns"),
                ),
            )
            with closing(sqlite3.connect(database.path)) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_import_journal'"
                    ).fetchone()
                )

    def test_reopening_database_does_not_reapply_migration(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            StateDatabase(db_path)

            reopened = StateDatabase(db_path)

            self.assertEqual(reopened.schema_version, 12)
            self.assertEqual(len(reopened.migration_records()), 10)

    def test_rejects_newer_schema_before_any_repository_can_write(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            StateDatabase(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "UPDATE schema_metadata SET value = '13' WHERE key = 'schema_version'"
                )
                connection.commit()

            with self.assertRaisesRegex(StateSchemaVersionError, "newer than supported"):
                StateDatabase(db_path)

    def test_unit_of_work_commits_all_repository_writes_together(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")

            with StateUnitOfWork(database) as unit_of_work:
                self.assertIsInstance(unit_of_work.tasks, TaskRepository)
                self.assertIsInstance(unit_of_work.execution, ExecutionRepository)
                self.assertIsInstance(unit_of_work.completion, CompletionRepository)
                self.assertIsInstance(unit_of_work.history, HistoryRepository)
                self.assertIsInstance(unit_of_work.history_records, HistoryRecordRepository)
                self.assertIs(unit_of_work.tasks.connection, unit_of_work.execution.connection)
                self.assertIs(unit_of_work.tasks.connection, unit_of_work.completion.connection)
                self.assertIs(unit_of_work.tasks.connection, unit_of_work.history.connection)
                self.assertIs(
                    unit_of_work.tasks.connection, unit_of_work.history_records.connection
                )
                unit_of_work.connection.execute(
                    "CREATE TABLE transaction_probe (value TEXT NOT NULL)"
                )
                unit_of_work.connection.execute(
                    "INSERT INTO transaction_probe (value) VALUES ('ok')"
                )

            with closing(sqlite3.connect(database.path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM transaction_probe").fetchone(),
                    ("ok",),
                )

    def test_unit_of_work_rolls_back_all_writes_after_error(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            with closing(sqlite3.connect(database.path)) as connection:
                connection.execute("CREATE TABLE transaction_probe (value TEXT NOT NULL)")
                connection.commit()

            with self.assertRaisesRegex(RuntimeError, "stop"):
                with StateUnitOfWork(database) as unit_of_work:
                    unit_of_work.connection.execute(
                        "INSERT INTO transaction_probe (value) VALUES ('discard')"
                    )
                    raise RuntimeError("stop")

            with closing(sqlite3.connect(database.path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM transaction_probe").fetchall(),
                    [],
                )

    def test_repository_access_requires_active_unit_of_work(self) -> None:
        with TemporaryDirectory() as directory:
            unit_of_work = StateUnitOfWork(StateDatabase(Path(directory) / "state.sqlite3"))

            with self.assertRaises(StateUnitOfWorkError):
                _ = unit_of_work.completion.connection

    def test_history_records_keep_identity_after_task_deletion(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            record = DownloadHistoryRecord(
                record_id="history-1",
                task_id="task-1",
                kind=HistoryKind.ITEM,
                site_id="pixiv",
                title="Illustration",
                source_url="https://pixiv.example/1",
                status="completed",
                completed_files=2,
                total_files=2,
                output_files=("C:/downloads/a.jpg", "C:/downloads/b.jpg"),
            )

            with StateUnitOfWork(database) as unit_of_work:
                unit_of_work.tasks.upsert(TaskRecord("task-1", TaskStatus.COMPLETED))
                unit_of_work.history_records.upsert(record)
                unit_of_work.tasks.delete("task-1")

            with StateUnitOfWork(database) as unit_of_work:
                self.assertEqual(unit_of_work.history_records.get("history-1"), record)

    def test_history_records_order_finished_runs_deterministically(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            created = datetime(2026, 7, 11, tzinfo=UTC)
            records = (
                DownloadHistoryRecord("h-1", "task-1", HistoryKind.ITEM, created_at=created),
                DownloadHistoryRecord("h-2", "task-2", HistoryKind.ITEM, created_at=created),
            )
            with StateUnitOfWork(database) as unit_of_work:
                for item in records:
                    unit_of_work.history_records.upsert(item)
                self.assertEqual(
                    tuple(item.record_id for item in unit_of_work.history_records.list()),
                    ("h-1", "h-2"),
                )

    def test_history_backfill_uses_audit_identity_without_task_reads(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            StateDatabase(db_path)
            created_at = datetime(2026, 7, 11, tzinfo=UTC).isoformat()
            events = (
                (
                    "parent-1",
                    "batch_created",
                    "selection_confirmed",
                    {"site_id": "pixiv", "title": "Artist batch"},
                ),
                (
                    "entry-1",
                    "task_persisted",
                    "batch_entry_persisted",
                    {
                        "batch_id": "parent-1",
                        "plugin_id": "pixiv_plugin",
                        "site_id": "pixiv",
                        "title": "Artwork",
                        "source_url": "https://pixiv.example/art/1",
                    },
                ),
                (
                    "parent-1",
                    "batch_progress",
                    "package_persisted",
                    {"entry_id": "entry-1", "child_id": "child-1", "asset_count": 2},
                ),
                (
                    "parent-1",
                    "batch_progress",
                    "child_completed",
                    {
                        "child_id": "child-1",
                        "completed_assets": 2,
                        "total_assets": 2,
                        "output_files": ["C:/downloads/art-1.jpg", "C:/downloads/art-2.jpg"],
                    },
                ),
            )
            with closing(sqlite3.connect(db_path)) as connection:
                sequences: dict[str, int] = {}
                for task_id, name, reason, payload in events:
                    sequences[task_id] = sequences.get(task_id, 0) + 1
                    connection.execute(
                        """
                        INSERT INTO task_events
                            (task_id, task_sequence, name, reason, payload_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            sequences[task_id],
                            name,
                            reason,
                            json.dumps(payload),
                            created_at,
                        ),
                    )
                connection.execute("DROP TABLE site_cooldowns")
                connection.execute("DELETE FROM migration_journal WHERE version >= 11")
                connection.execute(
                    "UPDATE schema_metadata SET value = '10' WHERE key = 'schema_version'"
                )
                connection.commit()

            reopened = StateDatabase(db_path)
            with StateUnitOfWork(reopened) as unit_of_work:
                parent = unit_of_work.history_records.get("parent-1")
                child = unit_of_work.history_records.get("child-1")
                self.assertEqual(parent.title, "Artist batch")
                self.assertEqual(child.title, "Artwork")
                self.assertEqual(child.source_url, "https://pixiv.example/art/1")
                self.assertEqual(child.completed_files, 2)
                self.assertEqual(child.output_files[0], "C:/downloads/art-1.jpg")
