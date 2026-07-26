from tests.state_store_support import StateBatchStore
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.application.batch_service import BatchService
from cli_downloader.core.events import TaskEvent
from cli_downloader.cli.app import (
    _cancel_tasks,
    _delete_task_records,
    _emit_hard_retry_error,
    _hard_retry_task,
    _print_hard_retry_summary,
)
from cli_downloader.cli.shell import _format_shell_output
from cli_downloader.cli.tasks import task_record_to_dict
from tests.example_site import build_example_runtime
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_batch_store import StateBatchStore as UnifiedStateBatchStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.task_store import InMemoryTaskStore, TaskRecord, TaskStatus
from cli_downloader.core.operations import ListItemData


class TaskPresenterTest(TestCase):
    def test_task_record_to_dict_includes_metadata(self) -> None:
        record = TaskRecord(
            task_id="task-1",
            status=TaskStatus.PLANNED,
            site_id="example",
            source_url="example://gallery/welcome",
            title="Example Item",
            metadata={"asset_count": 2},
        )

        payload = task_record_to_dict(record)

        self.assertEqual(payload["task_id"], "task-1")
        self.assertEqual(payload["status"], "planned")
        self.assertEqual(payload["metadata"], {"asset_count": 2})

    def test_task_list_console_uses_compact_table(self) -> None:
        output = _format_shell_output(
            json.dumps(
                [
                    {
                        "task_id": "task-1",
                        "type": "item",
                        "name": "A very long gallery name",
                        "progress": "3/10",
                        "status": "running",
                        "created_at": "2026-07-07T12:00:00+08:00",
                    }
                ]
            )
        )

        self.assertIn("Type", output)
        self.assertIn("Name", output)
        self.assertIn("3/10", output)
        self.assertNotIn("task_id", output)

    def test_bulk_delete_requires_confirmation_and_preserves_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output_file = root / "downloads" / "one.jpg"
            output_file.parent.mkdir()
            output_file.write_bytes(b"done")
            tasks = InMemoryTaskStore()
            tasks.upsert(
                TaskRecord(
                    task_id="task-1",
                    status=TaskStatus.COMPLETED,
                    metadata={"output_files": [str(output_file)]},
                )
            )
            service = TaskHierarchyService(
                task_store=tasks,
                batch_store=StateBatchStore(root / "state.sqlite3"),
            )

            preview = StringIO()
            with redirect_stdout(preview):
                code = _delete_task_records(
                    service,
                    task_ids=(),
                    status="completed",
                    older_than_days=None,
                    dry_run=True,
                    confirmed=False,
                )
            self.assertEqual(code, 0)
            self.assertIsNotNone(tasks.get("task-1"))

            with redirect_stdout(StringIO()):
                code = _delete_task_records(
                    service,
                    task_ids=("task-1",),
                    status=None,
                    older_than_days=None,
                    dry_run=False,
                    confirmed=True,
                )
            self.assertEqual(code, 0)
            self.assertIsNone(tasks.get("task-1"))
            self.assertTrue(output_file.exists())


