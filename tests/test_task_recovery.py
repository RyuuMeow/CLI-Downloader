import asyncio

from tests.state_store_support import StateBatchStore, StateTaskStore
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4

from cli_downloader.cli.app import _recover_interrupted_downloads
from cli_downloader.application.task_execution_coordinator import TaskExecutionCoordinator
from cli_downloader.core.batch import (
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    DownloadAsset,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
)
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.task_store import (
    TaskDesiredControl,
    TaskRecord,
    TaskStatus,
)


def _asset(asset_id: str) -> DownloadAsset:
    return DownloadAsset(
        site_id="example",
        gallery_id="example-gallery",
        plugin_asset_key=asset_id,
        url=f"https://example.test/asset/{asset_id}",
        filename=f"{asset_id}.jpg",
        index=1,
        metadata={"asset_kind": "image"},
    )


def _package(asset: DownloadAsset) -> DownloadPackage:
    return DownloadPackage(
        package_id=uuid4().hex,
        version=1,
        plugin_id="example",
        site_id="example",
        gallery_id="example-gallery",
        source_operation_id="batch_download",
        source_url="https://example.test/listing/test",
        strategy=DownloadStrategy.DIRECT,
        assets=(asset,),
        title="Test Gallery",
        folder_template="{site_id}/{gallery_id}",
        folder_fields={"site_id": "example", "gallery_id": "example-gallery"},
    )


# ------------------------------------------------------------------
# Task-level recovery
# ------------------------------------------------------------------


