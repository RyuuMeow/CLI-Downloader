import asyncio
from io import BytesIO
from threading import Event
from unittest import IsolatedAsyncioTestCase

from cli_downloader.application.execution_control import OwnerShutdownRequested
from cli_downloader.application.site_request_coordinator import (
    RequestPolicy,
    SiteRequestCoordinator,
)
from cli_downloader.core.http import HttpRequest, HttpStreamInterrupted, interrupt_http_streams
from cli_downloader.integrations.http_client import HttpStatusError, ManagedHttpClient


class Response(BytesIO):
    def __init__(self, body=b"data", status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class ManagedHttpTest(IsolatedAsyncioTestCase):
    def coordinator(self, limit=1):
        return SiteRequestCoordinator(default_policy=RequestPolicy(limit, 0))

    async def test_open_response_holds_capacity_until_pause_really_closes_it(self):
        coordinator = self.coordinator()
        response = Response()
        client = ManagedHttpClient("site", coordinator, opener=lambda request: response)
        async with client.stream(HttpRequest("https://site.test")) as stream:
            self.assertEqual(await stream.read(1), b"d")
            other = asyncio.create_task(coordinator.acquire("site"))
            await asyncio.sleep(0)
            self.assertFalse(other.done())
            await interrupt_http_streams()
            self.assertTrue(response.closed)
            (await asyncio.wait_for(other, 2)).release()
            with self.assertRaises(HttpStreamInterrupted):
                await stream.read()
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 0)

    async def test_redirect_reenters_admission_and_does_not_forward_authorization(self):
        coordinator = self.coordinator()
        requests = []
        responses = [
            Response(status=302, headers={"Location": "https://cdn.test/file"}),
            Response(),
        ]

        def opener(request):
            requests.append(request)
            if len(requests) == 2:
                self.assertTrue(responses[0].closed)
            return responses[len(requests) - 1]

        client = ManagedHttpClient("site", coordinator, opener=opener)
        await client.request(
            HttpRequest(
                "https://site.test",
                headers={"Authorization": "secret", "Cookie": "secret", "Accept": "*/*"},
            )
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(dict(requests[1].headers), {"Accept": "*/*"})
        self.assertEqual(len(coordinator.snapshot()), 1)
        self.assertTrue(all(response.closed for response in responses))

    async def test_five_internal_requests_are_five_admissions_including_fanout(self):
        coordinator = self.coordinator(2)
        requests = []

        def opener(request):
            requests.append(request)
            self.assertLessEqual(coordinator.snapshot()[0]["in_flight"], 2)
            return Response()

        client = ManagedHttpClient("site", coordinator, opener=opener)
        await asyncio.gather(
            *(client.request(HttpRequest(f"https://site.test/{i}")) for i in range(5))
        )
        self.assertEqual(len(requests), 5)
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 0)

    async def test_status_retry_closes_response_before_next_attempt(self):
        coordinator = self.coordinator()
        responses = [Response(status=429, headers={"Retry-After": "0"}), Response()]
        count = 0

        def opener(request):
            nonlocal count
            if count:
                self.assertTrue(responses[count - 1].closed)
            response = responses[count]
            count += 1
            return response

        result = await ManagedHttpClient("site", coordinator, opener=opener).request(
            HttpRequest("https://site.test")
        )
        self.assertEqual(result.body, b"data")
        self.assertEqual(count, 2)

    async def test_unsafe_post_is_not_replayed_but_still_cools_down_site(self):
        coordinator = self.coordinator()
        requests = []

        def opener(request):
            requests.append(request)
            return Response(status=429, headers={"Retry-After": "30"})

        with self.assertRaises(HttpStatusError):
            await ManagedHttpClient("site", coordinator, opener=opener).request(
                HttpRequest("https://site.test", method="POST", body=b"value")
            )
        self.assertEqual(len(requests), 1)
        self.assertGreater(coordinator.snapshot()[0]["cooldown_seconds"], 29)
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 0)

    async def test_cancellation_waits_for_blocking_read_before_returning_capacity(self):
        coordinator = self.coordinator()
        reading, release = Event(), Event()

        class SlowResponse(Response):
            def read(self, size=-1):
                reading.set()
                if not release.wait(3):
                    raise TimeoutError("test read timeout")
                return super().read(size)

        response = SlowResponse()
        client = ManagedHttpClient("site", coordinator, opener=lambda request: response)
        task = asyncio.create_task(client.request(HttpRequest("https://site.test")))
        self.assertTrue(await asyncio.to_thread(reading.wait, 2))
        task.cancel()
        await asyncio.sleep(0)
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 1)
        self.assertFalse(response.closed)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(response.closed)
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 0)

    async def test_cancellation_during_open_closes_late_response(self):
        coordinator = self.coordinator()
        opening, release = Event(), Event()
        response = Response()

        def opener(request):
            opening.set()
            release.wait(3)
            return response

        task = asyncio.create_task(
            ManagedHttpClient("site", coordinator, opener=opener).request(
                HttpRequest("https://site.test")
            )
        )
        self.assertTrue(await asyncio.to_thread(opening.wait, 2))
        task.cancel()
        await asyncio.sleep(0)
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(response.closed)
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 0)

    async def test_stop_rejects_waiters_and_new_transports_but_retains_active_response(self):
        coordinator = self.coordinator()
        response = Response()
        client = ManagedHttpClient("site", coordinator, opener=lambda _: response)
        async with client.stream(HttpRequest("https://site.test")):
            waiter = asyncio.create_task(client.request(HttpRequest("https://site.test/next")))
            await asyncio.sleep(0)
            coordinator.stop()
            with self.assertRaisesRegex(OwnerShutdownRequested, "stopping"):
                await waiter
            self.assertEqual(coordinator.active_transport_count, 1)
        self.assertTrue(response.closed)
        self.assertEqual(coordinator.active_transport_count, 0)

    async def test_close_failure_does_not_claim_capacity_was_returned(self):
        coordinator = self.coordinator()

        class BrokenClose(Response):
            def close(self):
                raise OSError("close failed")

        response = BrokenClose()
        client = ManagedHttpClient("site", coordinator, opener=lambda _: response)
        with self.assertRaisesRegex(OSError, "close failed"):
            async with client.stream(HttpRequest("https://site.test")):
                pass
        self.assertEqual(coordinator.active_transport_count, 1)
        BytesIO.close(response)

    async def test_concurrent_profiles_keep_request_credentials_separate(self):
        coordinator = self.coordinator(2)
        seen = {}

        def opener(request):
            seen[request.url] = request.headers["Authorization"]
            return Response()

        client = ManagedHttpClient("site", coordinator, opener=opener)
        await asyncio.gather(
            *(
                client.request(
                    HttpRequest(f"https://site.test/{name}", headers={"Authorization": name})
                )
                for name in ("profile-a", "profile-b")
            )
        )
        self.assertEqual(set(seen.values()), {"profile-a", "profile-b"})

    async def test_new_cooldown_after_grant_requeues_without_opening_transport(self):
        from cli_downloader.application.site_request_coordinator import TransportDeferred

        coordinator = self.coordinator()
        original = coordinator.start_transport
        calls = []

        def start(permit, cancelled):
            if not calls:
                calls.append("defer")
                coordinator.defer("site", 0.03, status=429)
                raise TransportDeferred
            original(permit, cancelled)

        coordinator.start_transport = start
        client = ManagedHttpClient("site", coordinator, opener=lambda _: Response())
        result = await asyncio.wait_for(client.request(HttpRequest("https://site.test")), 2)
        self.assertEqual(result.body, b"data")
        self.assertEqual(coordinator.active_transport_count, 0)
