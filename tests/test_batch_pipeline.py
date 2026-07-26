from tests.state_store_support import StateBatchStore
import asyncio
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.application.batch_pipeline import BatchPipeline, BatchQueueConfig
from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    BatchExecutionStatus,
    DownloadAsset,
    DownloadChildStatus,
    DownloadPackage,
)
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.storage.completion_store import InMemoryCompletionStore
from cli_downloader.storage.history_store import InMemoryHistoryStore


ASYNC_TEST_TIMEOUT = 10


def make_entry(parent_id: str, entry_id: str, site_id: str = "example") -> BatchEntry:
    return BatchEntry(
        entry_id=entry_id,
        parent_id=parent_id,
        plugin_id=f"{site_id}_plugin",
        site_id=site_id,
        candidate_id=f"candidate-{entry_id}",
        source_operation_id="search",
        source_url=f"https://{site_id}.invalid/gallery/{entry_id}",
    )


def make_package(entry: BatchEntry) -> DownloadPackage:
    gallery_id = f"gallery-{entry.entry_id}"
    return DownloadPackage(
        package_id=f"package-{entry.parent_id}-{entry.entry_id}",
        version=1,
        plugin_id=entry.plugin_id,
        site_id=entry.site_id,
        gallery_id=gallery_id,
        source_operation_id="resolve_gallery",
        source_url=entry.source_url,
        strategy=DownloadStrategy.DIRECT,
        assets=(
            DownloadAsset(
                site_id=entry.site_id,
                gallery_id=gallery_id,
                plugin_asset_key="page-1",
                url=f"https://cdn.{entry.site_id}.invalid/{entry.entry_id}/1.jpg",
                filename="001.jpg",
                index=0,
            ),
        ),
    )


def create_batch(
    store: StateBatchStore,
    parent_id: str,
    sites: tuple[str, ...],
    *,
    id_prefix: str = "",
) -> tuple[BatchEntry, ...]:
    entries = tuple(
        make_entry(parent_id, f"{id_prefix}entry-{index}", site_id)
        for index, site_id in enumerate(sites, start=1)
    )
    store.create_batch(
        BatchParent(parent_id, tuple(entry.entry_id for entry in entries)),
        entries,
    )
    return entries


def complete_assets(assets: tuple[AssetRecord, ...]) -> tuple[AssetRecord, ...]:
    return tuple(
        replace(
            asset,
            status=AssetStatus.COMPLETED,
            final_path=f"output/{asset.asset.filename}",
            content_sha256="a" * 64,
        )
        for asset in assets
    )


class OverlapResolver:
    def __init__(self) -> None:
        self.slow_started = asyncio.Event()
        self.release_slow = asyncio.Event()

    async def resolve(self, entry, cancel_event, progress):
        progress("gallery_page", {"page": 1})
        if entry.entry_id == "entry-2":
            self.slow_started.set()
            await self.release_slow.wait()
        return make_package(entry)


class OverlapDownloader:
    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def execute(self, child, assets, cancel_event, progress):
        progress("asset_fetch", {"current": 1, "total": len(assets)})
        if child.entry_id == "entry-1":
            self.first_started.set()
            await self.release_first.wait()
        return complete_assets(assets)


class LimitedResolver:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.two_active = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.site_active: defaultdict[str, int] = defaultdict(int)
        self.site_max: defaultdict[str, int] = defaultdict(int)

    async def resolve(self, entry, cancel_event, progress):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.site_active[entry.site_id] += 1
        self.site_max[entry.site_id] = max(
            self.site_max[entry.site_id], self.site_active[entry.site_id]
        )
        if self.active == 2:
            self.two_active.set()
        try:
            await self.release.wait()
            return make_package(entry)
        finally:
            self.active -= 1
            self.site_active[entry.site_id] -= 1


class LimitedDownloader:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.two_active = asyncio.Event()
        self.active = 0
        self.max_active = 0
        self.site_active: defaultdict[str, int] = defaultdict(int)
        self.site_max: defaultdict[str, int] = defaultdict(int)

    async def execute(self, child, assets, cancel_event, progress):
        site_id = child.package.site_id
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.site_active[site_id] += 1
        self.site_max[site_id] = max(self.site_max[site_id], self.site_active[site_id])
        if self.active == 2:
            self.two_active.set()
        try:
            await self.release.wait()
            return complete_assets(assets)
        finally:
            self.active -= 1
            self.site_active[site_id] -= 1