class TaskRecoveryTest(TestCase):
    def test_running_and_planned_tasks_are_requeued(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateTaskStore(Path(directory) / "state.sqlite3")
            store.upsert(
                TaskRecord(
                    task_id="running",
                    status=TaskStatus.RUNNING,
                    source_url="https://example.test/gallery/running",
                    metadata={"scheduler_managed": True},
                )
            )
            store.upsert(
                TaskRecord(
                    task_id="planned",
                    status=TaskStatus.PLANNED,
                    source_url="https://example.test/gallery/planned",
                    metadata={"scheduler_managed": True},
                )
            )
            store.upsert(
                TaskRecord(
                    task_id="plan-only",
                    status=TaskStatus.PLANNED,
                    source_url="https://example.test/gallery/plan-only",
                )
            )

            commands = _recover_interrupted_downloads(store)

            self.assertEqual(store.get("running").status, TaskStatus.PLANNED)
            self.assertEqual(commands[0][2], "running")
            self.assertEqual(commands[1][2], "planned")
            self.assertEqual(len(commands), 2)

    def test_paused_task_stays_paused_after_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            store = StateTaskStore(base / "state.sqlite3")

            store.upsert(
                TaskRecord(
                    task_id="paused-task",
                    status=TaskStatus.PAUSED,
                    source_url="https://example.test/gallery/paused",
                    metadata={"scheduler_managed": True},
                )
            )

            commands = _recover_interrupted_downloads(store)

            self.assertEqual(store.get("paused-task").status, TaskStatus.PAUSED)
            self.assertEqual(len(commands), 0)

    def test_paused_task_releases_stale_run_lease_after_restart(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            store = StateTaskStore(base / "state.sqlite3")

            store.upsert(
                TaskRecord(
                    task_id="paused-task",
                    status=TaskStatus.PAUSED,
                    desired_control=TaskDesiredControl.PAUSE,
                    active_run_id="stopped-process-run",
                    run_generation=2,
                    source_url="https://example.test/gallery/paused",
                    metadata={"scheduler_managed": True},
                )
            )

            commands = _recover_interrupted_downloads(store)

            recovered = store.get("paused-task")
            self.assertEqual(recovered.status, TaskStatus.PAUSED)
            self.assertEqual(recovered.desired_control, TaskDesiredControl.PAUSE)
            self.assertIsNone(recovered.active_run_id)
            self.assertEqual(recovered.run_generation, 2)
            self.assertEqual(commands, ())

    def test_task_resumed_before_crash_is_requeued(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            store = StateTaskStore(base / "state.sqlite3")

            store.upsert(
                TaskRecord(
                    task_id="resumed-task",
                    status=TaskStatus.RUNNING,
                    source_url="https://example.test/gallery/resumed",
                    metadata={"scheduler_managed": True},
                )
            )

            commands = _recover_interrupted_downloads(store)

            self.assertEqual(store.get("resumed-task").status, TaskStatus.PLANNED)
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][2], "resumed-task")

    def test_running_task_recovery_clears_stale_run_lease(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateTaskStore(Path(directory) / "state.sqlite3")
            store.upsert(
                TaskRecord(
                    task_id="stale-running",
                    status=TaskStatus.RUNNING,
                    desired_control=TaskDesiredControl.RUN,
                    active_run_id="old-owner-run",
                    run_generation=7,
                    source_url="https://example.test/gallery/stale-running",
                    metadata={"scheduler_managed": True},
                )
            )

            commands = _recover_interrupted_downloads(store)

            recovered = store.get("stale-running")
            self.assertEqual(recovered.status, TaskStatus.PLANNED)
            self.assertEqual(recovered.desired_control, TaskDesiredControl.RUN)
            self.assertIsNone(recovered.active_run_id)
            self.assertEqual(recovered.run_generation, 7)
            self.assertEqual(commands[0][2], "stale-running")

    def test_recovery_rotates_execution_fence_for_old_owner(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateTaskStore(Path(directory) / "state.sqlite3")
            store.upsert(
                TaskRecord(
                    task_id="fenced-running",
                    status=TaskStatus.RUNNING,
                    active_run_id="old-owner-run",
                    source_url="https://example.test/gallery/fenced-running",
                    metadata={
                        "scheduler_managed": True,
                        "execution_fence": "old-owner",
                    },
                )
            )

            _recover_interrupted_downloads(store)

            recovered = store.get("fenced-running")
            self.assertNotEqual(
                recovered.metadata.get("execution_fence"),
                "old-owner",
            )

    def test_non_managed_and_stable_tasks_ignored(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateTaskStore(Path(directory) / "state.sqlite3")
            store.upsert(
                TaskRecord(
                    task_id="manual",
                    status=TaskStatus.RUNNING,
                    source_url="https://example.test/gallery/manual",
                )
            )
            for status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELED,
                TaskStatus.PAUSED,
            ):
                store.upsert(
                    TaskRecord(
                        task_id=status.value,
                        status=status,
                        source_url="https://example.test/gallery/x",
                        metadata={"scheduler_managed": True},
                    )
                )

            commands = _recover_interrupted_downloads(store)
            self.assertEqual(len(commands), 0)


# ------------------------------------------------------------------
# Batch-level recovery
# ------------------------------------------------------------------


class BatchRecoveryTest(TestCase):
    """Tests for _recover_interrupted_downloads batch path.

    create_batch auto-computes parent status from entries. All batches
    start as PENDING. The recovery function handles both PENDING and
    RUNNING parents.
    """

    def _create_batch(
        self,
        batch_store: StateBatchStore,
        parent_id: str,
        entry_id: str,
        *,
        parent_title: str = "Test",
        entry_status: BatchEntryStatus = BatchEntryStatus.PENDING_RESOLUTION,
    ) -> None:
        parent = BatchParent(
            parent_id=parent_id,
            entry_ids=(entry_id,),
            title=parent_title,
        )
        entry = BatchEntry(
            entry_id=entry_id,
            parent_id=parent_id,
            plugin_id="example",
            site_id="example",
            candidate_id=f"candidate-{parent_id}",
            source_operation_id="batch_download",
            source_url="https://example.test/listing/test",
            status=entry_status,
        )
        batch_store.create_batch(parent, (entry,))

    def test_pending_batch_generates_resume_command(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            batch_store = StateBatchStore(base / "state.sqlite3")
            self._create_batch(batch_store, "batch-1", "entry-1")

            commands = _recover_interrupted_downloads(
                StateTaskStore(base / "state.sqlite3"),
                batch_store=batch_store,
            )

            self.assertIn(("batches", "resume", "batch-1"), commands)

    def test_paused_batch_skips_retry(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            batch_store = StateBatchStore(base / "state.sqlite3")
            task_store = StateTaskStore(base / "state.sqlite3")

            self._create_batch(batch_store, "paused-batch", "entry-p")
            # Pause state is stored in Task DB (authoritative source).
            task_store.upsert(
                TaskRecord(
                    task_id="paused-batch",
                    status=TaskStatus.PAUSED,
                    source_url="https://example.test/listing/test",
                    metadata={"scheduler_managed": True},
                )
            )

            commands = _recover_interrupted_downloads(
                task_store,
                batch_store=batch_store,
            )

            self.assertNotIn(("batches", "retry", "paused-batch"), commands)
            self.assertEqual(len(commands), 0)

    def test_paused_batch_releases_stale_lease_and_can_be_resumed(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            batch_store = StateBatchStore(base / "state.sqlite3")
            task_store = StateTaskStore(base / "state.sqlite3")

            self._create_batch(batch_store, "paused-batch", "entry-p")
            task_store.upsert(
                TaskRecord(
                    task_id="paused-batch",
                    status=TaskStatus.PAUSED,
                    desired_control=TaskDesiredControl.PAUSE,
                    active_run_id="stopped-process-run",
                    run_generation=3,
                    kind=TaskKind.BATCH,
                )
            )

            commands = _recover_interrupted_downloads(
                task_store,
                batch_store=batch_store,
            )

            recovered = task_store.get("paused-batch")
            self.assertEqual(recovered.status, TaskStatus.PAUSED)
            self.assertEqual(recovered.desired_control, TaskDesiredControl.PAUSE)
            self.assertIsNone(recovered.active_run_id)
            self.assertEqual(recovered.run_generation, 3)
            self.assertEqual(commands, ())

            async def resume_and_claim() -> None:
                coordinator = TaskExecutionCoordinator(task_store=task_store)
                resumed = await coordinator.request_resume("paused-batch")
                self.assertEqual(resumed.status, TaskStatus.PLANNED)
                handle = await coordinator.claim("paused-batch")
                self.assertEqual(handle.run_generation, 4)

            asyncio.run(resume_and_claim())

    def test_batch_resumed_before_crash_is_requeued(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            batch_store = StateBatchStore(base / "state.sqlite3")
            task_store = StateTaskStore(base / "state.sqlite3")

            self._create_batch(batch_store, "resumed-batch", "entry-r")
            task_store.upsert(
                TaskRecord(
                    task_id="resumed-batch",
                    status=TaskStatus.RUNNING,
                    desired_control=TaskDesiredControl.RUN,
                    active_run_id="crashed-run",
                    run_generation=4,
                    kind=TaskKind.BATCH,
                )
            )

            commands = _recover_interrupted_downloads(
                task_store,
                batch_store=batch_store,
            )

            self.assertIn(("batches", "resume", "resumed-batch"), commands)
            recovered = task_store.get("resumed-batch")
            self.assertEqual(recovered.status, TaskStatus.PLANNED)
            self.assertEqual(recovered.desired_control, TaskDesiredControl.RUN)
            self.assertIsNone(recovered.active_run_id)
            self.assertEqual(recovered.run_generation, 4)

    def test_pending_resolution_entries_remain_resumable(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            batch_store = StateBatchStore(base / "state.sqlite3")

            parent = BatchParent(
                parent_id="batch-resolving",
                entry_ids=("entry-resolving", "entry-pending"),
                title="Test",
            )
            e1 = BatchEntry(
                entry_id="entry-resolving",
                parent_id="batch-resolving",
                plugin_id="example",
                site_id="example",
                candidate_id="c1",
                source_operation_id="batch_download",
                source_url="https://example.test/listing/test",
                status=BatchEntryStatus.RESOLVING,
            )
            e2 = BatchEntry(
                entry_id="entry-pending",
                parent_id="batch-resolving",
                plugin_id="example",
                site_id="example",
                candidate_id="c2",
                source_operation_id="batch_download",
                source_url="https://example.test/listing/test",
                status=BatchEntryStatus.PENDING_RESOLUTION,
            )
            batch_store.create_batch(parent, (e1, e2))

            _recover_interrupted_downloads(
                StateTaskStore(base / "state.sqlite3"),
                batch_store=batch_store,
            )

            self.assertEqual(
                batch_store.get_entry("entry-pending").status,
                BatchEntryStatus.PENDING_RESOLUTION,
            )
            self.assertEqual(
                batch_store.get_entry("entry-resolving").status,
                BatchEntryStatus.RESOLVING,
            )

    def test_downloading_assets_return_to_queue(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            batch_store = StateBatchStore(base / "state.sqlite3")

            parent = BatchParent(
                parent_id="batch-assets",
                entry_ids=("entry-a",),
                title="Asset Test",
            )
            entry = BatchEntry(
                entry_id="entry-a",
                parent_id="batch-assets",
                plugin_id="example",
                site_id="example",
                candidate_id="c1",
                source_operation_id="batch_download",
                source_url="https://example.test/listing/test",
                status=BatchEntryStatus.RESOLVING,
            )
            batch_store.create_batch(parent, (entry,))

            a1 = _asset("asset-1")
            a2 = DownloadAsset(
                site_id="example",
                gallery_id="example-gallery",
                plugin_asset_key="asset-2",
                url="https://example.test/asset/asset-2",
                filename="asset-2.jpg",
                index=2,
                metadata={"asset_kind": "image"},
            )
            child = DownloadChild(
                child_id="child-a",
                parent_id="batch-assets",
                entry_id="entry-a",
                package=DownloadPackage(
                    package_id=uuid4().hex,
                    version=1,
                    plugin_id="example",
                    site_id="example",
                    gallery_id="example-gallery",
                    source_operation_id="batch_download",
                    source_url="https://example.test/listing/test",
                    strategy=DownloadStrategy.DIRECT,
                    assets=(a1, a2),
                    title="Test",
                    folder_template="{site_id}/{gallery_id}",
                    folder_fields={"site_id": "example", "gallery_id": "example-gallery"},
                ),
            )
            batch_store.attach_child(entry.entry_id, child)
            batch_store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            for asset_record in batch_store.list_assets("child-a"):
                if asset_record.asset.asset_id == a1.asset_id:
                    batch_store.update_asset(
                        "child-a",
                        replace(asset_record, status=AssetStatus.DOWNLOADING),
                    )

            _recover_interrupted_downloads(
                StateTaskStore(base / "state.sqlite3"),
                batch_store=batch_store,
            )

            self.assertEqual(batch_store.get_child("child-a").status, DownloadChildStatus.QUEUED)
            for record in batch_store.list_assets("child-a"):
                self.assertEqual(record.status, AssetStatus.QUEUED)

    def test_completed_assets_remain_untouched(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            batch_store = StateBatchStore(base / "state.sqlite3")

            parent = BatchParent(
                parent_id="batch-done",
                entry_ids=("entry-done",),
                title="Done Test",
            )
            entry = BatchEntry(
                entry_id="entry-done",
                parent_id="batch-done",
                plugin_id="example",
                site_id="example",
                candidate_id="c1",
                source_operation_id="batch_download",
                source_url="https://example.test/listing/test",
                status=BatchEntryStatus.PENDING_RESOLUTION,
            )
            batch_store.create_batch(parent, (entry,))

            batch_store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))

            a1 = _asset("asset-done")
            child = DownloadChild(
                child_id="child-done",
                parent_id="batch-done",
                entry_id="entry-done",
                package=DownloadPackage(
                    package_id=uuid4().hex,
                    version=1,
                    plugin_id="example",
                    site_id="example",
                    gallery_id="example-gallery",
                    source_operation_id="batch_download",
                    source_url="https://example.test/listing/test",
                    strategy=DownloadStrategy.DIRECT,
                    assets=(a1,),
                    title="Test",
                    folder_template="{site_id}/{gallery_id}",
                    folder_fields={"site_id": "example", "gallery_id": "example-gallery"},
                ),
            )
            batch_store.attach_child(entry.entry_id, child)
            batch_store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            for asset_record in batch_store.list_assets("child-done"):
                downloading = replace(asset_record, status=AssetStatus.DOWNLOADING)
                batch_store.update_asset("child-done", downloading)
                batch_store.update_asset(
                    "child-done",
                    replace(downloading, status=AssetStatus.COMPLETED, final_path="/done/file.jpg"),
                )

            _recover_interrupted_downloads(
                StateTaskStore(base / "state.sqlite3"),
                batch_store=batch_store,
            )

            for record in batch_store.list_assets("child-done"):
                self.assertEqual(record.status, AssetStatus.COMPLETED)

    def test_combined_task_and_batch_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            task_store = StateTaskStore(base / "state.sqlite3")
            batch_store = StateBatchStore(base / "state.sqlite3")

            task_store.upsert(
                TaskRecord(
                    task_id="task-x",
                    status=TaskStatus.RUNNING,
                    source_url="https://example.test/gallery/x",
                    metadata={"scheduler_managed": True},
                )
            )
            self._create_batch(batch_store, "batch-x", "entry-x")

            commands = _recover_interrupted_downloads(
                task_store,
                batch_store=batch_store,
            )

            self.assertEqual(len(commands), 2)
            task_cmds = [c for c in commands if c[0] == "tasks"]
            batch_cmds = [c for c in commands if c[0] == "batches"]
            self.assertEqual(len(task_cmds), 1)
            self.assertEqual(len(batch_cmds), 1)
            self.assertEqual(batch_cmds[0][1], "resume")

    def test_recovery_without_batch_store(self) -> None:
        """Without batch_store, only task recovery runs."""
        with TemporaryDirectory() as directory:
            store = StateTaskStore(Path(directory) / "state.sqlite3")
            store.upsert(
                TaskRecord(
                    task_id="t1",
                    status=TaskStatus.PLANNED,
                    source_url="https://example.test/gallery/t1",
                    metadata={"scheduler_managed": True},
                )
            )

            commands = _recover_interrupted_downloads(store, batch_store=None)
            self.assertEqual(len(commands), 1)