class HardRetryTest(TestCase):
    def test_emit_error_plain_output(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            _emit_hard_retry_error("something went wrong", json_output=False)

        self.assertIn("Error: something went wrong", output.getvalue())
        self.assertNotIn("{", output.getvalue())

    def test_emit_error_json_output(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            _emit_hard_retry_error("something went wrong", json_output=True)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["error"], "something went wrong")

    def test_print_summary_includes_task_info(self) -> None:
        results = [
            {
                "task_id": "task-abc123",
                "type": "item",
                "name": "Example Item: welcome",
            }
        ]
        output = StringIO()
        with redirect_stdout(output):
            _print_hard_retry_summary(results)

        text = output.getvalue()
        self.assertIn("task-abc123", text)
        self.assertIn("Example Item: welcome", text)
        self.assertIn("Hard-retry", text)

    def test_print_summary_batch_includes_entries_count(self) -> None:
        results = [
            {
                "parent_id": "batch-xyz",
                "type": "batch",
                "title": "My Batch",
                "entries": 3,
                "skipped": 0,
            }
        ]
        output = StringIO()
        with redirect_stdout(output):
            _print_hard_retry_summary(results)

        text = output.getvalue()
        self.assertIn("batch-xyz", text)
        self.assertIn("My Batch", text)
        self.assertIn("3", text)

    def test_standalone_task_hard_retry(self) -> None:
        runtime = build_example_runtime()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = InMemoryTaskStore()
            tasks.upsert(
                TaskRecord(
                    task_id="old-task-1",
                    status=TaskStatus.COMPLETED,
                    site_id="example",
                    source_url="example://gallery/welcome",
                    title="Old Example Task",
                )
            )
            original_created_at = tasks.get("old-task-1").created_at
            batch_db = root / "state.sqlite3"
            history = StateHistoryStore(StateDatabase(root / "state.sqlite3"))
            completion = SQLiteCompletionStore(StateDatabase(root / "state.sqlite3"))
            completion.record_completed("example", "example://gallery/welcome")
            history.append(TaskEvent("old-task-1", "download", "preexisting"))

            output = StringIO()
            with redirect_stdout(output):
                code = _hard_retry_task(
                    "old-task-1",
                    site_registry=runtime.site_registry,
                    runtime=runtime,
                    _task_store=tasks,
                    _batch_store=StateBatchStore(batch_db),
                    _history_store=history,
                )

            self.assertEqual(code, 0)
            retried = tasks.get("old-task-1")
            self.assertIsNotNone(retried)
            self.assertEqual(retried.task_id, "old-task-1")
            self.assertEqual(retried.status, TaskStatus.PLANNED)
            self.assertEqual(retried.created_at, original_created_at)
            self.assertEqual(retried.source_url, "example://gallery/welcome")
            self.assertEqual(
                tuple(event.reason for event in history.list(task_id="old-task-1")),
                ("preexisting", "hard_retry_requested", "planned_task_persisted"),
            )
            self.assertTrue(completion.is_completed("example", "example://gallery/welcome"))
            # Summary output (not JSON by default).
            self.assertNotIn("{", output.getvalue())

    def test_standalone_task_hard_retry_json_output(self) -> None:
        runtime = build_example_runtime()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = InMemoryTaskStore()
            tasks.upsert(
                TaskRecord(
                    task_id="old-task-2",
                    status=TaskStatus.COMPLETED,
                    site_id="example",
                    source_url="example://gallery/welcome",
                    title="Old Example Task",
                )
            )
            batch_db = root / "state.sqlite3"
            history = StateHistoryStore(StateDatabase(root / "state.sqlite3"))

            output = StringIO()
            with redirect_stdout(output):
                code = _hard_retry_task(
                    "old-task-2",
                    site_registry=runtime.site_registry,
                    runtime=runtime,
                    json_output=True,
                    _task_store=tasks,
                    _batch_store=StateBatchStore(batch_db),
                    _history_store=history,
                )

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertIsInstance(payload, dict)
            self.assertEqual(payload["task_id"], "old-task-2")
            self.assertEqual(tasks.get("old-task-2").status, TaskStatus.PLANNED)

    def test_batch_hard_retry_preserves_parent_and_history_identity(self) -> None:
        runtime = build_example_runtime()
        with TemporaryDirectory() as directory:
            db = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(db)
            batches = UnifiedStateBatchStore(db)
            history = StateHistoryStore(db)
            creation = BatchService(
                store=batches,
                task_store=tasks,
                history_store=history,
            ).confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="browse_list",
                items=(
                    ListItemData(
                        item_id="welcome",
                        source_url="https://example.test/gallery/welcome",
                        title="Welcome",
                    ),
                ),
            )
            parent_id = creation.snapshot.parent.parent_id
            original_entries = batches.list_entries(parent_id)
            original_created_at = tasks.get(parent_id).created_at

            output = StringIO()
            with redirect_stdout(output):
                code = _hard_retry_task(
                    parent_id,
                    site_registry=runtime.site_registry,
                    runtime=runtime,
                    json_output=True,
                    _task_store=tasks,
                    _batch_store=batches,
                    _history_store=history,
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["parent_id"], parent_id)
            self.assertEqual(tasks.get(parent_id).status, TaskStatus.PLANNED)
            self.assertEqual(tasks.get(parent_id).created_at, original_created_at)
            self.assertIsNotNone(history.get_record(parent_id))
            self.assertEqual(len(batches.list_entries(parent_id)), len(original_entries))
            self.assertNotEqual(
                batches.list_entries(parent_id)[0].entry_id,
                original_entries[0].entry_id,
            )

    def test_hard_retry_nonexistent_task_errors(self) -> None:
        runtime = build_example_runtime()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = InMemoryTaskStore()
            batch_db = root / "state.sqlite3"
            history = StateHistoryStore(StateDatabase(root / "state.sqlite3"))

            output = StringIO()
            with redirect_stdout(output):
                code = _hard_retry_task(
                    "nonexistent-id",
                    site_registry=runtime.site_registry,
                    runtime=runtime,
                    _task_store=tasks,
                    _batch_store=StateBatchStore(batch_db),
                    _history_store=history,
                )

            self.assertEqual(code, 1)
            self.assertIn("no source URL", output.getvalue())