class ImmediateDownloader:
    async def execute(self, child, assets, cancel_event, progress):
        return complete_assets(assets)


class BrokenUpdateStore(StateBatchStore):
    def update_entry(self, entry):
        raise RuntimeError("database unavailable")


class ErrorResolver:
    async def resolve(self, entry, cancel_event, progress):
        if entry.entry_id == "entry-1":
            raise OSError("gallery request failed")
        return make_package(entry)


class ErrorDownloader:
    async def execute(self, child, assets, cancel_event, progress):
        raise OSError("asset request failed")


class ImmediateResolver:
    async def resolve(self, entry, cancel_event, progress):
        return make_package(entry)


class TwoAssetResolver:
    async def resolve(self, entry, cancel_event, progress):
        package = make_package(entry)
        second = replace(
            package.assets[0],
            plugin_asset_key="page-2",
            url=f"https://cdn.{entry.site_id}.invalid/{entry.entry_id}/2.jpg",
            filename="002.jpg",
            index=2,
        )
        return replace(package, assets=(*package.assets, second))


class CancellingDownloader:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.second_started = asyncio.Event()
        self.completed_file = output_root / "entry-1" / "001.jpg"

    async def execute(self, child, assets, cancel_event, progress):
        if child.entry_id == "entry-1":
            self.completed_file.parent.mkdir(parents=True, exist_ok=True)
            self.completed_file.write_bytes(b"complete")
            return complete_assets(assets)
        if child.entry_id == "entry-2":
            self.second_started.set()
            await cancel_event.wait()
            raise asyncio.CancelledError
        return complete_assets(assets)


class PausingDownloader:
    def __init__(self) -> None:
        self.boundary_reached = asyncio.Event()
        self.cross_boundary = asyncio.Event()
        self.second_started = asyncio.Event()

    async def execute(self, child, assets, cancel_event, progress):
        outcomes = []
        for index, asset in enumerate(assets):
            if index == 1:
                self.boundary_reached.set()
                await self.cross_boundary.wait()
                await cancel_event.wait_if_paused()
                self.second_started.set()
            outcomes.extend(complete_assets((asset,)))
        return tuple(outcomes)


