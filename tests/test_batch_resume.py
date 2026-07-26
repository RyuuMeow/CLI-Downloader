from tests.state_store_support import StateBatchStore, StateTaskStore
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.batch_resume import BatchResumeService
from cli_downloader.cli.app import main
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
    RetryScope,
)
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.task_store import TaskRecord, TaskStatus
from cli_downloader.core.task_tree import TaskKind
from tests.example_site import build_example_runtime


def _entry(item_id: str, parent_id: str = "parent-1") -> BatchEntry:
    return BatchEntry(
        entry_id=item_id,
        parent_id=parent_id,
        plugin_id="example",
        site_id="example",
        candidate_id=item_id,
        source_operation_id="resolve_gallery",
        source_url=f"https://example.test/gallery/{item_id}",
    )


def _failure(stage: FailureStage = FailureStage.RESOLVE) -> FailureData:
    return FailureData(
        origin=FailureOrigin.PLUGIN,
        stage=stage,
        code="test_failure",
        message="Test failure",
        retryable=True,
    )


def _package(item: BatchEntry) -> DownloadPackage:
    return DownloadPackage(
        package_id=f"package-{item.entry_id}",
        version=1,
        plugin_id=item.plugin_id,
        site_id=item.site_id,
        gallery_id=item.candidate_id,
        source_operation_id=item.source_operation_id,
        source_url=item.source_url,
        strategy=DownloadStrategy.DIRECT,
        assets=tuple(
            DownloadAsset(
                site_id=item.site_id,
                gallery_id=item.candidate_id,
                plugin_asset_key=f"{item.entry_id}-{i}",
                url=f"https://cdn.example.test/{item.entry_id}/{i}",
                filename=f"asset-{i}.jpg",
                index=i,
            )
            for i in range(1, 3)
        ),
    )


class ResumePlanTests(TestCase):
    """Unit tests for BatchResumeService.plan_resume."""

    def test_plan_resume_includes_pending_resolution_entries(self) -> None:
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            e1 = _entry("e1")
            store.create_batch(BatchParent("parent-1", (e1.entry_id,)), (e1,))

            service = BatchResumeService(store=store, resolver=object())
            plan = service.plan_resume("parent-1")

            entry_ids = {
                tid for p in plan.plans if p.scope is RetryScope.ENTRY for tid in p.target_ids
            }
            self.assertIn(e1.entry_id, entry_ids)

    def test_plan_resume_includes_resolving_entries(self) -> None:
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            e1 = _entry("e1")
            store.create_batch(BatchParent("parent-1", (e1.entry_id,)), (e1,))
            store.update_entry(replace(e1, status=BatchEntryStatus.RESOLVING))

            service = BatchResumeService(store=store, resolver=object())
            plan = service.plan_resume("parent-1")

            entry_ids = {
                tid for p in plan.plans if p.scope is RetryScope.ENTRY for tid in p.target_ids
            }
            self.assertIn(e1.entry_id, entry_ids)

    def test_plan_resume_excludes_failed_states(self) -> None:
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            e1 = _entry("e1")
            store.create_batch(BatchParent("parent-1", (e1.entry_id,)), (e1,))
            store.update_entry(replace(e1, status=BatchEntryStatus.RESOLVING))
            store.update_entry(
                replace(e1, status=BatchEntryStatus.RESOLUTION_FAILED, failure=_failure())
            )

            service = BatchResumeService(store=store, resolver=object())
            plan = service.plan_resume("parent-1")

            entry_ids = {
                tid for p in plan.plans if p.scope is RetryScope.ENTRY for tid in p.target_ids
            }
            self.assertNotIn(e1.entry_id, entry_ids)

            # Also test CANCELLED and PACKAGE_REJECTED
            e2 = _entry("e2", parent_id="parent-2")
            store.create_batch(BatchParent("parent-2", (e2.entry_id,)), (e2,))
            # Transition: PENDING → ... → RESOLVING → cancelled
            store.update_entry(replace(e2, status=BatchEntryStatus.RESOLVING))
            store.update_entry(replace(e2, status=BatchEntryStatus.CANCELLED))
            plan2 = service.plan_resume("parent-2")
            entry_ids2 = {
                tid for p in plan2.plans if p.scope is RetryScope.ENTRY for tid in p.target_ids
            }
            self.assertNotIn(e2.entry_id, entry_ids2)

    def test_plan_resume_includes_queued_and_downloading_children(self) -> None:
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            item = _entry("item")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild(
                child_id="child-1",
                parent_id="parent-1",
                entry_id=item.entry_id,
                package=_package(item),
            )
            store.attach_child(item.entry_id, child)
            # Child is already QUEUED after attach

            service = BatchResumeService(store=store, resolver=object())
            plan = service.plan_resume("parent-1")

            child_ids = {
                tid for p in plan.plans if p.scope is RetryScope.CHILD for tid in p.target_ids
            }
            self.assertIn(child.child_id, child_ids)

            # Transition to DOWNLOADING — should still be included
            store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            plan2 = service.plan_resume("parent-1")
            child_ids2 = {
                tid for p in plan2.plans if p.scope is RetryScope.CHILD for tid in p.target_ids
            }
            self.assertIn(child.child_id, child_ids2)

    def test_plan_resume_excludes_completed_children(self) -> None:
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            item = _entry("item")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild(
                child_id="child-1",
                parent_id="parent-1",
                entry_id=item.entry_id,
                package=_package(item),
            )
            store.attach_child(item.entry_id, child)
            store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            store.update_child(replace(child, status=DownloadChildStatus.COMPLETED))

            service = BatchResumeService(store=store, resolver=object())
            plan = service.plan_resume("parent-1")

            child_ids = {tid for p in plan.plans for tid in p.target_ids}
            self.assertNotIn(child.child_id, child_ids)

    def test_plan_resume_excludes_failed_children(self) -> None:
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            item = _entry("item")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild(
                child_id="child-1",
                parent_id="parent-1",
                entry_id=item.entry_id,
                package=_package(item),
            )
            store.attach_child(item.entry_id, child)
            store.update_child(
                replace(
                    child,
                    status=DownloadChildStatus.FAILED,
                    failure=FailureData(
                        origin=FailureOrigin.SYSTEM,
                        stage=FailureStage.FETCH,
                        code="test_failure",
                        message="Test failure",
                        retryable=True,
                    ),
                )
            )

            service = BatchResumeService(store=store, resolver=object())
            plan = service.plan_resume("parent-1")

            child_ids = {
                tid for p in plan.plans if p.scope is RetryScope.CHILD for tid in p.target_ids
            }
            self.assertNotIn(child.child_id, child_ids)


