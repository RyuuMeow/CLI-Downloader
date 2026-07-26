from tests.state_store_support import StateBatchStore
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    AttemptData,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    BatchExecutionStatus,
    Checkpoint,
    CheckpointData,
    DownloadAsset,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureOrigin,
    FailureStage,
    RetryPlan,
    RetryScope,
)
from cli_downloader.core.errors import BatchConflictError
from cli_downloader.core.models import DownloadStrategy


def make_entry(entry_id: str, **changes: object) -> BatchEntry:
    values: dict[str, object] = {
        "entry_id": entry_id,
        "parent_id": "parent-1",
        "plugin_id": "example_plugin",
        "site_id": "example",
        "candidate_id": f"candidate-{entry_id}",
        "source_operation_id": "search",
        "source_url": f"https://example.test/gallery/{entry_id}",
    }
    values.update(changes)
    return BatchEntry(**values)


def make_package(package_id: str = "package-1", gallery_id: str = "gallery-1") -> DownloadPackage:
    return DownloadPackage(
        package_id=package_id,
        version=1,
        plugin_id="example_plugin",
        site_id="example",
        gallery_id=gallery_id,
        source_operation_id="resolve_gallery",
        source_url=f"https://example.test/gallery/{gallery_id}",
        strategy=DownloadStrategy.DIRECT,
        assets=(
            DownloadAsset(
                site_id="example",
                gallery_id=gallery_id,
                plugin_asset_key="page-1",
                url=f"https://cdn.example.test/{gallery_id}/1.jpg?token=temporary",
                filename="001.jpg",
                index=0,
                metadata={"width": 1200},
            ),
        ),
        expires_at=datetime(2026, 7, 4, tzinfo=UTC),
        refresh_on_status=(403,),
        secret_refs={"session": "secrets.example.session"},
        folder_template="{author_name}/{gallery_id}-{title}",
        folder_fields={"author_name": "Example Artist"},
    )


def make_child(
    entry_id: str,
    *,
    child_id: str = "child-1",
    package_id: str = "package-1",
) -> DownloadChild:
    return DownloadChild(
        child_id=child_id,
        parent_id="parent-1",
        entry_id=entry_id,
        package=make_package(package_id, gallery_id=f"gallery-{entry_id}"),
    )


def plugin_failure() -> FailureData:
    return FailureData(
        origin=FailureOrigin.PLUGIN,
        stage=FailureStage.RESOLVE,
        code="gallery_unavailable",
        message="Gallery could not be resolved",
        retryable=True,
    )


