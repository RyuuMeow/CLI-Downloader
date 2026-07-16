from tests.state_store_support import StateBatchStore, StateTaskStore
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.application.batch_execution_runner import CoordinatedBatchRunner
from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.task_execution_coordinator import TaskExecutionCoordinator
from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    DownloadAsset,
    DownloadPackage,
)
from cli_downloader.core.errors import TaskExecutionConflictError
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.task_store import TaskRecord, TaskStatus


ASYNC_TEST_TIMEOUT = 10


def _entry() -> BatchEntry:
    return BatchEntry(
        entry_id="entry-1",
        parent_id="parent-1",
        plugin_id="example",
        site_id="example",
        candidate_id="candidate-1",
        source_operation_id="search",
        source_url="https://example.test/gallery/1",
    )


def _package(entry: BatchEntry) -> DownloadPackage:
    return DownloadPackage(
        package_id="package-1",
        version=1,
        plugin_id=entry.plugin_id,
        site_id=entry.site_id,
        gallery_id="gallery-1",
        source_operation_id="resolve",
        source_url=entry.source_url,
        strategy=DownloadStrategy.DIRECT,
        assets=(
            DownloadAsset(
                site_id=entry.site_id,
                gallery_id="gallery-1",
                plugin_asset_key="page-1",
                url="https://example.test/1.jpg",
                filename="001.jpg",
                index=0,
            ),
        ),
    )


class BlockingResolver:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def resolve(self, entry, cancel_event, progress):
        self.started.set()
        await self.release.wait()
        return _package(entry)


class PauseBoundaryResolver:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release_boundary = asyncio.Event()
        self.resumed = asyncio.Event()

    async def resolve(self, entry, cancel_event, progress):
        self.started.set()
        await self.release_boundary.wait()
        await cancel_event.wait_if_paused()
        self.resumed.set()
        return _package(entry)


class CancelBoundaryResolver:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release_boundary = asyncio.Event()

    async def resolve(self, entry, cancel_event, progress):
        self.started.set()
        await self.release_boundary.wait()
        await cancel_event.wait()
        raise asyncio.CancelledError


class RevisionRaceCoordinator(TaskExecutionCoordinator):
    """Inject one control CAS between runner terminal attempts."""

    def __init__(self, *, task_store) -> None:
        super().__init__(task_store=task_store)
        self.injected = False

    async def finish(self, handle, *, status, expected_revision):
        if not self.injected:
            self.injected = True
            await self.request_pause(handle.task_id, expected_revision=handle.revision)
        return await super().finish(
            handle,
            status=status,
            expected_revision=expected_revision,
        )


class ImmediateDownloader:
    async def execute(self, child, assets, cancel_event, progress):
        return tuple(
            AssetRecord(
                asset=asset.asset,
                status=AssetStatus.COMPLETED,
                final_path=f"output/{asset.asset.filename}",
                content_sha256="a" * 64,
            )
            for asset in assets
        )


