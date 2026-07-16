import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase

from cli_downloader.application.download_service import DownloadService, WriteAssetsStage
from cli_downloader.application.download_limiter import GalleryDownloadLimiter
from cli_downloader.application.execution_control import ExecutionStopSignal, OwnerShutdownRequested
from cli_downloader.core.models import (
    Asset,
    AssetKind,
    AssetList,
    DownloadStrategy,
    DownloadStrategyPlan,
    ResolvedItem,
)
from cli_downloader.core.pipeline import PipelineContext
from cli_downloader.integrations.torrent import TorrentAsset, TorrentHandle, TorrentOutcome
from cli_downloader.sites.registry import SiteRegistry
from tests.example_site import ExampleAdapter, ExampleAssetFetcher
from cli_downloader.storage.completion_store import InMemoryCompletionStore
from cli_downloader.storage.file_writer import AssetFileWriter
from cli_downloader.storage.history_store import InMemoryHistoryStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import InMemoryTaskStore, TaskRecord, TaskStatus


class FailSecondFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, asset):
        self.calls += 1
        if self.calls == 2:
            raise OSError("second asset failed")
        return b"first asset"


class ShutdownAfterWrite(AssetFileWriter):
    def __init__(self, root: Path, signal: ExecutionStopSignal) -> None:
        super().__init__(root)
        self.signal = signal

    def write(self, asset, content, **kwargs):
        result = super().write(asset, content, **kwargs)
        self.signal.request_shutdown()
        return result


class StaticFetcher:
    async def fetch(self, _asset):
        return b"asset written before shutdown boundary"


class CountingTorrentFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, _asset):
        self.calls += 1
        return b"torrent descriptor"


class ExistingTorrentClient:
    def __init__(self, outcome: TorrentOutcome, poll_outcome: TorrentOutcome | None = None) -> None:
        self.outcome = outcome
        self.poll_outcome = poll_outcome or outcome
        self.status_calls = 0
        self.add_calls = 0
        self.poll_calls = 0

    async def get_status(self, handle: TorrentHandle) -> TorrentOutcome:
        self.status_calls += 1
        return self.outcome

    async def add_torrent(self, _torrent, _download_dir):
        self.add_calls += 1
        return TorrentHandle("new-handle", "new.torrent")

    async def poll_until_complete(self, _handle, **_kwargs):
        self.poll_calls += 1
        return self.poll_outcome


