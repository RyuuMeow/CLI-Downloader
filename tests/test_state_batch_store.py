from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.core.batch import (
    AssetStatus,
    AttemptData,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    DuplicateKind,
    DownloadAsset,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureOrigin,
    FailureStage,
)
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.events import TaskEvent
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus


class StateBatchStoreTest(TestCase):
    def test_persists_ordered_selection_root_and_entry_transitions(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            StateTaskStore(database).upsert(
                TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH)
            )
            store = StateBatchStore(database)
            first = _entry("entry-1")
            second = _entry("entry-2")

            store.create_batch(BatchParent("parent-1", ("entry-1", "entry-2")), (first, second))
            resolving = replace(first, status=BatchEntryStatus.RESOLVING)
            failed = replace(
                resolving,
                status=BatchEntryStatus.RESOLUTION_FAILED,
                failure=FailureData(
                    origin=FailureOrigin.PLUGIN,
                    stage=FailureStage.RESOLVE,
                    code="unavailable",
                    message="No longer available",
                    retryable=True,
                ),
            )
            store.update_entry(resolving)
            store.update_entry(failed)

            snapshot = store.get_snapshot("parent-1")
            self.assertEqual(store.list_parent_ids(), ("parent-1",))
            self.assertEqual(store.list_entries("parent-1"), (failed, second))
            self.assertEqual(snapshot.parent.entry_ids, ("entry-1", "entry-2"))
            self.assertEqual(
                store.find_duplicates("example", ("candidate-entry-2",)),
                {"candidate-entry-2": DuplicateKind.ACTIVE},
            )

    def test_delete_batch_keeps_its_task_and_history_ownership_separate(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            tasks.upsert(TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH))
            store = StateBatchStore(database)
            entry = _entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))

            store.delete_batch("parent-1")

            self.assertIsNotNone(tasks.get("parent-1"))
            self.assertEqual(store.list_parent_ids(), ())

    def test_child_handoff_persists_package_assets_and_transitions(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            StateTaskStore(database).upsert(
                TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH)
            )
            store = StateBatchStore(database)
            entry = _entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild("child-1", "parent-1", entry.entry_id, _package())

            store.attach_child(entry.entry_id, child)
            downloading = replace(child, status=DownloadChildStatus.DOWNLOADING)
            store.update_child(downloading)

            self.assertEqual(store.get_package("package-1", 1), child.package)
            self.assertEqual(store.list_children("parent-1"), (downloading,))
            self.assertEqual(store.get_entry(entry.entry_id).child_id, child.child_id)
            self.assertEqual(store.list_assets(child.child_id)[0].asset, child.package.assets[0])
            self.assertEqual(
                store.find_duplicates("example", (entry.candidate_id,)),
                {entry.candidate_id: DuplicateKind.ACTIVE},
            )

    def test_replaces_package_preserves_completed_assets_and_records_attempts(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            StateTaskStore(database).upsert(
                TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH)
            )
            store = StateBatchStore(database)
            entry = _entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild("child-1", "parent-1", entry.entry_id, _package())
            store.attach_child(entry.entry_id, child)
            original = store.list_assets(child.child_id)[0]
            store.update_asset(child.child_id, replace(original, status=AssetStatus.DOWNLOADING))
            completed = replace(original, status=AssetStatus.COMPLETED, final_path="001.jpg")
            store.update_asset(child.child_id, completed)

            updated = store.replace_child_package(child.child_id, _package(version=2))
            attempt = AttemptData("attempt-1", child.child_id, 1)
            store.append_attempt(attempt)

            self.assertEqual(updated.package.version, 2)
            self.assertEqual(store.list_assets(child.child_id), (completed,))
            self.assertEqual(store.list_attempts(child.child_id), (attempt,))

    def test_terminal_child_completion_commits_completion_and_history_together(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            StateTaskStore(database).upsert(
                TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH)
            )
            store = StateBatchStore(database)
            entry = _entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild("child-1", "parent-1", entry.entry_id, _package())
            store.attach_child(entry.entry_id, child)
            completed = replace(child, status=DownloadChildStatus.COMPLETED)

            store.finalize_child(
                completed,
                completion_identity=("example", child.package.source_url),
                history_events=(TaskEvent("parent-1", "batch_progress", "child_completed"),),
            )

            self.assertEqual(store.get_child(child.child_id), completed)
            self.assertTrue(
                SQLiteCompletionStore(database).is_completed("example", child.package.source_url)
            )
            self.assertEqual(StateHistoryStore(database).list()[0].reason, "child_completed")


def _entry(entry_id: str) -> BatchEntry:
    return BatchEntry(
        entry_id=entry_id,
        parent_id="parent-1",
        plugin_id="example_plugin",
        site_id="example",
        candidate_id=f"candidate-{entry_id}",
        source_operation_id="search",
        source_url=f"https://example.test/gallery/{entry_id}",
    )


def _package(*, version: int = 1) -> DownloadPackage:
    return DownloadPackage(
        package_id="package-1",
        version=version,
        plugin_id="example_plugin",
        site_id="example",
        gallery_id="gallery-1",
        source_operation_id="resolve",
        source_url="https://example.test/gallery/1",
        strategy=DownloadStrategy.DIRECT,
        assets=(
            DownloadAsset(
                site_id="example",
                gallery_id="gallery-1",
                plugin_asset_key="page-1",
                url="https://cdn.example.test/1.jpg",
                filename="001.jpg",
                index=0,
            ),
        ),
        expires_at=datetime(2026, 7, 11, tzinfo=UTC),
    )
