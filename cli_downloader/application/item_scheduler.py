"""Fair admission for executable Item work units.

The scheduler is deliberately narrower than the network request limiter.  It
admits one standalone Item or one Batch child at a time and leaves individual
file requests and qBittorrent's long-running transfer queue to their owners.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4


class SchedulerPolicy(StrEnum):
    ROUND_ROBIN = "round_robin"
    FIFO = "fifo"


@dataclass
class _Waiter:
    token: str
    root_id: str
    site_id: str
    sequence: int
    order: int
    event: asyncio.Event
    cancelled: bool = False


class ItemLease:
    """A granted Item slot.  Leases are idempotently releasable."""

    def __init__(self, scheduler: ItemScheduler, waiter: _Waiter) -> None:
        self._scheduler = scheduler
        self._waiter = waiter
        self._released = False

    @property
    def root_id(self) -> str:
        return self._waiter.root_id

    @property
    def site_id(self) -> str:
        return self._waiter.site_id

    async def release(self) -> None:
        if not self._released:
            self._released = True
            await self._scheduler.release(self._waiter)

    async def __aenter__(self) -> ItemLease:
        return self

    async def __aexit__(self, *_error: object) -> None:
        await self.release()


class ItemScheduler:
    """Admit Items under global/site limits with deterministic root fairness."""

    def __init__(
        self,
        max_concurrent_downloads: int = 2,
        max_concurrent_downloads_per_site: int | None = None,
        *,
        policy: SchedulerPolicy | str = SchedulerPolicy.ROUND_ROBIN,
    ) -> None:
        _positive_int(max_concurrent_downloads, "max_concurrent_downloads")
        per_site = (
            max_concurrent_downloads
            if max_concurrent_downloads_per_site is None
            else max_concurrent_downloads_per_site
        )
        _positive_int(per_site, "max_concurrent_downloads_per_site")
        try:
            selected_policy = SchedulerPolicy(policy)
        except ValueError as error:
            raise ValueError("policy must be round_robin or fifo") from error

        self.max_concurrent_downloads = max_concurrent_downloads
        self.max_concurrent_downloads_per_site = per_site
        self.policy = selected_policy
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: list[_Waiter] = []
        self._active: dict[str, _Waiter] = {}
        self._active_by_site: defaultdict[str, int] = defaultdict(int)
        self._active_by_root: defaultdict[str, int] = defaultdict(int)
        self._root_order: list[str] = []
        self._last_root: str | None = None
        self._next_order = 0
        self._suspended_roots: set[str] = set()

    async def acquire(
        self,
        *,
        root_id: str,
        site_id: str,
        sequence: int = 0,
    ) -> ItemLease:
        self._loop = asyncio.get_running_loop()
        if not root_id or not site_id:
            raise ValueError("root_id and site_id must be non-empty")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence must be a non-negative integer")

        waiter = _Waiter(
            token=uuid4().hex,
            root_id=root_id,
            site_id=site_id,
            sequence=sequence,
            order=0,
            event=asyncio.Event(),
        )
        async with self._lock:
            waiter.order = self._next_order
            self._next_order += 1
            self._pending.append(waiter)
            if root_id not in self._root_order:
                self._root_order.append(root_id)
            self._grant_locked()

        try:
            await waiter.event.wait()
        except asyncio.CancelledError:
            async with self._lock:
                if waiter.token in self._active:
                    self._release_locked(waiter)
                else:
                    self._remove_pending_locked(waiter)
                    self._grant_locked()
            raise
        if waiter.cancelled:
            raise asyncio.CancelledError
        return ItemLease(self, waiter)

    async def suspend_root(self, root_id: str) -> None:
        _require_root_id(root_id)
        async with self._lock:
            self._suspended_roots.add(root_id)
            self._grant_locked()

    async def resume_root(self, root_id: str) -> None:
        _require_root_id(root_id)
        async with self._lock:
            self._suspended_roots.discard(root_id)
            self._grant_locked()

    async def cancel_root(self, root_id: str) -> None:
        _require_root_id(root_id)
        async with self._lock:
            self._cancel_pending_root_locked(root_id)
            self._suspended_roots.discard(root_id)
            self._grant_locked()

    def suspend_root_sync(self, root_id: str) -> None:
        self._run_root_control_sync("suspend", root_id)

    def resume_root_sync(self, root_id: str) -> None:
        self._run_root_control_sync("resume", root_id)

    def cancel_root_sync(self, root_id: str) -> None:
        self._run_root_control_sync("cancel", root_id)

    @asynccontextmanager
    async def item(
        self,
        *,
        root_id: str,
        site_id: str,
        sequence: int = 0,
    ) -> AsyncIterator[ItemLease]:
        lease = await self.acquire(root_id=root_id, site_id=site_id, sequence=sequence)
        try:
            yield lease
        finally:
            await lease.release()

    async def release(self, waiter: _Waiter) -> None:
        async with self._lock:
            self._release_locked(waiter)

    async def waiting_snapshot(self) -> tuple[dict[str, object], ...]:
        """Return stable waiting rows for Tasks/diagnostic projections."""

        async with self._lock:
            return self._waiting_snapshot_locked()

    async def active_snapshot(self) -> tuple[dict[str, object], ...]:
        """Return the currently granted Item leases for bounded diagnostics."""

        async with self._lock:
            return self._active_snapshot_locked()

    def waiting_snapshot_sync(self) -> tuple[dict[str, object], ...]:
        """Return a best-effort synchronous view for the synchronous Tasks UI."""

        return self._waiting_snapshot_locked()

    def active_snapshot_sync(self) -> tuple[dict[str, object], ...]:
        """Return a best-effort synchronous active-lease view for diagnostics."""

        return self._active_snapshot_locked()

    def _active_snapshot_locked(self) -> tuple[dict[str, object], ...]:
        rows = [
            {
                "root_id": waiter.root_id,
                "site_id": waiter.site_id,
                "sequence": waiter.sequence,
            }
            for waiter in self._active.values()
        ]
        return tuple(sorted(rows, key=lambda row: (str(row["root_id"]), int(row["sequence"]))))

    def _waiting_snapshot_locked(self) -> tuple[dict[str, object], ...]:
        rows = []
        for position, waiter in enumerate(self._ordered_pending_locked(), start=1):
            rows.append(
                {
                    "root_id": waiter.root_id,
                    "site_id": waiter.site_id,
                    "sequence": waiter.sequence,
                    "position": position,
                    "reason": self._waiting_reason_locked(waiter),
                }
            )
        return tuple(rows)

    def _grant_locked(self) -> None:
        while len(self._active) < self.max_concurrent_downloads:
            eligible = self._eligible_locked()
            if not eligible:
                return
            waiter = self._choose_locked(eligible)
            self._pending.remove(waiter)
            self._active[waiter.token] = waiter
            self._active_by_site[waiter.site_id] += 1
            self._active_by_root[waiter.root_id] += 1
            waiter.event.set()

    def _eligible_locked(self) -> list[_Waiter]:
        heads: dict[str, _Waiter] = {}
        for waiter in self._pending:
            current = heads.get(waiter.root_id)
            if current is None or (waiter.sequence, waiter.order) < (
                current.sequence,
                current.order,
            ):
                heads[waiter.root_id] = waiter
        candidates = [
            waiter
            for waiter in heads.values()
            if waiter.root_id not in self._suspended_roots
            if self._active_by_site[waiter.site_id] < self.max_concurrent_downloads_per_site
        ]
        if not candidates:
            return []

        # Once another root is eligible, an already active root must yield its
        # next child. A sole root can still borrow every idle global slot.
        if self.policy is SchedulerPolicy.ROUND_ROBIN and len(candidates) > 1:
            candidates = [
                waiter for waiter in candidates if self._active_by_root[waiter.root_id] == 0
            ] or candidates
        return candidates

    def _choose_locked(self, candidates: list[_Waiter]) -> _Waiter:
        if self.policy is SchedulerPolicy.FIFO:
            return min(candidates, key=lambda waiter: waiter.order)
        roots = {waiter.root_id: waiter for waiter in candidates}
        order = self._root_order
        start = 0
        if self._last_root in order:
            start = (order.index(self._last_root) + 1) % len(order)
        for offset in range(len(order)):
            root = order[(start + offset) % len(order)]
            waiter = roots.get(root)
            if waiter is not None:
                self._last_root = root
                return waiter
        return min(candidates, key=lambda waiter: waiter.order)

    def _release_locked(self, waiter: _Waiter) -> None:
        active = self._active.pop(waiter.token, None)
        if active is None:
            return
        self._active_by_site[active.site_id] -= 1
        self._active_by_root[active.root_id] -= 1
        if self._active_by_site[active.site_id] <= 0:
            del self._active_by_site[active.site_id]
        if self._active_by_root[active.root_id] <= 0:
            del self._active_by_root[active.root_id]
        self._grant_locked()

    def _remove_pending_locked(self, waiter: _Waiter) -> None:
        try:
            self._pending.remove(waiter)
        except ValueError:
            pass

    def _ordered_pending_locked(self) -> list[_Waiter]:
        if self.policy is SchedulerPolicy.FIFO:
            return sorted(self._pending, key=lambda waiter: waiter.order)
        return sorted(
            self._pending,
            key=lambda waiter: (
                self._root_order.index(waiter.root_id),
                waiter.sequence,
                waiter.order,
            ),
        )

    def _waiting_reason_locked(self, waiter: _Waiter) -> str:
        if waiter.root_id in self._suspended_roots:
            return "root_suspended"
        if len(self._active) >= self.max_concurrent_downloads:
            return "global_slot"
        if self._active_by_site[waiter.site_id] >= self.max_concurrent_downloads_per_site:
            return "site_slot"
        head = min(
            (candidate for candidate in self._pending if candidate.root_id == waiter.root_id),
            key=lambda candidate: (candidate.sequence, candidate.order),
        )
        if head is not waiter:
            return "root_order"
        return "fair_turn"

    def _cancel_pending_root_locked(self, root_id: str) -> None:
        for waiter in tuple(self._pending):
            if waiter.root_id != root_id:
                continue
            waiter.cancelled = True
            self._pending.remove(waiter)
            waiter.event.set()

    def _run_root_control_sync(self, action: str, root_id: str) -> None:
        _require_root_id(root_id)
        operation = {
            "suspend": self.suspend_root,
            "resume": self.resume_root,
            "cancel": self.cancel_root,
        }[action]
        loop = self._loop
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if loop is None or not loop.is_running() or current_loop is loop:
            # Synchronous callers on the scheduler loop (Batch/UI tests) are
            # between awaits, so no other scheduler mutation can interleave.
            if action == "suspend":
                self._suspended_roots.add(root_id)
            elif action == "resume":
                self._suspended_roots.discard(root_id)
            else:
                self._cancel_pending_root_locked(root_id)
                self._suspended_roots.discard(root_id)
            self._grant_locked()
            return
        asyncio.run_coroutine_threadsafe(operation(root_id), loop).result()


def _positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_root_id(root_id: str) -> None:
    if not isinstance(root_id, str) or not root_id:
        raise ValueError("root_id must be a non-empty string")
