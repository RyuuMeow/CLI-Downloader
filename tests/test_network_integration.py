import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
from unittest import IsolatedAsyncioTestCase

from cli_downloader.application.batch_service import RuntimePackageResolver
from cli_downloader.application.download_limiter import GalleryDownloadLimiter, RequestPolicy
from cli_downloader.application.download_service import DownloadService, WriteAssetsStage
from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.application.site_request_coordinator import RequestContext, request_context
from cli_downloader.core.fetch import UrlAssetFetcher
from cli_downloader.core.http import HttpRequest, PluginServices
from cli_downloader.core.models import (
    Asset,
    AssetKind,
    AssetList,
    DownloadStrategy,
    DownloadStrategyPlan,
    ResolvedItem,
)
from cli_downloader.core.pipeline import PipelineContext
from cli_downloader.integrations.http_client import ManagedHttpClient
from cli_downloader.plugins.runtime import RuntimePlugin, build_application_runtime
from cli_downloader.storage.file_writer import AssetFileWriter
from cli_downloader.storage.task_store import (
    InMemoryTaskStore,
    TaskRecord,
    TaskStatus,
    TaskDesiredControl,
)
from tests.example_site import ExampleAdapter
from tests.test_batch_pipeline import make_entry


class Response(BytesIO):
    status = 200
    headers = {}


class ManagedAdapter(ExampleAdapter):
    managed_http_version = 1

    def bind_services(self, services):
        self.http = services.http

    async def resolve(self, request):
        await asyncio.gather(
            *(self.http.request(HttpRequest(f"https://example.test/resolve/{i}")) for i in range(3))
        )
        return await super().resolve(request)

    async def choose_strategy(self, item):
        await self.http.request(HttpRequest("https://example.test/strategy"))
        return await super().choose_strategy(item)

    async def execute(self, request):
        await self.http.request(HttpRequest("https://example.test/list"))
        return await super().execute(request)


