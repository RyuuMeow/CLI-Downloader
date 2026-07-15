from tests.state_store_support import StateBatchStore, StateTaskStore
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.core.batch import (
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    DownloadAsset,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureOrigin,
    FailureStage,
)
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.events import TaskEvent
from cli_downloader.core.errors import InvalidTaskTransitionError
from cli_downloader.core.task_tree import TaskKind, TaskState
from cli_downloader.storage.history_store import InMemoryHistoryStore
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_batch_store import StateBatchStore as ProductionStateBatchStore
from cli_downloader.storage.state_task_store import StateTaskStore as ProductionStateTaskStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus


def _entry(entry_id: str, parent_id: str = "batch-1") -> BatchEntry:
    return BatchEntry(
        entry_id=entry_id,
        parent_id=parent_id,
        plugin_id="example",
        site_id="example",
        candidate_id=entry_id,
        source_operation_id="resolve_gallery",
        source_url=f"https://example.test/gallery/{entry_id}",
        title=f"Gallery {entry_id}",
    )


def _failure() -> FailureData:
    return FailureData(
        origin=FailureOrigin.SYSTEM,
        stage=FailureStage.FETCH,
        code="fetch_failed",
        message="Fetch failed",
        retryable=True,
    )


def _attach_partial(store: StateBatchStore, entry: BatchEntry) -> Path:
    package = DownloadPackage(
        package_id=f"package-{entry.entry_id}",
        version=1,
        plugin_id=entry.plugin_id,
        site_id=entry.site_id,
        gallery_id=entry.candidate_id,
        source_operation_id=entry.source_operation_id,
        source_url=entry.source_url,
        strategy=DownloadStrategy.DIRECT,
        assets=tuple(
            DownloadAsset(
                site_id=entry.site_id,
                gallery_id=entry.candidate_id,
                plugin_asset_key=str(index),
                url=f"https://cdn.example.test/{index}.jpg",
                filename=f"{index}.jpg",
                index=index,
            )
            for index in (1, 2)
        ),
    )
    store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
    child = DownloadChild(
        child_id=f"child-{entry.entry_id}",
        parent_id=entry.parent_id,
        entry_id=entry.entry_id,
        package=package,
    )
    store.attach_child(entry.entry_id, child)
    store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
    first, second = store.list_assets(child.child_id)
    output = Path("output") / first.asset.filename
    store.update_asset(
        child.child_id,
        replace(first, status=AssetStatus.DOWNLOADING, attempt_count=1),
    )
    store.update_asset(
        child.child_id,
        replace(
            store.list_assets(child.child_id)[0],
            status=AssetStatus.COMPLETED,
            final_path=str(output),
        ),
    )
    store.update_asset(
        child.child_id,
        replace(second, status=AssetStatus.DOWNLOADING, attempt_count=1),
    )
    store.update_asset(
        child.child_id,
        replace(
            store.list_assets(child.child_id)[1], status=AssetStatus.FAILED, failure=_failure()
        ),
    )
    store.update_child(
        replace(
            child,
            status=DownloadChildStatus.PARTIALLY_COMPLETED,
            failure=_failure(),
        )
    )
    return output


class _TerminalRaceCoordinator:
    def __init__(self, task_store: StateTaskStore) -> None:
        self._task_store = task_store

    def request_control_sync(self, _action, task_id: str) -> None:
        record = self._task_store.get(task_id)
        assert record is not None
        self._task_store.upsert(record.transition(status=TaskStatus.COMPLETED))
        raise InvalidTaskTransitionError("task finished before control CAS")


class _FinishedBatchPipeline:
    def is_running(self, _task_id: str) -> bool:
        return True

    def pause(self, _task_id: str) -> bool:
        return False