class StateBatchStoreTests(TestCase):
    def test_batch_store_does_not_expose_entry_deletion(self) -> None:
        """Batch execution graphs can only be deleted as a whole aggregate."""
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entry = make_entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))

            self.assertFalse(hasattr(store, "delete_entry"))
            store.delete_batch("parent-1")
            self.assertEqual(store.list_parent_ids(), ())

    def test_parent_ids_list_newest_first(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            first = make_entry("entry-1")
            second = replace(make_entry("entry-2"), parent_id="parent-2")
            store.create_batch(
                BatchParent("parent-1", (first.entry_id,), title="Artist illustrations"),
                (first,),
            )
            store.create_batch(BatchParent("parent-2", (second.entry_id,)), (second,))

            self.assertEqual(store.list_parent_ids(), ("parent-2", "parent-1"))
            self.assertEqual(
                store.get_snapshot("parent-1").parent.title,
                "Artist illustrations",
            )

    def test_entries_without_children_persist_across_store_instances(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state" / "state.sqlite3"
            first = StateBatchStore(db_path)
            entries = (make_entry("entry-1"), make_entry("entry-2"))
            first.create_batch(
                BatchParent(parent_id="parent-1", entry_ids=("entry-1", "entry-2")),
                entries,
            )

            second = StateBatchStore(db_path)
            snapshot = second.get_snapshot("parent-1")

            self.assertEqual(second.list_entries("parent-1"), entries)
            self.assertEqual(second.list_children("parent-1"), ())
            self.assertEqual(snapshot.execution_status, BatchExecutionStatus.PENDING)
            self.assertEqual(snapshot.counts.unresolved, 2)
            self.assertEqual(snapshot.counts.total, 2)

    def test_resolution_failure_remains_queryable_and_retryable(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entry = make_entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            resolving = replace(entry, status=BatchEntryStatus.RESOLVING)
            failed = replace(
                resolving,
                status=BatchEntryStatus.RESOLUTION_FAILED,
                failure=plugin_failure(),
            )

            store.update_entry(resolving)
            store.update_entry(failed)

            self.assertEqual(store.get_entry(entry.entry_id), failed)
            self.assertIsNone(store.get_entry(entry.entry_id).child_id)
            self.assertEqual(
                store.get_snapshot("parent-1").execution_status, BatchExecutionStatus.FAILED
            )

    def test_parent_status_is_derived_as_cancelled(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entry = make_entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))

            store.update_entry(replace(entry, status=BatchEntryStatus.CANCELLED))

            snapshot = store.get_snapshot("parent-1")
            self.assertEqual(snapshot.execution_status, BatchExecutionStatus.CANCELLED)
            self.assertEqual(snapshot.counts.cancelled, 1)

    def test_attach_child_persists_package_child_assets_and_ready_entry(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            store = StateBatchStore(db_path)
            entry = make_entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = make_child(entry.entry_id)

            store.attach_child(entry.entry_id, child)
            reopened = StateBatchStore(db_path)

            self.assertEqual(reopened.get_child(child.child_id), child)
            self.assertEqual(
                reopened.get_package(child.package.package_id, child.package.version),
                child.package,
            )
            self.assertEqual(
                reopened.list_assets(child.child_id),
                (AssetRecord(asset=child.package.assets[0]),),
            )
            self.assertEqual(reopened.get_entry(entry.entry_id).child_id, child.child_id)
            snapshot = reopened.get_snapshot("parent-1")
            self.assertEqual(snapshot.execution_status, BatchExecutionStatus.RUNNING)
            self.assertEqual(snapshot.counts.queued, 1)

    def test_delete_batch_removes_execution_records_but_keeps_package_versions(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entry = make_entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = make_child(entry.entry_id)
            store.attach_child(entry.entry_id, child)

            store.delete_batch("parent-1")

            self.assertEqual(store.list_parent_ids(), ())
            self.assertIsNone(store.get_entry(entry.entry_id))
            self.assertIsNone(store.get_child(child.child_id))
            self.assertEqual(
                store.get_package(child.package.package_id, child.package.version), child.package
            )

    def test_asset_child_and_parent_progress_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entry = make_entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = make_child(entry.entry_id)
            store.attach_child(entry.entry_id, child)

            downloading_child = replace(child, status=DownloadChildStatus.DOWNLOADING)
            store.update_child(downloading_child)
            asset = store.list_assets(child.child_id)[0]
            downloading_asset = replace(asset, status=AssetStatus.DOWNLOADING, attempt_count=1)
            store.update_asset(child.child_id, downloading_asset)
            completed_asset = replace(
                downloading_asset,
                status=AssetStatus.COMPLETED,
                final_path="gallery/001.jpg",
                content_sha256="a" * 64,
            )
            store.update_asset(child.child_id, completed_asset)
            completed_child = replace(downloading_child, status=DownloadChildStatus.COMPLETED)
            store.update_child(completed_child)

            self.assertEqual(store.get_child(child.child_id), completed_child)
            self.assertEqual(store.list_assets(child.child_id), (completed_asset,))
            snapshot = store.get_snapshot("parent-1")
            self.assertEqual(snapshot.execution_status, BatchExecutionStatus.COMPLETED)
            self.assertEqual(snapshot.counts.completed, 1)

    def test_asset_torrent_checkpoint_round_trip_and_same_status_update(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            store = StateBatchStore(db_path)
            entry = make_entry("entry-1")
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            child = make_child(entry.entry_id)
            store.attach_child(entry.entry_id, child)

            asset = store.list_assets(child.child_id)[0]
            checkpoint = {
                "client": "qbittorrent",
                "client_id": "abc123",
                "submission": "accepted",
                "progress": 0.5,
                "completed_bytes": 512,
                "total_bytes": 1024,
            }
            downloading = replace(
                asset,
                status=AssetStatus.DOWNLOADING,
                metadata={"torrent_checkpoint": checkpoint},
            )
            store.update_asset(child.child_id, downloading)
            progressed = replace(
                downloading,
                metadata={
                    "torrent_checkpoint": {
                        **checkpoint,
                        "progress": 0.75,
                        "completed_bytes": 768,
                    }
                },
            )
            store.update_asset(child.child_id, progressed)

            reopened = StateBatchStore(db_path)
            restored = reopened.list_assets(child.child_id)[0]
            self.assertEqual(restored.status, AssetStatus.DOWNLOADING)
            self.assertEqual(restored.metadata["torrent_checkpoint"]["progress"], 0.75)
            self.assertEqual(restored.metadata["torrent_checkpoint"]["completed_bytes"], 768)

    def test_parent_aggregation_includes_success_failure_and_skip(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entries = tuple(make_entry(f"entry-{index}") for index in range(1, 4))
            store.create_batch(
                BatchParent("parent-1", tuple(entry.entry_id for entry in entries)),
                entries,
            )

            first = replace(entries[0], status=BatchEntryStatus.RESOLVING)
            store.update_entry(first)
            first_child = make_child(first.entry_id)
            store.attach_child(first.entry_id, first_child)
            downloading = replace(first_child, status=DownloadChildStatus.DOWNLOADING)
            store.update_child(downloading)
            store.update_child(replace(downloading, status=DownloadChildStatus.COMPLETED))

            second = replace(entries[1], status=BatchEntryStatus.RESOLVING)
            store.update_entry(second)
            store.update_entry(
                replace(
                    second,
                    status=BatchEntryStatus.RESOLUTION_FAILED,
                    failure=plugin_failure(),
                )
            )
            store.update_entry(replace(entries[2], status=BatchEntryStatus.DUPLICATE_SKIPPED))

            snapshot = store.get_snapshot("parent-1")
            self.assertEqual(snapshot.execution_status, BatchExecutionStatus.PARTIALLY_COMPLETED)
            self.assertEqual(snapshot.counts.completed, 1)
            self.assertEqual(snapshot.counts.rejected, 1)
            self.assertEqual(snapshot.counts.skipped, 1)

    def test_failed_child_handoff_rolls_back_package_and_entry_update(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entries = (make_entry("entry-1"), make_entry("entry-2"))
            store.create_batch(BatchParent("parent-1", ("entry-1", "entry-2")), entries)
            for entry in entries:
                store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            first_child = make_child("entry-1", child_id="shared-child", package_id="package-1")
            store.attach_child("entry-1", first_child)
            conflicting = make_child(
                "entry-2",
                child_id="shared-child",
                package_id="rolled-back-package",
            )

            with self.assertRaises(BatchConflictError):
                store.attach_child("entry-2", conflicting)

            self.assertIsNone(store.get_package("rolled-back-package", 1))
            self.assertEqual(
                store.get_entry("entry-2").status,
                BatchEntryStatus.RESOLVING,
            )
            self.assertEqual(store.list_children("parent-1"), (first_child,))

    def test_attempt_history_round_trips_in_number_order(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            store = StateBatchStore(db_path)
            plan = RetryPlan(
                plan_id="retry-1",
                start_at=FailureStage.RESOLVE,
                scope=RetryScope.ENTRY,
                target_ids=("entry-1",),
                reason="Retry failed resolution",
            )
            first = AttemptData(
                attempt_id="attempt-1",
                target_id="entry-1",
                number=1,
                checkpoints=(
                    CheckpointData(
                        checkpoint=Checkpoint.SELECTED,
                        target_id="entry-1",
                    ),
                ),
                failure=plugin_failure(),
            )
            second = AttemptData(
                attempt_id="attempt-2",
                target_id="entry-1",
                number=2,
                retry_plan=plan,
            )

            store.append_attempt(first)
            store.append_attempt(second)

            reopened = StateBatchStore(db_path)
            self.assertEqual(reopened.list_attempts("entry-1"), (first, second))


if __name__ == "__main__":
    import unittest

    unittest.main()
