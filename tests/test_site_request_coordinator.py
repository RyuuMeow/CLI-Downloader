import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.message import Message
from email.utils import format_datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import IsolatedAsyncioTestCase, TestCase
from urllib.error import HTTPError

from cli_downloader.application.site_request_coordinator import (
    RequestContext,
    RequestPolicy,
    SiteRequestCoordinator,
    retry_delay,
)
from cli_downloader.storage.site_cooldown_store import SiteCooldownStore
from cli_downloader.storage.state_database import StateDatabase


class SiteRequestsTest(IsolatedAsyncioTestCase):
    async def test_all_phases_share_capacity_and_cancelled_waiters_leave_no_permits(self):
        coordinator = SiteRequestCoordinator(default_policy=RequestPolicy(2, 0))
        first = await coordinator.acquire("site", RequestContext("A", "prepare"))
        second = await coordinator.acquire("site", RequestContext("B", "download"))
        waiting = asyncio.create_task(coordinator.acquire("site", RequestContext("C", "list")))
        await asyncio.sleep(0)
        self.assertEqual(coordinator.snapshot()[0]["waiting_by_phase"], {"list": 1})
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        first.release()
        second.release()
        self.assertEqual(coordinator.snapshot()[0]["in_flight"], 0)
        self.assertEqual(coordinator.snapshot()[0]["waiting_by_phase"], {})

    async def test_storage_failure_keeps_in_memory_cooldown(self):
        class Store:
            def load(self):
                return {}

            def extend(self, *_args):
                raise OSError("storage unavailable")

        coordinator = SiteRequestCoordinator(cooldown_store=Store())
        with self.assertRaises(OSError):
            coordinator.defer("site", 100, status=429)
        self.assertGreater(coordinator.snapshot()[0]["cooldown_seconds"], 99)

    async def test_roots_rotate_and_fifo_is_preserved_within_root(self):
        coordinator = SiteRequestCoordinator(default_policy=RequestPolicy(1, 0))
        first = await coordinator.acquire("site", RequestContext("A"))
        order = []

        async def enter(root, sequence):
            async with await coordinator.acquire("site", RequestContext(root)):
                order.append(sequence)

        tasks = [
            asyncio.create_task(enter(root, name))
            for root, name in [("A", "A1"), ("A", "A2"), ("B", "B1"), ("B", "B2")]
        ]
        await asyncio.sleep(0)
        first.release()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        self.assertEqual(order, ["B1", "A1", "B2", "A2"])

    async def test_spacing_is_between_starts_and_idle_does_not_accumulate_credits(self):
        now = [100.0]
        coordinator = SiteRequestCoordinator(
            default_policy=RequestPolicy(2, 1), clock=lambda: now[0]
        )
        first = await coordinator.acquire("site")
        now[0] = 200.0
        second = await coordinator.acquire("site")
        first.release()
        third = asyncio.create_task(coordinator.acquire("site"))
        await asyncio.sleep(0)
        self.assertEqual(coordinator.snapshot()[0]["waiting"][0]["reason"], "request_interval")
        self.assertFalse(third.done())
        now[0] = 201.0
        second.release()
        (await asyncio.wait_for(third, 2)).release()

    async def test_cooldown_blocks_every_phase_but_not_another_site(self):
        now = [100.0]
        coordinator = SiteRequestCoordinator(
            default_policy=RequestPolicy(2, 0), clock=lambda: now[0], wall_clock=lambda: now[0]
        )
        coordinator.defer("site", 30, status=429)
        coordinator.defer("site", 1, status=429)
        waiting = asyncio.create_task(coordinator.acquire("site", RequestContext("B", "prepare")))
        await asyncio.sleep(0)
        async with await coordinator.acquire("other"):
            snapshot = next(s for s in coordinator.snapshot() if s["site_id"] == "site")
            self.assertEqual(snapshot["cooldown_seconds"], 30)
            self.assertEqual(snapshot["waiting"][0]["reason"], "site_cooldown")
        now[0] = 130.0
        coordinator.defer("site", 0)  # Also wakes waiting loops after the injected clock advances.
        (await asyncio.wait_for(waiting, 2)).release()


class SiteRequestsPersistenceTest(TestCase):
    def test_owner_restart_restores_server_cooldown_without_restoring_active_leases(self):
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            store = SiteCooldownStore(database)
            old = SiteRequestCoordinator(
                cooldown_store=store, wall_clock=lambda: 100, clock=lambda: 10
            )
            old.defer("site", 90, status=429)
            new = SiteRequestCoordinator(
                cooldown_store=store, wall_clock=lambda: 120, clock=lambda: 2
            )
            snapshot = new.snapshot()[0]
            self.assertEqual(snapshot["cooldown_seconds"], 70)
            self.assertEqual(snapshot["in_flight"], 0)

    def test_http_date_and_wrapped_errors_preserve_server_delay_beyond_local_cap(self):
        headers = Message()
        headers["Retry-After"] = format_datetime(datetime.fromtimestamp(200, UTC), usegmt=True)
        error = HTTPError("https://site.test/", 429, "limited", headers, None)
        wrapper = RuntimeError("stream failed")
        wrapper.__cause__ = error
        self.assertEqual(
            retry_delay(wrapper, 1, RequestPolicy(max_backoff_seconds=1), wall_clock=lambda: 100),
            100,
        )
        headers["Retry-After"] = "bad"

    def test_waiter_wakes_on_a_different_event_loop(self):
        coordinator = SiteRequestCoordinator(default_policy=RequestPolicy(1, 0))
        holding = Event()
        release = Event()
        started = Event()

        async def first():
            async with await coordinator.acquire("site"):
                holding.set()
                await asyncio.to_thread(release.wait, 2)

        async def second():
            started.set()
            async with await coordinator.acquire("site"):
                return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            one = executor.submit(asyncio.run, first(), debug=True)
            self.assertTrue(holding.wait(2))
            two = executor.submit(asyncio.run, second(), debug=True)
            self.assertTrue(started.wait(2))
            release.set()
            self.assertTrue(two.result(3))
            one.result(3)