class StandaloneResumeTest(TestCase):
    def test_completed_checkpoint_skips_fetch_and_add(self) -> None:
        store = InMemoryTaskStore()
        asset = Asset(
            url="https://example.test/item.torrent",
            kind=AssetKind.TORRENT,
            filename="item.torrent",
        )
        handle = TorrentHandle("existing-hash", "item.torrent")
        store.upsert(
            TaskRecord(
                "task-1",
                TaskStatus.RUNNING,
                metadata={
                    "torrent_checkpoints": {
                        "1:https://example.test/item.torrent": {
                            "client_id": handle.client_id,
                            "name": handle.name,
                            "submission": "accepted",
                            "descriptor_path": "downloads/item.torrent",
                        }
                    }
                },
            )
        )
        client = ExistingTorrentClient(
            TorrentOutcome(
                handle,
                "completed",
                "downloads",
                found=True,
                progress=1.0,
                completed_bytes=1024,
                total_bytes=1024,
            )
        )
        fetcher = CountingTorrentFetcher()
        stage = WriteAssetsStage(
            fetcher=fetcher,
            file_writer=AssetFileWriter(Path("downloads")),
            task_store=store,
            torrent_client=client,
        )
        plan = DownloadStrategyPlan(
            strategy=DownloadStrategy.TORRENT,
            reason="test",
            assets=AssetList((asset,)),
        )

        result = asyncio.run(
            stage._run_torrent(
                PipelineContext("task-1"),
                ResolvedItem("example", "https://example.test/gallery", "Example"),
                plan,
            )
        )

        self.assertEqual(fetcher.calls, 0)
        self.assertEqual(client.add_calls, 0)
        self.assertEqual(client.status_calls, 1)
        self.assertEqual(len(result.data["written_assets"]), 1)

    def test_downloading_checkpoint_reuses_handle_for_polling(self) -> None:
        store = InMemoryTaskStore()
        asset = Asset(
            url="https://example.test/item.torrent",
            kind=AssetKind.TORRENT,
            filename="item.torrent",
        )
        handle = TorrentHandle("existing-hash", "item.torrent")
        store.upsert(
            TaskRecord(
                "task-1",
                TaskStatus.RUNNING,
                metadata={
                    "torrent_checkpoints": {
                        "1:https://example.test/item.torrent": {
                            "client_id": handle.client_id,
                            "name": handle.name,
                            "submission": "accepted",
                            "descriptor_path": "downloads/item.torrent",
                        }
                    }
                },
            )
        )
        client = ExistingTorrentClient(
            TorrentOutcome(handle, "downloading", "downloads", found=True),
            TorrentOutcome(
                handle,
                "completed",
                "downloads",
                found=True,
                progress=1.0,
            ),
        )
        fetcher = CountingTorrentFetcher()
        stage = WriteAssetsStage(
            fetcher=fetcher,
            file_writer=AssetFileWriter(Path("downloads")),
            task_store=store,
            torrent_client=client,
        )
        plan = DownloadStrategyPlan(
            strategy=DownloadStrategy.TORRENT,
            reason="test",
            assets=AssetList((asset,)),
        )

        asyncio.run(
            stage._run_torrent(
                PipelineContext("task-1"),
                ResolvedItem("example", "https://example.test/gallery", "Example"),
                plan,
            )
        )

        self.assertEqual(fetcher.calls, 0)
        self.assertEqual(client.add_calls, 0)
        self.assertEqual(client.status_calls, 1)
        self.assertEqual(client.poll_calls, 1)


class RecordingTorrentClient:
    def __init__(self) -> None:
        self.added: list[tuple[TorrentAsset, Path]] = []

    async def add_torrent(self, torrent: TorrentAsset, download_dir: Path) -> TorrentHandle:
        self.added.append((torrent, download_dir))
        return TorrentHandle(client_id="hash-1", name=torrent.name)

    async def poll_until_complete(
        self, handle: TorrentHandle, *, cancel_requested=None, status_callback=None
    ):
        outcome = TorrentOutcome(
            handle=handle,
            status="completed",
            output_dir="",
            found=True,
            progress=1.0,
            completed_bytes=1024,
            total_bytes=1024,
            files=("downloaded-file.bin",),
        )
        if status_callback is not None:
            status_callback(outcome)
        return outcome


