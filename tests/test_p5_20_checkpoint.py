import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.batch_service import SystemBatchDownloader
from cli_downloader.application.download_limiter import GalleryDownloadLimiter
from cli_downloader.storage.file_writer import AssetFileWriter

from tests.state_store_support import StateBatchStore
from tests.test_batch_pipeline import complete_assets, create_batch, make_package
from dataclasses import replace


class ImmediateResolver:
    async def resolve(self, entry, cancel_event, progress):
        return make_package(entry)


class TorrentOwnershipDownloader:
    def __init__(self) -> None:
        self.torrent_started = asyncio.Event()
        self.release_torrent = asyncio.Event()
        self.other_started = asyncio.Event()
        self.release_other = asyncio.Event()

    async def execute(self, child, assets, cancel_event, progress):
        release_admission = getattr(cancel_event, "release_admission", None)
        if child.parent_id == "parent-a":
            if callable(release_admission):
                await release_admission()
            self.torrent_started.set()
            await self.release_torrent.wait()
        else:
            self.other_started.set()
            await self.release_other.wait()
        return complete_assets(assets)


class StreamFetcher:
    def __init__(self) -> None:
        self.a_chunks = 0
        self.a_first_chunk = asyncio.Event()
        self.b_started = asyncio.Event()
        self.b_release = asyncio.Event()

    async def fetch_stream(self, asset):
        if "parent-a" in asset.url:
            self.a_chunks += 1
            self.a_first_chunk.set()
            yield b"a", 2
            self.a_chunks += 1
            yield b"b", 2
            return
        self.b_started.set()
        yield b"b", 1
        await self.b_release.wait()


class P520CheckpointTests(IsolatedAsyncioTestCase):
    async def test_torrent_submission_releases_core_admission_before_polling(self) -> None:
        with TemporaryDirectory() as directory:
            limiter = GalleryDownloadLimiter(1, item_site_limit=1)
            store_a = StateBatchStore(Path(directory) / "a.sqlite3")
            store_b = StateBatchStore(Path(directory) / "b.sqlite3")
            create_batch(store_a, "parent-a", ("pixiv",))
            create_batch(store_b, "parent-b", ("ehentai",))
            downloader = TorrentOwnershipDownloader()
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
                await downloader.torrent_started.wait()
                await asyncio.wait_for(downloader.other_started.wait(), timeout=1)
                active = await limiter.active_snapshot()
                self.assertEqual({row["root_id"] for row in active}, {"parent-b"})
                downloader.release_other.set()
                downloader.release_torrent.set()
                await asyncio.gather(first, second)
            finally:
                downloader.release_other.set()
                downloader.release_torrent.set()
                for task in (first, second):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(first, second, return_exceptions=True)

    async def test_stream_pause_stops_next_chunk_and_allows_another_root_to_run(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            limiter = GalleryDownloadLimiter(1, item_site_limit=1)
            store_a = StateBatchStore(root / "stream-a.sqlite3")
            store_b = StateBatchStore(root / "stream-b.sqlite3")
            create_batch(store_a, "parent-a", ("pixiv",))
            create_batch(store_b, "parent-b", ("ehentai",))
            fetcher = StreamFetcher()

            class Resolver:
                async def resolve(self, entry, cancel_event, progress):
                    package = make_package(entry)
                    asset = replace(
                        package.assets[0],
                        url=f"https://cdn.invalid/{entry.parent_id}",
                    )
                    return replace(package, assets=(asset,))

            downloader_a = SystemBatchDownloader(
                file_writer=AssetFileWriter(root / "a"),
                store=store_a,
                fetchers={"pixiv": fetcher},
            )
            downloader_b = SystemBatchDownloader(
                file_writer=AssetFileWriter(root / "b"),
                store=store_b,
                fetchers={"ehentai": fetcher},
            )
            pipeline_a: BatchPipeline | None = None
            paused = False
            byte_updates = 0

            def sink(event) -> None:
                nonlocal byte_updates, paused
                if (
                    event.payload.get("progress_reason") == "byte_progress"
                ):
                    byte_updates += 1
                if (
                    not paused
                    and event.payload.get("progress_reason") == "byte_progress"
                ):
                    paused = True
                    assert pipeline_a is not None
                    pipeline_a.pause("parent-a")

            pipeline_a = BatchPipeline(
                store=store_a,
                resolver=Resolver(),
                downloader=downloader_a,
                download_limiter=limiter,
                event_sink=sink,
                child_id_factory=lambda entry, package: f"child-{entry.parent_id}",
            )
            pipeline_b = BatchPipeline(
                store=store_b,
                resolver=Resolver(),
                downloader=downloader_b,
                download_limiter=limiter,
                child_id_factory=lambda entry, package: f"child-{entry.parent_id}",
            )
            first = asyncio.create_task(pipeline_a.run_batch("parent-a"))
            second = asyncio.create_task(pipeline_b.run_batch("parent-b"))
            try:
                await fetcher.a_first_chunk.wait()
                await asyncio.wait_for(fetcher.b_started.wait(), timeout=1)
                self.assertEqual(byte_updates, 1)
                active = await limiter.active_snapshot()
                self.assertEqual({row["root_id"] for row in active}, {"parent-b"})
                pipeline_a.resume("parent-a")
                fetcher.b_release.set()
                await asyncio.gather(first, second)
            finally:
                pipeline_a.resume("parent-a")
                fetcher.b_release.set()
                for task in (first, second):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(first, second, return_exceptions=True)


if __name__ == "__main__":
    import unittest

    unittest.main()
