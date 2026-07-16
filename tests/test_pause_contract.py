import asyncio
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, expectedFailure

from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.download_limiter import GalleryDownloadLimiter

from tests.test_batch_pipeline import (
    complete_assets,
    create_batch,
    make_package,
)
from tests.state_store_support import StateBatchStore


async def _wait_for_row(
    snapshot_factory,
    *,
    root_id: str,
    active: bool,
    turns: int = 200,
) -> None:
    """Wait for a scheduler diagnostic row without relying on wall-clock sleeps."""

    for _ in range(turns):
        rows = await snapshot_factory()
        found = any(row["root_id"] == root_id for row in rows)
        if found is active:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"scheduler row did not reach active={active}: {root_id}")


class ImmediateResolver:
    async def resolve(self, entry, cancel_event, progress):
        return make_package(entry)


class PauseBoundaryDownloader:
    def __init__(self) -> None:
        self.active_started = asyncio.Event()
        self.about_to_wait = asyncio.Event()
        self.allow_boundary = asyncio.Event()
        self.other_started = asyncio.Event()

    async def execute(self, child, assets, cancel_event, progress):
        if child.parent_id == "parent-a":
            self.active_started.set()
            await self.allow_boundary.wait()
            self.about_to_wait.set()
            await cancel_event.wait_if_paused()
            if cancel_event.is_set():
                raise asyncio.CancelledError
        else:
            self.other_started.set()
        return complete_assets(assets)


class HoldingDownloader:
    def __init__(self) -> None:
        self.active_started = asyncio.Event()
        self.release_active = asyncio.Event()
        self.other_started = asyncio.Event()

    async def execute(self, child, assets, cancel_event, progress):
        if child.parent_id == "parent-a":
            self.active_started.set()
            await self.release_active.wait()
        else:
            self.other_started.set()
        return complete_assets(assets)


class PauseContractReproductionTests(IsolatedAsyncioTestCase):
    @expectedFailure
    async def test_paused_root_releases_item_lease_before_quiescent_ack(self) -> None:
        """Current code keeps the central Item lease while a Batch waits paused."""

        with TemporaryDirectory() as directory:
            limiter = GalleryDownloadLimiter(1, item_site_limit=1)
            store_a = StateBatchStore(f"{directory}/a.sqlite3")
            store_b = StateBatchStore(f"{directory}/b.sqlite3")
            create_batch(store_a, "parent-a", ("pixiv",))
            create_batch(store_b, "parent-b", ("ehentai",))
            downloader = PauseBoundaryDownloader()
            pipeline_a = BatchPipeline(
                store=store_a,
                resolver=ImmediateResolver(),
                downloader=downloader,
                download_limiter=limiter,
                child_id_factory=lambda entry, package: f"child-{entry.parent_id}",
            )
            pipeline_b = BatchPipeline(
                store=store_b,
                resolver=ImmediateResolver(),
                downloader=downloader,
                download_limiter=limiter,
                child_id_factory=lambda entry, package: f"child-{entry.parent_id}",
            )
            first = asyncio.create_task(pipeline_a.run_batch("parent-a"))
            second = asyncio.create_task(pipeline_b.run_batch("parent-b"))
            try:
                await downloader.active_started.wait()
                await _wait_for_row(limiter.waiting_snapshot, root_id="parent-b", active=False)
                self.assertTrue(pipeline_a.pause("parent-a"))
                downloader.allow_boundary.set()
                await downloader.about_to_wait.wait()

                active = await limiter.active_snapshot()
                self.assertEqual(active, ())
            finally:
                pipeline_a.resume("parent-a")
                downloader.allow_boundary.set()
                await asyncio.gather(first, second, return_exceptions=True)

    @expectedFailure
    async def test_paused_waiting_root_cannot_acquire_a_released_item_lease(self) -> None:
        """Current code grants a slot to a waiter that was paused mid-acquire."""

        with TemporaryDirectory() as directory:
            limiter = GalleryDownloadLimiter(1, item_site_limit=1)
            store_a = StateBatchStore(f"{directory}/a.sqlite3")
            store_b = StateBatchStore(f"{directory}/b.sqlite3")
            create_batch(store_a, "parent-a", ("pixiv",))
            create_batch(store_b, "parent-b", ("ehentai",))
            downloader = HoldingDownloader()
            pipeline_a = BatchPipeline(
                store=store_a,
                resolver=ImmediateResolver(),
                downloader=downloader,
                download_limiter=limiter,
                child_id_factory=lambda entry, package: f"child-{entry.parent_id}",
            )
            pipeline_b = BatchPipeline(
                store=store_b,
                resolver=ImmediateResolver(),
                downloader=downloader,
                download_limiter=limiter,
                child_id_factory=lambda entry, package: f"child-{entry.parent_id}",
            )
            first = asyncio.create_task(pipeline_a.run_batch("parent-a"))
            second = asyncio.create_task(pipeline_b.run_batch("parent-b"))
            try:
                await downloader.active_started.wait()
                await _wait_for_row(limiter.waiting_snapshot, root_id="parent-b", active=False)
                self.assertTrue(pipeline_b.pause("parent-b"))
                downloader.release_active.set()
                await first
                await _wait_for_row(limiter.active_snapshot, root_id="parent-b", active=True)

                active = await limiter.active_snapshot()
                self.assertNotIn("parent-b", {row["root_id"] for row in active})
            finally:
                pipeline_b.resume("parent-b")
                downloader.release_active.set()
                await asyncio.gather(first, second, return_exceptions=True)


if __name__ == "__main__":
    import unittest

    unittest.main()