class FakeResolver:
    """Returns a fresh package for every entry."""

    async def resolve(self, entry, cancel_event, progress):
        return _package(entry)


class CompletingDownloader:
    async def execute(self, child, assets, cancel_event, progress):
        return tuple(
            replace(
                record,
                status=AssetStatus.COMPLETED,
                final_path=f"output/{record.asset.filename}",
                content_sha256="c" * 64,
            )
            for record in assets
        )


class ResumeRunTests(TestCase):
    """Integration tests for BatchResumeService.resume_parent."""

    def test_resume_parent_completes_interrupted_batch(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = _entry("item")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            # Simulate interrupted state: entry is RESOLVING with no child yet
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))

            resolver = FakeResolver()
            pipeline = BatchPipeline(
                store=store,
                resolver=resolver,
                downloader=CompletingDownloader(),
            )

            result = await BatchResumeService(store=store, resolver=resolver).resume_parent(
                "parent-1", pipeline=pipeline
            )

            self.assertEqual(result.run.snapshot.counts.completed, 1)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_resume_parent_continues_downloading_child(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = _entry("item")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            # Simulate interrupted download: entry resolved, child QUEUED
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))
            pkg = _package(item)
            child = DownloadChild(
                child_id="child-item",
                parent_id="parent-1",
                entry_id=item.entry_id,
                package=pkg,
            )
            store.attach_child(item.entry_id, child)
            # Child is QUEUED (as left by a fresh attachment or a prepared resume).

            resolver = FakeResolver()
            pipeline = BatchPipeline(
                store=store,
                resolver=resolver,
                downloader=CompletingDownloader(),
            )

            service = BatchResumeService(store=store, resolver=resolver)
            plan = service.plan_resume("parent-1")
            child_ids = {
                tid for p in plan.plans if p.scope is RetryScope.CHILD for tid in p.target_ids
            }
            self.assertIn(child.child_id, child_ids)

            result = await service.resume_parent("parent-1", pipeline=pipeline)
            self.assertEqual(result.run.snapshot.counts.completed, 1)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_resume_with_no_resumable_work_raises(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = _entry("item")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            # All entries resolved, all children completed
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))
            child = DownloadChild(
                child_id="child-item",
                parent_id="parent-1",
                entry_id=item.entry_id,
                package=_package(item),
            )
            store.attach_child(item.entry_id, child)
            store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            store.update_child(replace(child, status=DownloadChildStatus.COMPLETED))

            resolver = FakeResolver()
            pipeline = BatchPipeline(
                store=store,
                resolver=resolver,
                downloader=CompletingDownloader(),
            )

            with self.assertRaises(ValueError):
                await BatchResumeService(store=store, resolver=resolver).resume_parent(
                    "parent-1", pipeline=pipeline
                )

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_cli_resume_recovers_queued_child_without_creating_retry_attempt(self) -> None:
        stream = StringIO()
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            database = StateDatabase(state / "state.sqlite3")
            StateTaskStore(database).upsert(
                TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH)
            )
            store = StateBatchStore(database)
            item = _entry("cli")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))
            store.attach_child(
                item.entry_id,
                DownloadChild(
                    child_id="child-cli",
                    parent_id="parent-1",
                    entry_id=item.entry_id,
                    package=_package(item),
                ),
            )

            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with redirect_stdout(stream):
                    exit_code = main(
                        ["batches", "resume", "parent-1", "--json"],
                        runtime=build_example_runtime(),
                    )

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["resume"]["parent_id"], "parent-1")
        self.assertEqual(payload["batch"]["counts"]["completed"], 1)


if __name__ == "__main__":
    TestCase.main()