class CancelTasksTest(TestCase):
    def _make_service(self, task_store, directory):
        return TaskHierarchyService(
            task_store=task_store,
            batch_store=StateBatchStore(Path(directory) / "state.sqlite3"),
        )

    def test_cancel_single_task(self) -> None:
        tasks = InMemoryTaskStore()
        tasks.upsert(
            TaskRecord(
                task_id="task-1",
                status=TaskStatus.RUNNING,
                site_id="example",
                source_url="example://gallery/welcome",
            )
        )
        with TemporaryDirectory() as d:
            service = self._make_service(tasks, d)

            output = StringIO()
            with redirect_stdout(output):
                code = _cancel_tasks(
                    service,
                    task_id="task-1",
                    cancel_all=False,
                    confirmed=False,
                )

        self.assertEqual(code, 0)
        self.assertEqual(tasks.get("task-1").status, TaskStatus.CANCELED)
        self.assertIn("Cancelled", output.getvalue())

    def test_cancel_single_task_json_output(self) -> None:
        tasks = InMemoryTaskStore()
        tasks.upsert(
            TaskRecord(
                task_id="task-2",
                status=TaskStatus.PLANNED,
                site_id="example",
                source_url="example://gallery/welcome",
            )
        )
        with TemporaryDirectory() as d:
            service = self._make_service(tasks, d)

            output = StringIO()
            with redirect_stdout(output):
                code = _cancel_tasks(
                    service,
                    task_id="task-2",
                    cancel_all=False,
                    confirmed=False,
                    json_output=True,
                )

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertIn("task-2", payload["cancelled"])

    def test_cancel_completed_task_is_noop(self) -> None:
        tasks = InMemoryTaskStore()
        tasks.upsert(
            TaskRecord(
                task_id="task-3",
                status=TaskStatus.COMPLETED,
                site_id="example",
                source_url="example://gallery/welcome",
            )
        )
        with TemporaryDirectory() as d:
            service = self._make_service(tasks, d)

            output = StringIO()
            with redirect_stdout(output):
                code = _cancel_tasks(
                    service,
                    task_id="task-3",
                    cancel_all=False,
                    confirmed=False,
                )

        # Completed tasks are not cancellable; should error.
        self.assertEqual(code, 1)
        self.assertEqual(tasks.get("task-3").status, TaskStatus.COMPLETED)

    def test_cancel_all_requires_confirmation(self) -> None:
        tasks = InMemoryTaskStore()
        with TemporaryDirectory() as d:
            service = self._make_service(tasks, d)

            output = StringIO()
            with redirect_stdout(output):
                code = _cancel_tasks(
                    service,
                    task_id=None,
                    cancel_all=True,
                    confirmed=False,
                )

        self.assertEqual(code, 2)
        self.assertIn("--yes", output.getvalue())

    def test_cancel_all_cancels_all_cancellable_tasks(self) -> None:
        tasks = InMemoryTaskStore()
        tasks.upsert(
            TaskRecord(
                task_id="c-1",
                status=TaskStatus.RUNNING,
                site_id="example",
                source_url="example://gallery/a",
            )
        )
        tasks.upsert(
            TaskRecord(
                task_id="c-2",
                status=TaskStatus.PLANNED,
                site_id="example",
                source_url="example://gallery/b",
            )
        )
        tasks.upsert(
            TaskRecord(
                task_id="c-3",
                status=TaskStatus.COMPLETED,
                site_id="example",
                source_url="example://gallery/c",
            )
        )
        with TemporaryDirectory() as d:
            service = self._make_service(tasks, d)

            output = StringIO()
            with redirect_stdout(output):
                code = _cancel_tasks(
                    service,
                    task_id=None,
                    cancel_all=True,
                    confirmed=True,
                )

        self.assertEqual(code, 0)
        self.assertEqual(tasks.get("c-1").status, TaskStatus.CANCELED)
        self.assertEqual(tasks.get("c-2").status, TaskStatus.CANCELED)
        self.assertEqual(tasks.get("c-3").status, TaskStatus.COMPLETED)
        self.assertIn("Cancelled 2", output.getvalue())

    def test_cancel_missing_task_id_and_all(self) -> None:
        tasks = InMemoryTaskStore()
        with TemporaryDirectory() as d:
            service = self._make_service(tasks, d)

            output = StringIO()
            with redirect_stdout(output):
                code = _cancel_tasks(
                    service,
                    task_id=None,
                    cancel_all=False,
                    confirmed=False,
                )

        self.assertEqual(code, 2)
        self.assertIn("TASK_ID", output.getvalue())
