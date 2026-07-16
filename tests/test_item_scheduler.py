import asyncio
from unittest import IsolatedAsyncioTestCase

from cli_downloader.application.item_scheduler import ItemScheduler, SchedulerPolicy


class ItemSchedulerTest(IsolatedAsyncioTestCase):
    async def test_round_robin_rotates_roots_and_preserves_batch_order(self) -> None:
        scheduler = ItemScheduler(1, 1, policy=SchedulerPolicy.ROUND_ROBIN)
        first = await scheduler.acquire(root_id="batch-a", site_id="pixiv", sequence=0)
        order: list[str] = []

        async def wait_for(root_id: str, sequence: int) -> None:
            lease = await scheduler.acquire(root_id=root_id, site_id="pixiv", sequence=sequence)
            order.append(f"{root_id}:{sequence}")
            await lease.release()

        a2 = asyncio.create_task(wait_for("batch-a", 1))
        b1 = asyncio.create_task(wait_for("batch-b", 0))
        await asyncio.sleep(0)
        await first.release()
        await asyncio.gather(a2, b1)

        self.assertEqual(order, ["batch-b:0", "batch-a:1"])

    async def test_fifo_uses_enqueue_order_when_roots_are_eligible(self) -> None:
        scheduler = ItemScheduler(1, 2, policy=SchedulerPolicy.FIFO)
        first = await scheduler.acquire(root_id="batch-a", site_id="pixiv", sequence=0)
        order: list[str] = []

        async def wait_for(root_id: str) -> None:
            lease = await scheduler.acquire(root_id=root_id, site_id="pixiv")
            order.append(root_id)
            await lease.release()

        b = asyncio.create_task(wait_for("batch-b"))
        a = asyncio.create_task(wait_for("batch-a"))
        await asyncio.sleep(0)
        await first.release()
        await asyncio.gather(b, a)

        self.assertEqual(order, ["batch-b", "batch-a"])

    async def test_fifo_keeps_oldest_root_when_it_has_next_child_queued(self) -> None:
        scheduler = ItemScheduler(1, 1, policy=SchedulerPolicy.FIFO)
        first = await scheduler.acquire(root_id="batch-a", site_id="pixiv", sequence=0)
        order: list[str] = []

        async def wait_for(root_id: str, sequence: int) -> None:
            lease = await scheduler.acquire(
                root_id=root_id,
                site_id="pixiv",
                sequence=sequence,
            )
            order.append(f"{root_id}:{sequence}")
            await lease.release()

        a2 = asyncio.create_task(wait_for("batch-a", 1))
        b1 = asyncio.create_task(wait_for("batch-b", 0))
        await asyncio.sleep(0)
        await first.release()
        await asyncio.gather(a2, b1)

        self.assertEqual(order, ["batch-a:1", "batch-b:0"])

    async def test_site_limit_does_not_block_another_site(self) -> None:
        scheduler = ItemScheduler(3, 1, policy=SchedulerPolicy.FIFO)
        first = await scheduler.acquire(root_id="pixiv-1", site_id="pixiv")
        second = await scheduler.acquire(root_id="eh-1", site_id="example")
        blocked = asyncio.create_task(scheduler.acquire(root_id="pixiv-2", site_id="pixiv"))
        await asyncio.sleep(0)
        self.assertFalse(blocked.done())
        snapshot = await scheduler.waiting_snapshot()
        self.assertEqual(snapshot[0]["reason"], "site_slot")
        await first.release()
        third = await blocked
        await third.release()
        await second.release()

    async def test_cancelled_waiter_is_removed_without_consuming_capacity(self) -> None:
        scheduler = ItemScheduler(1, 1)
        first = await scheduler.acquire(root_id="a", site_id="pixiv")
        waiting = asyncio.create_task(scheduler.acquire(root_id="b", site_id="pixiv"))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(await scheduler.waiting_snapshot(), ())
        await first.release()

    async def test_waiting_snapshot_exposes_site_and_position(self) -> None:
        scheduler = ItemScheduler(1, 1)
        first = await scheduler.acquire(root_id="a", site_id="pixiv")
        waiting = asyncio.create_task(scheduler.acquire(root_id="b", site_id="pixiv"))
        await asyncio.sleep(0)
        snapshot = await scheduler.waiting_snapshot()
        self.assertEqual(snapshot[0]["root_id"], "b")
        self.assertEqual(snapshot[0]["site_id"], "pixiv")
        self.assertEqual(snapshot[0]["position"], 1)
        self.assertEqual(snapshot[0]["reason"], "global_slot")
        await first.release()
        await (await waiting).release()

    async def test_active_snapshot_exposes_granted_root_and_site(self) -> None:
        scheduler = ItemScheduler(2, 1)
        first = await scheduler.acquire(root_id="batch-a", site_id="pixiv", sequence=3)
        second = await scheduler.acquire(root_id="item-b", site_id="ehentai", sequence=0)

        self.assertEqual(
            await scheduler.active_snapshot(),
            (
                {"root_id": "batch-a", "site_id": "pixiv", "sequence": 3},
                {"root_id": "item-b", "site_id": "ehentai", "sequence": 0},
            ),
        )

        await first.release()
        await second.release()
        self.assertEqual(await scheduler.active_snapshot(), ())

    async def test_suspended_root_cannot_receive_a_pending_grant_until_resumed(self) -> None:
        scheduler = ItemScheduler(1, 1)
        first = await scheduler.acquire(root_id="batch-a", site_id="pixiv")
        waiting = asyncio.create_task(
            scheduler.acquire(root_id="batch-b", site_id="pixiv")
        )
        await asyncio.sleep(0)

        await scheduler.suspend_root("batch-b")
        await first.release()
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        snapshot = await scheduler.waiting_snapshot()
        self.assertEqual(snapshot[0]["reason"], "root_suspended")

        await scheduler.resume_root("batch-b")
        await (await waiting).release()

    async def test_cancelled_root_removes_pending_waiters_and_unblocks_acquire(self) -> None:
        scheduler = ItemScheduler(1, 1)
        first = await scheduler.acquire(root_id="batch-a", site_id="pixiv")
        waiting = asyncio.create_task(
            scheduler.acquire(root_id="batch-b", site_id="pixiv")
        )
        await asyncio.sleep(0)

        await scheduler.cancel_root("batch-b")
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(await scheduler.waiting_snapshot(), ())
        await first.release()