class CoordinatedBatchRunnerTests(TestCase):
    def test_owner_shutdown_checkpoints_batch_without_cancellation(self) -> None:
        async def scenario(directory: str) -> None:
            root = Path(directory)
            batches = StateBatchStore(root / "state.sqlite3")
            tasks = StateTaskStore(root / "state.sqlite3")
            entry = _entry()
            batches.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            tasks.upsert(TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH))
            resolver = BlockingResolver()
            pipeline = BatchPipeline(
                store=batches,
                resolver=resolver,
                downloader=ImmediateDownloader(),
            )
            runner = CoordinatedBatchRunner(
                pipeline=pipeline,
                coordinator=TaskExecutionCoordinator(task_store=tasks),
            )

            running = asyncio.create_task(runner.run_batch("parent-1"))
            await asyncio.wait_for(resolver.started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            self.assertTrue(pipeline.shutdown("parent-1"))
            resolver.release.set()
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertTrue(result.stopped)
            self.assertEqual(result.task_status, TaskStatus.PLANNED)
            recovered = tasks.get("parent-1")
            self.assertEqual(recovered.status, TaskStatus.PLANNED)
            self.assertTrue(recovered.metadata["owner_shutdown_checkpoint"])
            self.assertIsNone(recovered.active_run_id)
            self.assertEqual(batches.get_entry("entry-1").status, BatchEntryStatus.RESOLVING)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_cancel_stays_pending_until_worker_terminates(self) -> None:
        async def scenario(directory: str) -> None:
            root = Path(directory)
            batches = StateBatchStore(root / "state.sqlite3")
            tasks = StateTaskStore(root / "state.sqlite3")
            entry = _entry()
            batches.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            tasks.upsert(TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH))
            resolver = CancelBoundaryResolver()
            pipeline = BatchPipeline(
                store=batches,
                resolver=resolver,
                downloader=ImmediateDownloader(),
            )
            runner = CoordinatedBatchRunner(
                pipeline=pipeline,
                coordinator=TaskExecutionCoordinator(task_store=tasks),
            )

            running = asyncio.create_task(runner.run_batch("parent-1"))
            await asyncio.wait_for(resolver.started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            await runner.request_cancel("parent-1")
            self.assertTrue(pipeline.cancel("parent-1"))
            self.assertEqual(tasks.get("parent-1").status, TaskStatus.RUNNING)

            resolver.release_boundary.set()
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertEqual(result.snapshot.execution_status.value, "cancelled")
            self.assertEqual(result.task_status, TaskStatus.CANCELED)
            self.assertEqual(tasks.get("parent-1").status, TaskStatus.CANCELED)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_recovery_run_records_one_generation_and_rejects_duplicate(self) -> None:
        async def scenario(directory: str) -> None:
            root = Path(directory)
            batches = StateBatchStore(root / "state.sqlite3")
            tasks = StateTaskStore(root / "state.sqlite3")
            entry = _entry()
            batches.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            tasks.upsert(
                TaskRecord(
                    task_id="parent-1",
                    status=TaskStatus.PAUSED,
                    kind=TaskKind.BATCH,
                )
            )
            coordinator = TaskExecutionCoordinator(task_store=tasks)
            resolver = BlockingResolver()
            runner = CoordinatedBatchRunner(
                pipeline=BatchPipeline(
                    store=batches,
                    resolver=resolver,
                    downloader=ImmediateDownloader(),
                ),
                coordinator=coordinator,
            )

            await runner.request_resume("parent-1")
            running = asyncio.create_task(runner.run_recovery_batch("parent-1"))
            duplicate = asyncio.create_task(runner.run_recovery_batch("parent-1"))
            await asyncio.wait_for(resolver.started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            resolver.release.set()
            results = await asyncio.wait_for(
                asyncio.gather(running, duplicate, return_exceptions=True),
                timeout=1,
            )

            record = tasks.get("parent-1")
            self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
            self.assertEqual(
                sum(isinstance(result, TaskExecutionConflictError) for result in results), 1
            )
            self.assertEqual(record.metadata["last_run_reason"], "recovery")
            self.assertEqual(record.run_generation, 1)
            self.assertIsNone(record.active_run_id)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_parent_pause_acknowledges_after_worker_boundary_and_resume_restarts_task(self) -> None:
        async def scenario(directory: str) -> None:
            root = Path(directory)
            batches = StateBatchStore(root / "state.sqlite3")
            tasks = StateTaskStore(root / "state.sqlite3")
            entry = _entry()
            batches.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            tasks.upsert(
                TaskRecord(
                    task_id="parent-1",
                    status=TaskStatus.PENDING,
                    kind=TaskKind.BATCH,
                )
            )
            resolver = PauseBoundaryResolver()
            pipeline = BatchPipeline(
                store=batches,
                resolver=resolver,
                downloader=ImmediateDownloader(),
            )
            coordinator = TaskExecutionCoordinator(task_store=tasks)
            runner = CoordinatedBatchRunner(pipeline=pipeline, coordinator=coordinator)

            running = asyncio.create_task(runner.run_batch("parent-1"))
            await asyncio.wait_for(resolver.started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            await coordinator.request_pause("parent-1")
            self.assertTrue(pipeline.pause("parent-1"))
            resolver.release_boundary.set()

            for _ in range(20):
                if tasks.get("parent-1").status is TaskStatus.PAUSED:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(tasks.get("parent-1").status, TaskStatus.PAUSED)
            self.assertFalse(resolver.resumed.is_set())

            await coordinator.request_resume("parent-1")
            self.assertTrue(pipeline.resume("parent-1"))
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertTrue(resolver.resumed.is_set())
            self.assertEqual(result.snapshot.execution_status.value, "completed")
            self.assertEqual(result.task_status, TaskStatus.COMPLETED)
            self.assertEqual(tasks.get("parent-1").status, TaskStatus.COMPLETED)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_shared_task_store_rejects_second_pipeline_and_tags_events_with_run_id(self) -> None:
        async def scenario(directory: str) -> None:
            root = Path(directory)
            batches = StateBatchStore(root / "state.sqlite3")
            tasks = StateTaskStore(root / "state.sqlite3")
            entry = _entry()
            batches.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            tasks.upsert(
                TaskRecord(
                    task_id="parent-1",
                    status=TaskStatus.PENDING,
                    kind=TaskKind.BATCH,
                )
            )
            resolver = BlockingResolver()
            first_events = []
            first = CoordinatedBatchRunner(
                pipeline=BatchPipeline(
                    store=batches,
                    resolver=resolver,
                    downloader=ImmediateDownloader(),
                    event_sink=first_events.append,
                ),
                coordinator=TaskExecutionCoordinator(
                    task_store=tasks,
                    run_id_factory=lambda: "run-1",
                ),
            )
            second = CoordinatedBatchRunner(
                pipeline=BatchPipeline(
                    store=batches,
                    resolver=resolver,
                    downloader=ImmediateDownloader(),
                ),
                coordinator=TaskExecutionCoordinator(
                    task_store=tasks,
                    run_id_factory=lambda: "run-2",
                ),
            )

            running = asyncio.create_task(first.run_batch("parent-1"))
            await asyncio.wait_for(resolver.started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            with self.assertRaises(TaskExecutionConflictError):
                await second.run_batch("parent-1")

            resolver.release.set()
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertEqual(result.snapshot.execution_status.value, "completed")
            self.assertEqual(result.task_status, TaskStatus.COMPLETED)
            self.assertTrue(first_events)
            self.assertEqual({event.payload["run_id"] for event in first_events}, {"run-1"})
            record = tasks.get("parent-1")
            self.assertEqual(record.status, TaskStatus.COMPLETED)
            self.assertIsNone(record.active_run_id)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_terminal_commit_retries_after_control_revision_race(self) -> None:
        async def scenario(directory: str) -> None:
            root = Path(directory)
            batches = StateBatchStore(root / "state.sqlite3")
            tasks = StateTaskStore(root / "state.sqlite3")
            entry = _entry()
            batches.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            tasks.upsert(
                TaskRecord(
                    task_id="parent-1",
                    status=TaskStatus.PENDING,
                    kind=TaskKind.BATCH,
                )
            )
            resolver = BlockingResolver()
            runner = CoordinatedBatchRunner(
                pipeline=BatchPipeline(
                    store=batches,
                    resolver=resolver,
                    downloader=ImmediateDownloader(),
                ),
                coordinator=RevisionRaceCoordinator(task_store=tasks),
            )

            running = asyncio.create_task(runner.run_batch("parent-1"))
            await asyncio.wait_for(resolver.started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            resolver.release.set()
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertEqual(result.task_status, TaskStatus.COMPLETED)
            self.assertEqual(tasks.get("parent-1").status, TaskStatus.COMPLETED)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))