class BatchPipelineTests(TestCase):
    def test_completed_batch_records_durable_dedupe_before_execution_delete(self) -> None:
        """Batch completion must survive deletion of its execution aggregate.

        BatchPipeline writes the completed content identity through the
        Completion Repository before the execution aggregate can be deleted.
        """

        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            (entry,) = create_batch(store, "parent-1", ("example",))
            history = InMemoryHistoryStore()
            completion = InMemoryCompletionStore()
            pipeline = BatchPipeline(
                store=store,
                resolver=ImmediateResolver(),
                downloader=ImmediateDownloader(),
                history_store=history,
                completion_store=completion,
                child_id_factory=lambda item, package: f"child-{item.entry_id}",
            )

            result = await pipeline.run_batch("parent-1")
            self.assertEqual(result.snapshot.execution_status, BatchExecutionStatus.COMPLETED)
            store.delete_batch("parent-1")

            self.assertTrue(completion.is_completed(entry.site_id, entry.source_url))

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_item_pause_waits_at_file_boundary_and_resume_continues(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("example",))
            downloader = PausingDownloader()
            pipeline = BatchPipeline(
                store=store,
                resolver=TwoAssetResolver(),
                downloader=downloader,
                child_id_factory=lambda entry, package: f"child-{entry.entry_id}",
            )

            running = asyncio.create_task(pipeline.run_batch("parent-1"))
            await asyncio.wait_for(downloader.boundary_reached.wait(), timeout=ASYNC_TEST_TIMEOUT)
            self.assertTrue(pipeline.pause("entry-1"))
            self.assertTrue(pipeline.is_paused("entry-1"))
            self.assertFalse(pipeline.is_paused("missing"))
            downloader.cross_boundary.set()
            await asyncio.sleep(0.05)
            self.assertFalse(downloader.second_started.is_set())
            self.assertTrue(pipeline.resume("entry-1"))
            self.assertFalse(pipeline.is_paused("entry-1"))
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertTrue(downloader.second_started.is_set())
            self.assertEqual(result.snapshot.counts.completed, 1)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_ready_child_downloads_while_another_entry_is_still_resolving(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("example", "example"))
            resolver = OverlapResolver()
            downloader = OverlapDownloader()
            history = InMemoryHistoryStore()
            observed = []
            pipeline = BatchPipeline(
                store=store,
                resolver=resolver,
                downloader=downloader,
                config=BatchQueueConfig(resolution_workers=2, download_workers=1),
                history_store=history,
                event_sink=observed.append,
                child_id_factory=lambda entry, package: f"child-{entry.entry_id}",
            )

            running = asyncio.create_task(pipeline.run_batch("parent-1"))
            await asyncio.wait_for(resolver.slow_started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            await asyncio.wait_for(downloader.first_started.wait(), timeout=ASYNC_TEST_TIMEOUT)

            self.assertEqual(
                store.get_entry("entry-2").status,
                BatchEntryStatus.RESOLVING,
            )
            self.assertEqual(
                store.get_child("child-entry-1").status,
                DownloadChildStatus.DOWNLOADING,
            )
            resolver.release_slow.set()
            downloader.release_first.set()
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertEqual(result.snapshot.execution_status, BatchExecutionStatus.COMPLETED)
            self.assertEqual(tuple(observed), result.events)
            self.assertEqual(history.list(task_id="parent-1"), result.events)
            self.assertTrue(
                any(
                    event.payload.get("progress_reason") == "gallery_page"
                    for event in result.events
                )
            )

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_global_site_and_queue_limits_are_enforced(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("alpha", "alpha", "beta", "beta"))
            resolver = LimitedResolver()
            downloader = LimitedDownloader()
            pipeline = BatchPipeline(
                store=store,
                resolver=resolver,
                downloader=downloader,
                config=BatchQueueConfig(
                    resolution_workers=4,
                    download_workers=4,
                    resolution_queue_size=1,
                    download_queue_size=1,
                    site_resolution_limits={"alpha": 1, "beta": 1},
                    site_download_limits={"alpha": 1, "beta": 1},
                ),
                child_id_factory=lambda entry, package: f"child-{entry.entry_id}",
            )

            running = asyncio.create_task(pipeline.run_batch("parent-1"))
            await asyncio.wait_for(resolver.two_active.wait(), timeout=ASYNC_TEST_TIMEOUT)
            self.assertEqual(resolver.max_active, 2)
            self.assertEqual(dict(resolver.site_max), {"alpha": 1, "beta": 1})
            resolver.release.set()
            await asyncio.wait_for(downloader.two_active.wait(), timeout=ASYNC_TEST_TIMEOUT)
            self.assertEqual(downloader.max_active, 2)
            self.assertEqual(dict(downloader.site_max), {"alpha": 1, "beta": 1})
            downloader.release.set()

            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)
            self.assertEqual(result.snapshot.execution_status, BatchExecutionStatus.COMPLETED)
            self.assertLessEqual(result.resolution_queue_peak, 1)
            self.assertLessEqual(result.download_queue_peak, 1)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_global_limit_is_shared_by_concurrent_batches(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("alpha", "alpha"))
            create_batch(store, "parent-2", ("beta", "beta"), id_prefix="second-")
            resolver = LimitedResolver()
            pipeline = BatchPipeline(
                store=store,
                resolver=resolver,
                downloader=ImmediateDownloader(),
                config=BatchQueueConfig(
                    resolution_workers=2,
                    download_workers=2,
                    resolution_queue_size=2,
                    download_queue_size=2,
                ),
                child_id_factory=lambda entry, package: f"child-{entry.parent_id}-{entry.entry_id}",
            )

            first = asyncio.create_task(pipeline.run_batch("parent-1"))
            second = asyncio.create_task(pipeline.run_batch("parent-2"))
            await asyncio.wait_for(resolver.two_active.wait(), timeout=ASYNC_TEST_TIMEOUT)
            await asyncio.sleep(0)

            self.assertEqual(resolver.max_active, 2)
            resolver.release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second), timeout=ASYNC_TEST_TIMEOUT
            )
            self.assertTrue(
                all(
                    result.snapshot.execution_status is BatchExecutionStatus.COMPLETED
                    for result in results
                )
            )

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_plugin_and_system_errors_persist_at_their_owned_stages(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("example", "example"))
            pipeline = BatchPipeline(
                store=store,
                resolver=ErrorResolver(),
                downloader=ErrorDownloader(),
                child_id_factory=lambda entry, package: f"child-{entry.entry_id}",
            )

            result = await pipeline.run_batch("parent-1")

            first = store.get_entry("entry-1")
            second_child = store.get_child("child-entry-2")
            self.assertEqual(first.status, BatchEntryStatus.RESOLUTION_FAILED)
            self.assertEqual(first.failure.origin.value, "plugin")
            self.assertEqual(second_child.status, DownloadChildStatus.FAILED)
            self.assertEqual(second_child.failure.origin.value, "system")
            self.assertEqual(store.list_assets(second_child.child_id)[0].status, AssetStatus.FAILED)
            self.assertEqual(result.snapshot.execution_status, BatchExecutionStatus.FAILED)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_cancel_stops_both_stages_and_preserves_completed_work(self) -> None:
        async def scenario(directory: str) -> None:
            output_root = Path(directory) / "output"
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("example", "example", "example"))
            downloader = CancellingDownloader(output_root)
            pipeline = BatchPipeline(
                store=store,
                resolver=ImmediateResolver(),
                downloader=downloader,
                config=BatchQueueConfig(
                    resolution_workers=1,
                    download_workers=1,
                    resolution_queue_size=1,
                    download_queue_size=1,
                ),
                child_id_factory=lambda entry, package: f"child-{entry.entry_id}",
            )

            running = asyncio.create_task(pipeline.run_batch("parent-1"))
            await asyncio.wait_for(downloader.second_started.wait(), timeout=ASYNC_TEST_TIMEOUT)
            self.assertTrue(pipeline.cancel("parent-1"))
            result = await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            self.assertTrue(downloader.completed_file.exists())
            self.assertEqual(
                store.get_child("child-entry-1").status,
                DownloadChildStatus.COMPLETED,
            )
            self.assertEqual(
                store.get_child("child-entry-2").status,
                DownloadChildStatus.CANCELLED,
            )
            third = store.get_entry("entry-3")
            if third.child_id is None:
                self.assertEqual(third.status, BatchEntryStatus.CANCELLED)
            else:
                self.assertEqual(
                    store.get_child(third.child_id).status,
                    DownloadChildStatus.CANCELLED,
                )
            self.assertEqual(
                result.snapshot.execution_status, BatchExecutionStatus.PARTIALLY_COMPLETED
            )
            self.assertFalse(pipeline.cancel("parent-1"))

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_invalid_queue_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BatchQueueConfig(download_queue_size=0)
        with self.assertRaises(ValueError):
            BatchQueueConfig(site_resolution_limits={"example": 0})

    def test_unexpected_worker_error_terminates_without_queue_deadlock(self) -> None:
        async def scenario(directory: str) -> None:
            store = BrokenUpdateStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("example", "example"))
            pipeline = BatchPipeline(
                store=store,
                resolver=ImmediateResolver(),
                downloader=ImmediateDownloader(),
                config=BatchQueueConfig(
                    resolution_workers=1,
                    download_workers=1,
                    resolution_queue_size=1,
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await asyncio.wait_for(pipeline.run_batch("parent-1"), timeout=ASYNC_TEST_TIMEOUT)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_state_control_events_skip_event_sink(self) -> None:
        """Pause/resume/cancel events do not flow through event_sink."""
        from cli_downloader.application.batch_pipeline import _STATE_CONTROL_REASONS

        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            create_batch(store, "parent-1", ("example",))
            events: list[object] = []
            downloader = PausingDownloader()
            pipeline = BatchPipeline(
                store=store,
                resolver=TwoAssetResolver(),
                downloader=downloader,
                event_sink=events.append,
                child_id_factory=lambda entry, package: f"child-{entry.entry_id}",
            )

            running = asyncio.create_task(pipeline.run_batch("parent-1"))
            await asyncio.wait_for(downloader.boundary_reached.wait(), timeout=ASYNC_TEST_TIMEOUT)
            self.assertTrue(pipeline.pause("entry-1"))
            pipeline.cancel("parent-1")
            downloader.cross_boundary.set()
            await asyncio.wait_for(running, timeout=ASYNC_TEST_TIMEOUT)

            for event in events:
                self.assertNotIn(event.reason, _STATE_CONTROL_REASONS)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))


if __name__ == "__main__":
    import unittest

    unittest.main()