class NetworkIntegrationTest(IsolatedAsyncioTestCase):
    async def test_batch_standalone_listing_and_download_share_actual_request_budget(self):
        limiter = GalleryDownloadLimiter(4, default_request_policy=RequestPolicy(2, 0.01))
        adapter = ManagedAdapter()
        fetcher = UrlAssetFetcher()
        runtime = build_application_runtime(
            plugin_roots=(),
            builtin_plugins=(RuntimePlugin("example", adapter, adapter.get_manifest(), fetcher),),
        )
        runtime.bind_http_services(limiter)
        starts = []

        def opener(request):
            starts.append(monotonic())
            self.assertLessEqual(limiter.requests.snapshot()[0]["in_flight"], 2)
            return Response(b"data")

        client = ManagedHttpClient("example", limiter.requests, opener=opener)
        adapter.bind_services(PluginServices(client))
        fetcher.bind_services(PluginServices(client))
        resolver = RuntimePackageResolver(runtime, request_limiter=limiter)
        entry = replace(make_entry("batch", "entry"), plugin_id="example")
        service = DownloadService(
            registry=runtime.site_registry, task_store=InMemoryTaskStore(), request_limiter=limiter
        )
        dispatcher = OperationDispatcher(runtime)
        await asyncio.wait_for(
            asyncio.gather(
                resolver.resolve(entry, Event(), lambda *_: None),
                service.plan_task("https://example.test/gallery/a", task_id="single"),
                dispatcher.dispatch("https://example.test/list", request_id="list"),
                fetcher.fetch(Asset("https://example.test/file", AssetKind.FILE, "file")),
            ),
            5,
        )
        self.assertEqual(len(starts), 10)
        self.assertTrue(all(b - a >= 0.007 for a, b in zip(starts, starts[1:])), starts)
        self.assertEqual(limiter.requests.snapshot()[0]["in_flight"], 0)

    async def test_site_cooldown_returns_item_slot_to_other_site(self):
        limiter = GalleryDownloadLimiter(1, default_request_policy=RequestPolicy(1, 0))
        item = [await limiter.acquire_item(root_id="A", site_id="site")]
        released = asyncio.Event()

        async def release():
            if item[0] is not None:
                await item[0].release()
                item[0] = None
                released.set()

        async def resume():
            item[0] = await limiter.acquire_item(root_id="A", site_id="site")

        async def checkpoint():
            return

        async def waiting():
            with request_context(
                RequestContext(
                    "A", "download", checkpoint=checkpoint, release_item=release, resume_item=resume
                )
            ):
                return await limiter.requests.acquire("site")

        limiter.requests.defer("site", 60, status=429)
        task = asyncio.create_task(waiting())
        try:
            await asyncio.wait_for(released.wait(), 2)
            other = await asyncio.wait_for(limiter.acquire_item(root_id="B", site_id="other"), 2)
            await other.release()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_managed_stream_pause_closes_and_resume_restarts_without_duplicate_bytes(self):
        limiter = GalleryDownloadLimiter(1, default_request_policy=RequestPolicy(1, 0))
        store = InMemoryTaskStore()
        store.upsert(TaskRecord("item", TaskStatus.RUNNING, site_id="example"))
        responses = []

        class PausingResponse(Response):
            def read(self, size=-1):
                chunk = super().read(2)
                if len(responses) == 1 and self.tell() == 2:
                    record = store.get("item")
                    store.upsert(record.transition(desired_control=TaskDesiredControl.PAUSE))
                return chunk

        def opener(request):
            response = PausingResponse(b"abcdef")
            responses.append(response)
            return response

        fetcher = UrlAssetFetcher()
        fetcher.bind_services(
            PluginServices(ManagedHttpClient("example", limiter.requests, opener=opener))
        )
        with TemporaryDirectory() as directory:
            writer = AssetFileWriter(Path(directory))
            stage = WriteAssetsStage(fetcher, writer, task_store=store, download_limiter=limiter)
            asset = Asset("https://example.test/file", AssetKind.FILE, "file.txt")
            plan = DownloadStrategyPlan(DownloadStrategy.DIRECT, "test", AssetList((asset,)))
            task = asyncio.create_task(
                stage.run(
                    PipelineContext(
                        "item",
                        {
                            "item": ResolvedItem("example", "https://example.test/file", "item"),
                            "plan": plan,
                        },
                    )
                )
            )
            try:

                async def paused():
                    while store.get("item").status is not TaskStatus.PAUSED:
                        await asyncio.sleep(0.005)

                await asyncio.wait_for(paused(), 2)
                self.assertTrue(responses[0].closed)
                self.assertEqual(limiter.requests.snapshot()[0]["in_flight"], 0)
                # A different site can run while the first response is closed.
                other = await asyncio.wait_for(
                    limiter.acquire_item(root_id="other", site_id="other"), 2
                )
                await other.release()
                store.upsert(
                    store.get("item").transition(
                        status=TaskStatus.PLANNED, desired_control=TaskDesiredControl.RUN
                    )
                )
                await asyncio.wait_for(task, 3)
                target, _ = writer.paths(asset, task_id="item")
                self.assertEqual(target.read_bytes(), b"abcdef")
                self.assertEqual(len(responses), 2)
                self.assertTrue(all(response.closed for response in responses))
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_fanout_pause_during_cooldown_is_acknowledged_only_after_all_reads_close(self):
        from cli_downloader.application.plugin_network import invoke_plugin_network

        limiter = GalleryDownloadLimiter(1, default_request_policy=RequestPolicy(1, 0))
        limiter.requests.defer("site", 60, status=429)
        paused, resume = asyncio.Event(), asyncio.Event()
        calls = []

        async def checkpoint():
            if limiter.requests.snapshot()[0]["waiting"]:
                paused.set()
                await resume.wait()

        client = ManagedHttpClient("site", limiter.requests, opener=lambda _: calls.append(1))

        async def operation():
            return await asyncio.gather(
                *(client.request(HttpRequest("https://site.test")) for _ in range(3))
            )

        task = asyncio.create_task(
            invoke_plugin_network(
                ManagedAdapter(),
                limiter,
                "site",
                operation,
                context=RequestContext("root", "prepare", checkpoint=checkpoint),
            )
        )
        try:
            await asyncio.wait_for(paused.wait(), 2)
            self.assertEqual(limiter.requests.active_transport_count, 0)
            self.assertEqual(calls, [])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(limiter.requests.snapshot()[0]["waiting"], ())

    async def test_managed_cap_one_and_legacy_nested_operations_do_not_deadlock(self):
        from cli_downloader.application.plugin_network import invoke_plugin_network

        limiter = GalleryDownloadLimiter(1, default_request_policy=RequestPolicy(1, 0))
        client = ManagedHttpClient("site", limiter.requests, opener=lambda _: Response(b"ok"))
        result = await asyncio.wait_for(
            invoke_plugin_network(
                ManagedAdapter(),
                limiter,
                "site",
                lambda: client.request(HttpRequest("https://site.test")),
                context=RequestContext("root"),
            ),
            2,
        )
        self.assertEqual(result.body, b"ok")

        async def inner():
            return "ok"

        self.assertEqual(
            await asyncio.wait_for(
                limiter.request("site", lambda: limiter.request("site", inner)), 2
            ),
            "ok",
        )

    async def test_projection_reads_network_once_without_leaking_wait_to_siblings(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from cli_downloader.application.task_hierarchy_service import TaskHierarchyService

        snapshot = Mock(
            return_value=(
                {
                    "in_flight": 1,
                    "limit": 1,
                    "cooldown_until": None,
                    "waiting": (
                        {
                            "root_id": "root",
                            "task_id": "second",
                            "phase": "prepare",
                            "reason": "request_capacity",
                        },
                    ),
                },
            )
        )
        service = TaskHierarchyService.__new__(TaskHierarchyService)
        service._scheduler = SimpleNamespace(
            active_snapshot_sync=lambda: ({"root_id": "root", "site_id": "site", "sequence": 0},),
            waiting_snapshot_sync=lambda: (),
            requests=SimpleNamespace(snapshot=snapshot),
        )
        projection = service._scheduler_projection()
        snapshot.assert_called_once()
        self.assertEqual(projection[("root", None)]["network_wait_reason"], "request_capacity")
        self.assertNotIn("network_wait_reason", projection[("root", 0)])
        self.assertEqual(projection[("second", "network")]["network_phase"], "prepare")

    async def test_batch_cancel_closes_managed_response_before_more_bytes_are_written(self):
        from cli_downloader.application.batch_service import SystemBatchDownloader
        from cli_downloader.core.batch import AssetRecord, DownloadChild
        from tests.test_batch_pipeline import make_package

        cancellation = Event()
        limiter = GalleryDownloadLimiter(1, default_request_policy=RequestPolicy(1, 0))

        class CancelResponse(Response):
            def read(self, size=-1):
                cancellation.set()
                return super().read(2)

        response = CancelResponse(b"abcdef")
        fetcher = UrlAssetFetcher()
        fetcher.bind_services(
            PluginServices(
                ManagedHttpClient(
                    "example",
                    limiter.requests,
                    opener=lambda _: response,
                )
            )
        )
        package = make_package(make_entry("root", "entry"))
        child = DownloadChild("child", "root", "entry", package)
        with TemporaryDirectory() as directory:
            downloader = SystemBatchDownloader(
                file_writer=AssetFileWriter(Path(directory)),
                fetchers={"example": fetcher},
                request_limiter=limiter,
            )
            with self.assertRaises(asyncio.CancelledError):
                await downloader.execute(
                    child,
                    (AssetRecord(package.assets[0]),),
                    cancellation,
                    lambda *_args, **_kwargs: None,
                )
            self.assertTrue(response.closed)
            self.assertEqual(limiter.requests.active_transport_count, 0)
            self.assertFalse(tuple(Path(directory).rglob("001.jpg")))