class DownloadServiceTest(TestCase):
    def test_owner_shutdown_keeps_standalone_task_planned(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        signal = ExecutionStopSignal()
        signal.request_shutdown()
        service = DownloadService(registry=registry, task_store=store)

        async def run() -> None:
            with self.assertRaises(OwnerShutdownRequested):
                await service.download_task(
                    "example://gallery/shutdown",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(Path("downloads")),
                    cancel_signal=signal,
                    task_id="shutdown-task",
                )

        asyncio.run(run())
        record = store.get("shutdown-task")
        self.assertIsNotNone(record)
        self.assertEqual(record.status, TaskStatus.PLANNED)
        self.assertTrue(record.metadata["owner_shutdown_checkpoint"])

    def test_owner_shutdown_at_terminal_boundary_does_not_complete_item(self) -> None:
        with TemporaryDirectory() as directory:
            store = InMemoryTaskStore()
            signal = ExecutionStopSignal()
            stage = WriteAssetsStage(
                fetcher=StaticFetcher(),
                file_writer=ShutdownAfterWrite(Path(directory), signal),
                task_store=store,
                cancel_signal=signal,
            )
            store.upsert(TaskRecord("boundary-task", TaskStatus.PLANNED))
            plan = DownloadStrategyPlan(
                strategy=DownloadStrategy.DIRECT,
                reason="test",
                assets=AssetList(
                    (Asset("https://example.test/asset", AssetKind.FILE, "boundary.txt"),)
                ),
            )

            async def run() -> None:
                with self.assertRaises(OwnerShutdownRequested):
                    await stage._run(
                        PipelineContext(
                            "boundary-task",
                            data={
                                "item": ResolvedItem(
                                    "example", "example://boundary", "Boundary"
                                ),
                                "plan": plan,
                            },
                        )
                    )

            asyncio.run(run())
            record = store.get("boundary-task")
            self.assertIsNotNone(record)
            self.assertEqual(record.status, TaskStatus.RUNNING)
            self.assertEqual(record.revision, 2)

    def test_standalone_pause_releases_shared_item_slot_between_assets(self) -> None:
        async def scenario(directory: Path) -> None:
            limiter = GalleryDownloadLimiter(1, item_site_limit=1)
            store_a = InMemoryTaskStore()
            store_b = InMemoryTaskStore()
            store_a.upsert(TaskRecord("task-a", TaskStatus.PLANNED))
            store_b.upsert(TaskRecord("task-b", TaskStatus.PLANNED))
            item_a = ResolvedItem("example", "example://a", "A")
            item_b = ResolvedItem("example", "example://b", "B")
            plan_a = DownloadStrategyPlan(
                strategy=DownloadStrategy.DIRECT,
                reason="test",
                assets=AssetList(
                    (
                        Asset("https://example.test/a/1", AssetKind.FILE, "a-1.txt"),
                        Asset("https://example.test/a/2", AssetKind.FILE, "a-2.txt"),
                    )
                ),
            )
            plan_b = DownloadStrategyPlan(
                strategy=DownloadStrategy.DIRECT,
                reason="test",
                assets=AssetList((Asset("https://example.test/b/1", AssetKind.FILE, "b-1.txt"),)),
            )
            b_started = asyncio.Event()
            b_release = asyncio.Event()

            class Fetcher:
                def __init__(
                    self,
                    started: asyncio.Event | None = None,
                    release: asyncio.Event | None = None,
                ) -> None:
                    self.started = started
                    self.release = release

                async def fetch(self, asset):
                    if self.started is not None:
                        self.started.set()
                    if self.release is not None:
                        await self.release.wait()
                    return asset.filename.encode()

            def pause_after_first(current: int, _total: int) -> None:
                if current == 1:
                    record = store_a.get("task-a")
                    assert record is not None
                    store_a.upsert(record.transition(status=TaskStatus.PAUSED))

            stage_a = WriteAssetsStage(
                fetcher=Fetcher(),
                file_writer=AssetFileWriter(directory / "a"),
                task_store=store_a,
                progress=pause_after_first,
                download_limiter=limiter,
            )
            stage_b = WriteAssetsStage(
                fetcher=Fetcher(b_started, b_release),
                file_writer=AssetFileWriter(directory / "b"),
                task_store=store_b,
                download_limiter=limiter,
            )
            task_a = asyncio.create_task(
                stage_a.run(PipelineContext("task-a", {"item": item_a, "plan": plan_a}))
            )
            await asyncio.sleep(0)
            task_b = asyncio.create_task(
                stage_b.run(PipelineContext("task-b", {"item": item_b, "plan": plan_b}))
            )
            try:
                await asyncio.wait_for(b_started.wait(), timeout=1)
                active = await limiter.active_snapshot()
                self.assertEqual({row["root_id"] for row in active}, {"task-b"})
                b_release.set()
                record = store_a.get("task-a")
                assert record is not None
                store_a.upsert(record.transition(status=TaskStatus.PLANNED))
                await asyncio.gather(task_a, task_b)
            finally:
                for task in (task_a, task_b):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(task_a, task_b, return_exceptions=True)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_torrent_checkpoint_persists_client_progress(self) -> None:
        store = InMemoryTaskStore()
        store.upsert(TaskRecord("task-1", TaskStatus.PLANNED))
        client = RecordingTorrentClient()
        service = WriteAssetsStage(
            fetcher=ExampleAssetFetcher(),
            file_writer=AssetFileWriter(Path("downloads")),
            task_store=store,
            torrent_client=client,
        )
        asset = Asset(
            url="https://cdn.example.test/item.torrent",
            kind=AssetKind.TORRENT,
            filename="item.torrent",
        )
        handle = TorrentHandle(client_id="abc123", name="item.torrent")

        service._persist_torrent_checkpoint(
            "task-1",
            asset,
            handle,
            download_dir=Path("downloads"),
            descriptor_path=Path("downloads/item.torrent"),
            status="submitted",
        )
        service._persist_torrent_outcome(
            "task-1",
            asset,
            TorrentOutcome(
                handle=handle,
                status="downloading",
                output_dir="downloads",
                found=True,
                client_state="downloading",
                progress=0.5,
                completed_bytes=512,
                total_bytes=1024,
                download_speed=128,
            ),
        )

        checkpoint = store.get("task-1").metadata["torrent_checkpoints"][
            "1:https://cdn.example.test/item.torrent"
        ]
        self.assertEqual(checkpoint["client_id"], "abc123")
        self.assertEqual(checkpoint["progress"], 0.5)
        self.assertEqual(checkpoint["completed_bytes"], 512)
        self.assertEqual(checkpoint["total_bytes"], 1024)

    def test_torrent_control_callbacks_follow_standalone_task_status(self) -> None:
        store = InMemoryTaskStore()
        record = TaskRecord("task-1", TaskStatus.PLANNED)
        store.upsert(record)
        service = WriteAssetsStage(
            fetcher=ExampleAssetFetcher(),
            file_writer=AssetFileWriter(Path("downloads")),
            task_store=store,
        )

        self.assertFalse(service._torrent_pause_requested("task-1"))
        self.assertFalse(service._torrent_cancel_requested("task-1"))

        paused = record.transition(status=TaskStatus.PAUSED)
        store.upsert(paused)
        self.assertTrue(service._torrent_pause_requested("task-1"))

        async def wait_for_resume() -> None:
            waiter = asyncio.create_task(service._wait_for_torrent_resume("task-1"))
            await asyncio.sleep(0)
            store.upsert(paused.transition(status=TaskStatus.PLANNED))
            await asyncio.wait_for(waiter, timeout=1)

        asyncio.run(wait_for_resume())

        store.upsert(paused.transition(status=TaskStatus.CANCELED))
        self.assertTrue(service._torrent_cancel_requested("task-1"))

    def test_partial_failure_persists_completed_file_progress(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        service = DownloadService(
            registry=registry,
            task_store=store,
            task_id_factory=lambda: "task-1",
        )

        with TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                asyncio.run(
                    service.download_task(
                        "example://gallery/welcome",
                        fetcher=FailSecondFetcher(),
                        file_writer=AssetFileWriter(Path(directory)),
                    )
                )

        record = store.get("task-1")
        self.assertEqual(record.status, TaskStatus.PARTIALLY_COMPLETED)
        self.assertEqual(record.metadata["completed_files"], 1)
        self.assertEqual(len(record.metadata["output_files"]), 1)

    def test_plan_task_resolves_plans_and_persists_record(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        service = DownloadService(
            registry=registry,
            task_store=store,
            task_id_factory=lambda: "task-1",
        )

        result = asyncio.run(service.plan_task("example://gallery/welcome"))

        self.assertEqual(result.task_id, "task-1")
        self.assertEqual(result.item.site_id, "example")
        self.assertEqual(result.item.title, "Example Item: gallery/welcome")
        self.assertEqual(result.record.status, TaskStatus.PLANNED)
        self.assertEqual(store.get("task-1"), result.record)
        self.assertEqual(result.plan.reason, "example_direct_assets")
        self.assertEqual(
            [event.reason for event in result.events],
            [
                "persist_planned_task_started",
                "planned_task_persisted",
                "persist_planned_task_finished",
            ],
        )

    def test_plan_task_uses_completion_ledger_to_skip_existing_download(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        completion = InMemoryCompletionStore()
        completion.record_completed("example", "example://gallery/welcome")
        service = DownloadService(
            registry=registry,
            task_store=InMemoryTaskStore(),
            completion_store=completion,
            task_id_factory=lambda: "task-1",
        )

        result = asyncio.run(service.plan_task("example://gallery/welcome"))

        self.assertEqual(result.record.status, TaskStatus.SKIPPED)

    def test_download_task_writes_assets_and_completes_record(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        service = DownloadService(
            registry=registry,
            task_store=store,
            task_id_factory=lambda: "task-1",
        )

        with TemporaryDirectory() as directory:
            result = asyncio.run(
                service.download_task(
                    "example://gallery/welcome",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(Path(directory)),
                )
            )

            output_files = [asset.path for asset in result.written_assets]
            self.assertEqual(len(output_files), 2)
            for output_file in output_files:
                self.assertTrue(output_file.exists())

        self.assertEqual(result.record.status, TaskStatus.COMPLETED)
        self.assertEqual(store.get("task-1"), result.record)
        self.assertEqual(result.record.metadata["completed_files"], 2)
        self.assertEqual(result.record.metadata["total_files"], 2)
        self.assertEqual(result.events[-1].reason, "complete_task_finished")

    def test_state_download_persists_history_projection_after_task_is_deleted(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "state.sqlite3")
            tasks = StateTaskStore(database)
            history = StateHistoryStore(database)
            service = DownloadService(
                registry=registry,
                task_store=tasks,
                history_store=history,
                task_id_factory=lambda: "task-1",
                auto_delete_completed=True,
            )

            result = asyncio.run(
                service.download_task(
                    "example://gallery/welcome",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(root / "downloads"),
                )
            )

            self.assertIsNone(tasks.get("task-1"))
            record = history.get_record("task-1")
            self.assertIsNotNone(record)
            self.assertEqual(record.kind.value, "item")
            self.assertEqual(record.title, result.item.title)
            self.assertEqual(record.source_url, result.item.source_url)
            self.assertEqual(record.status, "completed")
            self.assertEqual(record.completed_files, 2)
            self.assertEqual(len(record.output_files), 2)

    def test_download_task_reuses_recovered_task_id(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        store.upsert(
            TaskRecord(
                task_id="task-1",
                status=TaskStatus.PLANNED,
                revision=5,
                run_generation=3,
                source_url="example://gallery/welcome",
                metadata={"recovered": True},
            )
        )
        service = DownloadService(registry=registry, task_store=store)

        with TemporaryDirectory() as directory:
            result = asyncio.run(
                service.download_task(
                    "example://gallery/welcome",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(Path(directory)),
                    task_id="task-1",
                )
            )

        self.assertEqual(result.task_id, "task-1")
        self.assertTrue(result.record.metadata["recovered"])
        self.assertGreaterEqual(result.record.revision, 5)
        self.assertEqual(result.record.run_generation, 3)

    def test_download_task_applies_gallery_folder_template(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        service = DownloadService(
            registry=registry,
            task_store=InMemoryTaskStore(),
            task_id_factory=lambda: "task-1",
        )

        with TemporaryDirectory() as directory:
            result = asyncio.run(
                service.download_task(
                    "example://gallery/welcome",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(
                        Path(directory),
                        folder_template="{site_id}/{gallery_id}-{title}",
                    ),
                )
            )

            self.assertTrue(
                all(
                    asset.path.parent.relative_to(directory)
                    == Path("example") / "gallery_welcome-Example Item_ gallery_welcome"
                    for asset in result.written_assets
                )
            )

    def test_download_task_snapshots_plugin_output_policy(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        service = DownloadService(
            registry=registry,
            task_store=store,
            task_id_factory=lambda: "task-1",
            output_folder_template="{slug}/{title}",
            output_field_names=("slug",),
        )

        with TemporaryDirectory() as directory:
            result = asyncio.run(
                service.download_task(
                    "example://gallery/welcome",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(Path(directory)),
                )
            )

            self.assertTrue(
                all(
                    asset.path.parent.relative_to(directory)
                    == Path("gallery_welcome") / "Example Item_ gallery_welcome"
                    for asset in result.written_assets
                )
            )
        self.assertEqual(result.record.metadata["output_folder_template"], "{slug}/{title}")
        self.assertEqual(result.record.metadata["output_folder_fields"]["slug"], "gallery/welcome")

    def test_cancelled_download_keeps_written_assets_and_records_status(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        history = InMemoryHistoryStore()
        cancellation = Event()
        service = DownloadService(
            registry=registry,
            task_store=store,
            history_store=history,
            task_id_factory=lambda: "task-1",
        )

        def report(current: int, total: int) -> None:
            if current == 1:
                cancellation.set()

        with TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(
                    service.download_task(
                        "example://gallery/welcome",
                        fetcher=ExampleAssetFetcher(),
                        file_writer=AssetFileWriter(output),
                        cancel_signal=cancellation,
                        progress=report,
                    )
                )

            self.assertEqual(len(tuple(output.rglob("*.txt"))), 1)

        self.assertEqual(store.get("task-1").status, TaskStatus.CANCELED)
        self.assertEqual(store.get("task-1").metadata["completed_files"], 1)
        self.assertEqual(store.get("task-1").metadata["total_files"], 2)
        self.assertEqual(history.list(task_id="task-1")[-1].reason, "download_cancelled")

    def test_plan_task_appends_history_when_store_is_available(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        history_store = InMemoryHistoryStore()
        service = DownloadService(
            registry=registry,
            task_store=InMemoryTaskStore(),
            history_store=history_store,
            task_id_factory=lambda: "task-1",
        )

        result = asyncio.run(service.plan_task("example://gallery/welcome"))

        self.assertEqual(history_store.list(task_id="task-1"), result.events)

    def test_plan_task_persists_extra_record_metadata(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        service = DownloadService(
            registry=registry,
            task_store=store,
            task_id_factory=lambda: "task-1",
        )

        result = asyncio.run(
            service.plan_task(
                "example://gallery/welcome",
                record_metadata={
                    "live_http": True,
                    "transport": "FakeTransport",
                },
            )
        )

        self.assertEqual(result.record.metadata["live_http"], True)
        self.assertEqual(result.record.metadata["transport"], "FakeTransport")
        self.assertEqual(store.get("task-1"), result.record)

    def test_auto_delete_on_complete_deletes_task_record(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        history = InMemoryHistoryStore()
        completion = InMemoryCompletionStore()
        service = DownloadService(
            registry=registry,
            task_store=store,
            history_store=history,
            completion_store=completion,
            task_id_factory=lambda: "task-1",
            auto_delete_completed=True,
        )

        with TemporaryDirectory() as directory:
            result = asyncio.run(
                service.download_task(
                    "example://gallery/welcome",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(Path(directory)),
                )
            )

        # Record must be deleted from the store.
        self.assertIsNone(store.get("task-1"))
        # Result must still report COMPLETED status.
        self.assertEqual(result.record.status, TaskStatus.COMPLETED)
        # Written assets must be reported correctly.
        self.assertEqual(len(result.written_assets), 2)

    def test_auto_delete_on_complete_still_records_history(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())
        store = InMemoryTaskStore()
        history = InMemoryHistoryStore()
        completion = InMemoryCompletionStore()
        service = DownloadService(
            registry=registry,
            task_store=store,
            history_store=history,
            completion_store=completion,
            task_id_factory=lambda: "task-2",
            auto_delete_completed=True,
        )

        with TemporaryDirectory() as directory:
            asyncio.run(
                service.download_task(
                    "example://gallery/welcome",
                    fetcher=ExampleAssetFetcher(),
                    file_writer=AssetFileWriter(Path(directory)),
                )
            )

        # History must still record the completion.
        completed_events = [
            e for e in history.list(task_id="task-2") if e.reason == "task_completed"
        ]
        self.assertGreaterEqual(len(completed_events), 1)
        # Completion must be recorded so re-download is skipped.
        self.assertTrue(completion.is_completed("example", "example://gallery/welcome"))
