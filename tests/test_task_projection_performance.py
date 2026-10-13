from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.core.batch import BatchParent
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.state_database import StateDatabase, StateUnitOfWork
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus
from tests.test_task_hierarchy_service import _entry


class TracedDatabase(StateDatabase):
    def _connect(self):
        connection = super()._connect()
        if hasattr(self, "queries"):
            connection.set_trace_callback(self.queries.append)
        return connection


class TaskProjectionPerformanceTests(TestCase):
    def test_mixed_roots_are_bounded_and_aggregated_in_six_queries(self):
        with TemporaryDirectory() as directory:
            database = TracedDatabase(Path(directory) / "state.sqlite3")
            tasks, batches = StateTaskStore(database), StateBatchStore(database)
            for index in range(105):
                task_id = f"batch-{index}"
                tasks.upsert(TaskRecord(task_id, TaskStatus.PLANNED, kind=TaskKind.BATCH))
                entry = _entry(f"entry-{index}", task_id)
                batches.create_batch(BatchParent(task_id, (entry.entry_id,)), (entry,))
                tasks.upsert(TaskRecord(f"item-{index}", TaskStatus.PLANNED))
                tasks.upsert(TaskRecord(entry.entry_id, TaskStatus.PLANNED))

            class Scheduler:
                active_calls = 0
                waiting_calls = 0

                def active_snapshot_sync(self):
                    self.active_calls += 1
                    return ()

                def waiting_snapshot_sync(self):
                    self.waiting_calls += 1
                    return tuple(
                        {
                            "root_id": f"item-{i}",
                            "position": i + 1,
                            "sequence": 0,
                            "reason": "global_slot",
                        }
                        for i in range(105)
                    )

            scheduler = Scheduler()
            service = TaskHierarchyService(
                task_store=tasks, batch_store=batches, scheduler=scheduler
            )
            database.queries = []
            page = service.list_root_page(offset=100, limit=100)
            selects = [q for q in database.queries if q.lstrip().upper().startswith("SELECT")]
            self.assertEqual(len(selects), 6)
            self.assertEqual(page.total_count, 210)
            self.assertEqual(len(page.items), 100)
            self.assertEqual(page.items[0].task_id, "item-54")
            self.assertEqual(page.items[-1].task_id, "batch-5")
            self.assertEqual(scheduler.active_calls, 1)
            self.assertEqual(scheduler.waiting_calls, 1)
            self.assertEqual(page.items[0].details["scheduler_queue_position"], 55)
            self.assertEqual(page.items[1].progress.preparing_items, 1)
            self.assertNotIn("BEGIN IMMEDIATE", database.queries)
            self.assertEqual(len(service.list_root_page(offset=200).items), 10)

    def test_reader_sees_committed_state_while_writer_holds_reserved_lock(self):
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            record = TaskRecord("task", TaskStatus.PLANNED)
            tasks.upsert(record)
            with StateUnitOfWork(database) as writer:
                writer.tasks.upsert(record.transition(status=TaskStatus.RUNNING))
                self.assertEqual(tasks.get("task").status, TaskStatus.PLANNED)
                self.assertEqual(tasks.list_root_page()[1], 1)
            self.assertEqual(tasks.get("task").status, TaskStatus.RUNNING)