class TaskHierarchyServiceTest(TestCase):
    def test_available_folder_uses_item_metadata_and_bounded_batch_assets(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            tasks.upsert(
                TaskRecord(
                    "item-1",
                    TaskStatus.COMPLETED,
                    kind=TaskKind.ITEM,
                    metadata={"output_files": ["downloads/item-1/001.jpg"]},
                )
            )
            entry = _entry("entry-1")
            batches.create_batch(BatchParent("batch-1", (entry.entry_id,)), (entry,))
            _attach_partial(batches, entry)
            service = TaskHierarchyService(task_store=tasks, batch_store=batches)

            self.assertEqual(
                service.get_available_folder("item-1"),
                str(Path("downloads") / "item-1"),
            )
            self.assertEqual(service.get_available_folder("entry-1"), "output")
            self.assertEqual(service.get_available_folder("batch-1"), "output")

    def test_large_batch_root_projection_does_not_materialise_children(self) -> None:
        class GuardBatchStore(ProductionStateBatchStore):
            def list_entries(self, _parent_id):
                raise AssertionError("root projection decoded all entries")

            def list_children(self, _parent_id):
                raise AssertionError("root projection materialised all children")

            def list_assets(self, _child_id):
                raise AssertionError("root projection read child assets")

            def list_attempts(self, _child_id):
                raise AssertionError("root projection read child attempts")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "state.sqlite3")
            tasks = ProductionStateTaskStore(database)
            batches = GuardBatchStore(database)
            entries = tuple(_entry(f"entry-{index}") for index in range(1000))
            tasks.upsert(TaskRecord("batch-1", TaskStatus.RUNNING, kind=TaskKind.BATCH))
            batches.create_batch(BatchParent("batch-1", tuple(entry.entry_id for entry in entries)), entries)

            summary = TaskHierarchyService(task_store=tasks, batch_store=batches).list_root_summaries()

            self.assertEqual(len(summary), 1)
            self.assertEqual(summary[0].progress.total_files, 0)
            self.assertEqual(summary[0].progress.preparing_items, 1000)
            self.assertEqual(summary[0].details["counts"]["unresolved"], 1000)

    def test_large_batch_child_projection_is_paged_and_ordered(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            batches = ProductionStateBatchStore(StateDatabase(root / "state.sqlite3"))
            entries = tuple(_entry(f"entry-{index}") for index in range(1000))
            StateTaskStore(StateDatabase(root / "state.sqlite3")).upsert(
                TaskRecord("batch-1", TaskStatus.PENDING, kind=TaskKind.BATCH)
            )
            batches.create_batch(BatchParent("batch-1", tuple(entry.entry_id for entry in entries)), entries)

            page = batches.list_child_summaries("batch-1", offset=400, limit=100)

            self.assertEqual(page.total_count, 1000)
            self.assertEqual(page.offset, 400)
            self.assertEqual(len(page.items), 100)
            self.assertEqual(page.items[0].task_id, "entry-400")
            self.assertEqual(page.items[-1].task_id, "entry-499")
            self.assertTrue(all(item.state is TaskState.PREPARING for item in page.items))

    def test_state_batch_delete_is_atomic_and_preserves_history_completion(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = ProductionStateTaskStore(database)
            batches = ProductionStateBatchStore(database)
            history = StateHistoryStore(database)
            completion = SQLiteCompletionStore(database)
            entry = _entry("entry-1")
            tasks.upsert(TaskRecord("batch-1", TaskStatus.PENDING, kind=TaskKind.BATCH))
            batches.create_batch(BatchParent("batch-1", (entry.entry_id,)), (entry,))
            history.append(TaskEvent("batch-1", "batch_created", "selected"))
            completion.record_completed("example", "gallery-entry-1")

            TaskHierarchyService(
                task_store=tasks,
                batch_store=batches,
                history_store=history,
            ).delete("batch-1")

            self.assertIsNone(tasks.get("batch-1"))
            self.assertEqual(batches.list_parent_ids(), ())
            self.assertEqual(len(history.list(task_id="batch-1")), 1)
            self.assertTrue(completion.is_completed("example", "gallery-entry-1"))

    def test_batch_parent_task_record_routes_as_batch(self) -> None:
        """Repository routing must use Task kind, never TaskRecord presence."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            item = _entry("entry-1")
            batches.create_batch(BatchParent("batch-1", (item.entry_id,)), (item,))
            tasks.upsert(
                TaskRecord(
                    task_id="batch-1",
                    status=TaskStatus.PENDING,
                    kind=TaskKind.BATCH,
                )
            )
            service = TaskHierarchyService(task_store=tasks, batch_store=batches)

            self.assertEqual(service.get("batch-1").kind, TaskKind.BATCH)

    def test_batch_torrent_checkpoint_is_exposed_as_byte_progress(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = ProductionStateTaskStore(database)
            batches = ProductionStateBatchStore(database)
            entry = _entry("entry-1")
            tasks.upsert(TaskRecord("batch-1", TaskStatus.RUNNING, kind=TaskKind.BATCH))
            batches.create_batch(BatchParent("batch-1", (entry.entry_id,)), (entry,))
            batches.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild(
                child_id="child-entry-1",
                parent_id="batch-1",
                entry_id=entry.entry_id,
                package=DownloadPackage(
                    package_id="package-entry-1",
                    version=1,
                    plugin_id="example",
                    site_id="example",
                    gallery_id="entry-1",
                    source_operation_id="resolve_gallery",
                    source_url=entry.source_url,
                    strategy=DownloadStrategy.TORRENT,
                    assets=(
                        DownloadAsset(
                            site_id="example",
                            gallery_id="entry-1",
                            plugin_asset_key="torrent-1",
                            url="https://cdn.example.test/item.torrent",
                            filename="item.torrent",
                        ),
                    ),
                ),
            )
            batches.attach_child(entry.entry_id, child)
            batches.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            asset = batches.list_assets(child.child_id)[0]
            batches.update_asset(
                child.child_id,
                replace(
                    asset,
                    status=AssetStatus.DOWNLOADING,
                    metadata={
                        "torrent_checkpoint": {
                            "client_id": "abc123",
                            "completed_bytes": 512,
                            "total_bytes": 1024,
                        }
                    },
                ),
            )
            node = TaskHierarchyService(task_store=tasks, batch_store=batches).get("batch-1")

            self.assertEqual(node.progress.completed_bytes, 512)
            self.assertEqual(node.progress.total_bytes, 1024)

    def test_batch_torrent_checkpoint_clamps_legacy_excess_bytes(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = ProductionStateTaskStore(database)
            batches = ProductionStateBatchStore(database)
            entry = _entry("entry-legacy", "batch-legacy")
            tasks.upsert(TaskRecord("batch-legacy", TaskStatus.RUNNING, kind=TaskKind.BATCH))
            batches.create_batch(BatchParent("batch-legacy", (entry.entry_id,)), (entry,))
            child = DownloadChild(
                child_id="child-entry-legacy",
                parent_id="batch-legacy",
                entry_id=entry.entry_id,
                package=DownloadPackage(
                    package_id="package-entry-legacy",
                    version=1,
                    plugin_id="example",
                    site_id="example",
                    gallery_id="entry-legacy",
                    source_operation_id="resolve_gallery",
                    source_url=entry.source_url,
                    strategy=DownloadStrategy.TORRENT,
                    assets=(
                        DownloadAsset(
                            site_id="example",
                            gallery_id="entry-legacy",
                            plugin_asset_key="torrent-legacy",
                            url="https://cdn.example.test/item.torrent",
                            filename="item.torrent",
                        ),
                    ),
                ),
            )
            batches.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            batches.attach_child(entry.entry_id, child)
            asset = batches.list_assets(child.child_id)[0]
            batches.update_asset(
                child.child_id,
                replace(
                    asset,
                    status=AssetStatus.DOWNLOADING,
                    metadata={
                        "torrent_checkpoint": {
                            "client_id": "abc123",
                            "completed_bytes": 4096,
                            "total_bytes": 1024,
                        }
                    },
                ),
            )

            node = TaskHierarchyService(task_store=tasks, batch_store=batches).get("batch-legacy")

            self.assertEqual(node.progress.completed_bytes, 1024)
            self.assertEqual(node.progress.total_bytes, 1024)

    def test_late_batch_control_is_noop_after_terminal_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            item = _entry("entry-1")
            batches.create_batch(BatchParent("batch-1", (item.entry_id,)), (item,))
            tasks.upsert(TaskRecord("batch-1", TaskStatus.PENDING, kind=TaskKind.BATCH))
            service = TaskHierarchyService(
                task_store=tasks,
                batch_store=batches,
                batch_pipeline=_FinishedBatchPipeline(),
                execution_coordinator=_TerminalRaceCoordinator(tasks),
            )

            result = service.pause("batch-1")

            self.assertEqual(result.status, TaskStatus.COMPLETED)
            self.assertEqual(tasks.get("batch-1").status, TaskStatus.COMPLETED)

    def test_deleting_batch_task_record_deletes_aggregate(self) -> None:
        """Task deletion must remove its execution aggregate, not just its row."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            item = _entry("entry-1")
            batches.create_batch(BatchParent("batch-1", (item.entry_id,)), (item,))
            tasks.upsert(
                TaskRecord(
                    task_id="batch-1",
                    status=TaskStatus.PENDING,
                    kind=TaskKind.BATCH,
                )
            )
            service = TaskHierarchyService(task_store=tasks, batch_store=batches)

            service.delete("batch-1")

            self.assertNotIn("batch-1", batches.list_parent_ids())

    def test_creation_times_come_from_task_records_without_history_reads(self) -> None:
        class CountingHistoryStore(InMemoryHistoryStore):
            def __init__(self) -> None:
                super().__init__()
                self.list_calls: list[str | None] = []

            def list(self, *, task_id: str | None = None):
                self.list_calls.append(task_id)
                return super().list(task_id=task_id)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            history = CountingHistoryStore()
            created = datetime(2026, 7, 11, tzinfo=UTC)
            history.append(TaskEvent("first", "created", "test"))
            tasks.upsert(
                TaskRecord(task_id="first", status=TaskStatus.COMPLETED, created_at=created)
            )
            service = TaskHierarchyService(
                task_store=tasks,
                batch_store=StateBatchStore(root / "state.sqlite3"),
                history_store=history,
            )

            service.list_roots()
            service.list_roots()
            history.append(TaskEvent("second", "created", "test"))
            tasks.upsert(TaskRecord(task_id="second", status=TaskStatus.COMPLETED))
            service.list_roots()

            self.assertEqual(history.list_calls, [])
            self.assertEqual(
                service.get("first").details["created_at"], created.astimezone().isoformat()
            )

    def test_projects_standalone_items_batches_and_preparation_items(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            tasks.upsert(
                TaskRecord(
                    task_id="standalone",
                    status=TaskStatus.COMPLETED,
                    site_id="example",
                    title="Standalone",
                    metadata={
                        "completed_files": 2,
                        "total_files": 2,
                        "output_files": ["one.jpg", "two.jpg"],
                    },
                )
            )
            pending, partial = _entry("pending"), _entry("partial")
            batches.create_batch(
                BatchParent(
                    "batch-1",
                    ("pending", "partial"),
                    title="Artist illustrations",
                ),
                (pending, partial),
            )
            _attach_partial(batches, partial)

            service = TaskHierarchyService(task_store=tasks, batch_store=batches)
            roots = {task.task_id: task for task in service.list_roots()}
            children = {task.task_id: task for task in service.list_children("batch-1")}

            self.assertEqual(roots["standalone"].kind, TaskKind.ITEM)
            self.assertEqual(roots["standalone"].progress.completed_files, 2)
            self.assertEqual(roots["batch-1"].kind, TaskKind.BATCH)
            self.assertEqual(roots["batch-1"].title, "Artist illustrations")
            self.assertEqual(roots["batch-1"].state, TaskState.PREPARING)
            self.assertEqual(roots["batch-1"].progress.completed_files, 1)
            self.assertEqual(roots["batch-1"].progress.total_files, 2)
            self.assertEqual(roots["batch-1"].progress.preparing_items, 1)
            self.assertEqual(children["pending"].state, TaskState.PREPARING)
            self.assertEqual(children["partial"].state, TaskState.PARTIALLY_COMPLETED)
            self.assertEqual(children["partial"].title, "Gallery partial")
            self.assertIn("failure", children["partial"].details)
            files = service.list_files("partial")
            self.assertEqual(len(files), 2)
            self.assertEqual(files[0].state, "completed")
            self.assertEqual(files[1].state, "failed")
            self.assertEqual(files[1].attempts, 1)
            self.assertEqual(files[1].failure["origin"], "system")

    def test_batch_entry_cannot_be_deleted_and_keeps_aggregate_checkpoint(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            item = _entry("partial")
            batches.create_batch(BatchParent("batch-1", (item.entry_id,)), (item,))
            output = root / _attach_partial(batches, item)
            output.parent.mkdir(parents=True)
            output.write_bytes(b"already downloaded")
            service = TaskHierarchyService(task_store=tasks, batch_store=batches)

            with self.assertRaises(InvalidTaskTransitionError):
                service.delete(item.entry_id)

            self.assertEqual(service.list_children("batch-1")[0].task_id, item.entry_id)
            self.assertTrue(output.exists())

    def test_legacy_entry_task_record_stays_internal_and_rejects_public_controls(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = StateTaskStore(root / "state.sqlite3")
            batches = StateBatchStore(root / "state.sqlite3")
            item = _entry("entry-1")
            batches.create_batch(BatchParent("batch-1", (item.entry_id,)), (item,))
            tasks.upsert(
                TaskRecord(
                    task_id=item.entry_id,
                    status=TaskStatus.RUNNING,
                    kind=TaskKind.ITEM,
                )
            )
            service = TaskHierarchyService(task_store=tasks, batch_store=batches)

            self.assertNotIn(item.entry_id, {node.task_id for node in service.list_roots()})
            self.assertEqual(service.get(item.entry_id).parent_id, "batch-1")
            with self.assertRaises(InvalidTaskTransitionError):
                service.pause(item.entry_id)
            with self.assertRaises(InvalidTaskTransitionError):
                service.resume(item.entry_id)
            with self.assertRaises(InvalidTaskTransitionError):
                service.cancel(item.entry_id)
            self.assertEqual(
                batches.get_entry(item.entry_id).status, BatchEntryStatus.PENDING_RESOLUTION
            )

    def test_deleting_standalone_record_keeps_output_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output" / "one.jpg"
            output.parent.mkdir()
            output.write_bytes(b"downloaded")
            tasks = StateTaskStore(root / "state.sqlite3")
            tasks.upsert(
                TaskRecord(
                    task_id="standalone",
                    status=TaskStatus.COMPLETED,
                    metadata={"output_files": [str(output)]},
                )
            )
            service = TaskHierarchyService(
                task_store=tasks,
                batch_store=StateBatchStore(root / "state.sqlite3"),
            )

            service.delete("standalone")

            self.assertIsNone(tasks.get("standalone"))
            self.assertTrue(output.exists())
