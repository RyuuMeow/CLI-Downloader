import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import IsolatedAsyncioTestCase

from cli_downloader.application.batch_execution_runner import CoordinatedBatchRunner
from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.download_limiter import GalleryDownloadLimiter, RequestPolicy
from cli_downloader.application.item_scheduler import ItemScheduler
from cli_downloader.application.task_execution_coordinator import TaskExecutionCoordinator
from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.storage.task_store import TaskStatus, TaskDesiredControl
from tests.state_store_support import StateBatchStore, StateTaskStore
from tests.test_batch_pipeline import complete_assets, create_batch, make_package


class QueueRuntimeTests(IsolatedAsyncioTestCase):
    async def test_queue_position_matches_round_robin_dispatch(self):
        scheduler = ItemScheduler(1, 1)
        first = await scheduler.acquire(root_id="A", site_id="site")
        actual = []

        async def run(root, sequence):
            async with scheduler.item(root_id=root, site_id="site", sequence=sequence):
                actual.append((root, sequence))

        workers = [
            asyncio.create_task(run(root, seq))
            for root, seq in (("A", 1), ("A", 2), ("B", 0), ("B", 1))
        ]
        await asyncio.sleep(0)
        shown = [(r["root_id"], r["sequence"]) for r in await scheduler.waiting_snapshot()]
        await first.release()
        await asyncio.gather(*workers)
        self.assertEqual(shown, actual)
        self.assertEqual(actual, [("B", 0), ("A", 1), ("B", 1), ("A", 2)])

    async def test_shared_scheduler_wakes_a_different_worker_loop(self):
        scheduler = ItemScheduler(1, 1)
        first = await scheduler.acquire(root_id="A", site_id="site")
        enqueued = Event()

        async def other_loop():
            waiting = asyncio.create_task(scheduler.acquire(root_id="B", site_id="site"))
            await asyncio.sleep(0)
            enqueued.set()
            lease = await asyncio.wait_for(waiting, 2)
            await lease.release()
            return True

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(lambda: asyncio.run(other_loop(), debug=True))
            self.assertTrue(await asyncio.to_thread(enqueued.wait, 2))
            await first.release()
            self.assertTrue(await asyncio.wrap_future(future))
        self.assertEqual(await scheduler.active_snapshot(), ())

    async def test_pausing_older_batch_controls_its_worker_and_preserves_revision(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "state.sqlite3"
            batches, tasks = StateBatchStore(db), StateTaskStore(db)
            service = TaskHierarchyService(task_store=tasks, batch_store=batches)
            started = {root: asyncio.Event() for root in ("A", "B")}
            boundary = {root: asyncio.Event() for root in ("A", "B")}
            advanced = {root: asyncio.Event() for root in ("A", "B")}

            class Resolver:
                async def resolve(self, entry, cancel_event, progress):
                    return make_package(entry)

            class Downloader:
                async def execute(self, child, assets, cancel_event, progress):
                    root = child.parent_id
                    started[root].set()
                    await boundary[root].wait()
                    await cancel_event.wait_if_paused()
                    advanced[root].set()
                    return complete_assets(assets)

            workers, pipelines = [], {}
            for root in ("A", "B"):
                create_batch(batches, root, ("example",), id_prefix=root)
                pipeline = BatchPipeline(
                    store=batches, resolver=Resolver(), downloader=Downloader()
                )
                coordinator = TaskExecutionCoordinator(task_store=tasks)
                pipelines[root] = pipeline
                service.configure_batch_runtime(
                    task_id=root,
                    pipeline=pipeline,
                    retry_service=None,
                    execution_coordinator=coordinator,
                )
                self.assertTrue(service.is_running(root))
                runner = CoordinatedBatchRunner(pipeline=pipeline, coordinator=coordinator)
                workers.append(asyncio.create_task(runner.run_batch(root)))
                await asyncio.wait_for(started[root].wait(), 2)
            try:
                await asyncio.to_thread(service.pause, "A")
                boundary["A"].set()

                async def wait_paused():
                    while tasks.get("A").status is not TaskStatus.PAUSED:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_paused(), 2)
                self.assertTrue(pipelines["A"].is_paused("A"))
                self.assertFalse(advanced["A"].is_set())
                self.assertTrue(service.is_batch_running("A"))
                boundary["B"].set()
                await asyncio.wait_for(advanced["B"].wait(), 2)
                await asyncio.to_thread(service.resume, "A")
                await asyncio.wait_for(asyncio.gather(*workers), 3)
                self.assertEqual(tasks.get("A").status, TaskStatus.COMPLETED)
                self.assertEqual(tasks.get("B").status, TaskStatus.COMPLETED)
            finally:
                for root, pipeline in pipelines.items():
                    pipeline.resume(root)
                    boundary[root].set()
                await asyncio.gather(*workers, return_exceptions=True)
                for root, pipeline in pipelines.items():
                    service.release_batch_runtime(root, pipeline)
            self.assertFalse(service.is_batch_running("A"))

    async def test_batch_can_use_more_than_two_global_slots(self):
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "batch.sqlite3")
            create_batch(store, "batch", ("example",) * 4)
            started = set()
            all_started, release = asyncio.Event(), asyncio.Event()

            class Resolver:
                async def resolve(self, entry, cancel_event, progress):
                    return make_package(entry)

            class Downloader:
                async def execute(self, child, assets, cancel_event, progress):
                    started.add(child.child_id)
                    if len(started) == 4:
                        all_started.set()
                    await release.wait()
                    return complete_assets(assets)

            pipeline = BatchPipeline(
                store=store,
                resolver=Resolver(),
                downloader=Downloader(),
                download_limiter=GalleryDownloadLimiter(4, item_site_limit=4),
            )
            worker = asyncio.create_task(pipeline.run_batch("batch"))
            try:
                await asyncio.wait_for(all_started.wait(), 3)
            finally:
                release.set()
                await worker

    async def test_pause_inside_request_releases_single_site_permit(self):
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            limiter = GalleryDownloadLimiter(
                1, default_request_policy=RequestPolicy(max_concurrency=1, min_interval_seconds=0)
            )
            a_started, a_boundary, b_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

            class Resolver:
                async def resolve(self, entry, cancel_event, progress):
                    return make_package(entry)

            class Downloader:
                async def execute(self, child, assets, cancel_event, progress):
                    async def request():
                        if child.parent_id == "A":
                            a_started.set()
                            await a_boundary.wait()
                            await cancel_event.wait_if_paused()
                        else:
                            b_finished.set()
                        return complete_assets(assets)

                    return await limiter.request("example", request)

            pipelines = {}
            for root in ("A", "B"):
                create_batch(store, root, ("example",), id_prefix=root)
                pipelines[root] = BatchPipeline(
                    store=store,
                    resolver=Resolver(),
                    downloader=Downloader(),
                    download_limiter=limiter,
                )
            a = asyncio.create_task(pipelines["A"].run_batch("A"))
            await asyncio.wait_for(a_started.wait(), 2)
            b = asyncio.create_task(pipelines["B"].run_batch("B"))
            try:
                pipelines["A"].pause("A")
                a_boundary.set()
                await asyncio.wait_for(b_finished.wait(), 2)
                self.assertFalse(a.done())
                await b
                pipelines["A"].resume("A")
                await asyncio.wait_for(a, 2)
                self.assertEqual(await limiter.active_snapshot(), ())
                self.assertEqual(
                    await limiter.request("example", lambda: asyncio.sleep(0, result="ok")), "ok"
                )
            finally:
                pipelines["A"].resume("A")
                a_boundary.set()
                for worker in (a, b):
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(a, b, return_exceptions=True)

    async def test_cancelled_request_waiter_does_not_leak_capacity(self):
        limiter = GalleryDownloadLimiter(
            default_request_policy=RequestPolicy(max_concurrency=1, min_interval_seconds=0)
        )
        started, release = asyncio.Event(), asyncio.Event()

        async def first():
            started.set()
            await release.wait()

        holder = asyncio.create_task(limiter.request("site", first))
        await started.wait()
        cancelled = asyncio.create_task(limiter.request("site", lambda: asyncio.sleep(0)))
        await asyncio.sleep(0.03)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        release.set()
        await holder
        # A leaked executor waiter steals the newly released permit here.
        await asyncio.sleep(0.03)
        await asyncio.wait_for(limiter.request("site", lambda: asyncio.sleep(0)), 1)

    async def test_standalone_reports_pausing_until_stream_boundary(self):
        from cli_downloader.application.download_service import WriteAssetsStage
        from cli_downloader.core.models import (
            Asset,
            AssetKind,
            AssetList,
            DownloadStrategy,
            DownloadStrategyPlan,
            ResolvedItem,
        )
        from cli_downloader.core.pipeline import PipelineContext
        from cli_downloader.storage.file_writer import AssetFileWriter
        from cli_downloader.storage.task_store import TaskRecord
        from cli_downloader.core.task_tree import TaskState

        with TemporaryDirectory() as directory:
            root = Path(directory)
            tasks, batches = (
                StateTaskStore(root / "state.sqlite3"),
                StateBatchStore(root / "state.sqlite3"),
            )
            limiter = GalleryDownloadLimiter(
                1, default_request_policy=RequestPolicy(max_concurrency=1, min_interval_seconds=0)
            )
            service = TaskHierarchyService(task_store=tasks, batch_store=batches, scheduler=limiter)
            started, boundary, b_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

            class Fetcher:
                async def fetch_stream(self, asset):
                    if asset.filename == "a.txt":
                        started.set()
                        await boundary.wait()
                    else:
                        b_started.set()
                    yield b"data", 4

            def run(root_id):
                tasks.upsert(TaskRecord(root_id, TaskStatus.PLANNED))
                service.register_standalone_runtime(root_id)
                stage = WriteAssetsStage(
                    Fetcher(),
                    AssetFileWriter(root / root_id),
                    task_store=tasks,
                    download_limiter=limiter,
                )
                plan = DownloadStrategyPlan(
                    strategy=DownloadStrategy.DIRECT,
                    reason="test",
                    assets=AssetList(
                        (
                            Asset(
                                f"https://example.test/{root_id}", AssetKind.FILE, f"{root_id}.txt"
                            ),
                        )
                    ),
                )
                return asyncio.create_task(
                    stage.run(
                        PipelineContext(
                            root_id,
                            {
                                "item": ResolvedItem("example", f"example://{root_id}", root_id),
                                "plan": plan,
                            },
                        )
                    )
                )

            a = run("a")
            await asyncio.wait_for(started.wait(), 2)
            b = run("b")
            try:
                service.pause("a")
                self.assertEqual(service.get("a").state, TaskState.PAUSING)
                boundary.set()
                await asyncio.wait_for(b_started.wait(), 2)
                self.assertEqual(service.get("a").state, TaskState.PAUSED)
                self.assertFalse(a.done())
                await b
                service.resume("a")
                await asyncio.wait_for(a, 2)
            finally:
                boundary.set()
                for worker in (a, b):
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(a, b, return_exceptions=True)

    async def test_rejected_pause_does_not_suspend_later_retry(self):
        from cli_downloader.storage.task_store import TaskRecord
        from cli_downloader.core.errors import InvalidTaskTransitionError

        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            tasks, batches = StateTaskStore(database), StateBatchStore(database)
            limiter = GalleryDownloadLimiter(1)
            service = TaskHierarchyService(task_store=tasks, batch_store=batches, scheduler=limiter)
            tasks.upsert(
                TaskRecord(
                    "finished", TaskStatus.COMPLETED, desired_control=TaskDesiredControl.PAUSE
                )
            )
            with self.assertRaises(InvalidTaskTransitionError):
                service.pause("finished")
            await service.retry("finished")
            lease = await asyncio.wait_for(
                limiter.acquire_item(root_id="finished", site_id="example"), 1
            )
            await lease.release()
