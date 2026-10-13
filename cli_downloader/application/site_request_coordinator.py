"""Owner-wide HTTP admission, separate from Item download scheduling.

No asyncio primitive is shared between loops: each waiter owns its own Event.
Capacity, fairness and start spacing are decided together under one short lock.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Event, RLock
from time import monotonic, time
from typing import Protocol
from uuid import uuid4

from cli_downloader.application.execution_control import OwnerShutdownRequested


@dataclass(frozen=True)
class RequestPolicy:
    max_concurrency: int = 2
    min_interval_seconds: float = 0.5
    retryable_status_codes: tuple[int, ...] = (429,)
    max_attempts: int = 5
    max_backoff_seconds: float = 300.0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.max_concurrency, self.max_attempts)
        ):
            raise ValueError("request concurrency and attempts must be positive integers")
        if (
            not math.isfinite(self.min_interval_seconds)
            or not math.isfinite(self.max_backoff_seconds)
            or self.min_interval_seconds < 0
            or self.max_backoff_seconds <= 0
        ):
            raise ValueError("request timing values are invalid")


@dataclass
class RequestGate:
    blocked: bool = False


@dataclass(frozen=True)
class RequestContext:
    root_id: str
    phase: str = "operation"
    task_id: str | None = None
    profile: str | None = None
    checkpoint: Callable[[], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )
    release_item: Callable[[], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )
    resume_item: Callable[[], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )
    gate: RequestGate | None = field(default=None, repr=False, compare=False)
    owner_task: object | None = field(default=None, repr=False, compare=False)


_context: ContextVar[RequestContext | None] = ContextVar("site_request_context", default=None)


@contextmanager
def request_context(context: RequestContext):
    context = replace(context, owner_task=asyncio.current_task())
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def current_request_context() -> RequestContext:
    return _context.get() or RequestContext(root_id=uuid4().hex)


class CooldownStore(Protocol):
    def load(self) -> Mapping[str, tuple[float, int | None]]: ...

    def extend(self, site_id: str, retry_at: float, status: int | None) -> None: ...


@dataclass(eq=False)
class _Waiter:
    context: RequestContext
    loop: asyncio.AbstractEventLoop
    event: asyncio.Event
    eligible: bool = True

    def wake(self) -> None:
        if not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.event.set)
            except RuntimeError:
                # A canceled caller's loop can close before its queued wake is delivered.
                if not self.loop.is_closed():
                    raise


@dataclass
class _Site:
    policy: RequestPolicy
    active: dict[str, RequestContext] = field(default_factory=dict)
    waiting: list[_Waiter] = field(default_factory=list)
    last_root: str | None = None
    next_start: float = 0.0
    last_transport_start: float = float("-inf")
    cooldown_until: float = 0.0
    retry_at: float = 0.0
    status: int | None = None

    def first(self) -> _Waiter | None:
        waiting = [waiter for waiter in self.waiting if waiter.eligible]
        if not waiting:
            return None
        roots = list(dict.fromkeys(waiter.context.root_id for waiter in waiting))
        if self.last_root in roots:
            root = roots[(roots.index(self.last_root) + 1) % len(roots)]
        else:
            root = roots[0]
        return next(waiter for waiter in waiting if waiter.context.root_id == root)

    def wake(self) -> None:
        for waiter in self.waiting:
            waiter.wake()


class TransportDeferred(Exception):
    """A new cooldown requires returning to asynchronous admission."""


class RequestPermit:
    def __init__(self, owner: SiteRequestCoordinator, site_id: str, identity: str) -> None:
        self._owner = owner
        self.site_id = site_id
        self.identity = identity

    def release(self) -> None:
        self._owner.release(self.site_id, self.identity)

    async def __aenter__(self) -> RequestPermit:
        return self

    async def __aexit__(self, *_error: object) -> None:
        self.release()


class SiteRequestCoordinator:
    def __init__(
        self,
        *,
        policies: Mapping[str, RequestPolicy] | None = None,
        default_policy: RequestPolicy | None = None,
        cooldown_store: CooldownStore | None = None,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], float] = time,
    ) -> None:
        self._policies = dict(policies or {})
        self._default = default_policy or RequestPolicy()
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = RLock()
        self._sites: dict[str, _Site] = {}
        self._store: CooldownStore | None = None
        self._stopped = False
        self._transports: set[str] = set()
        if cooldown_store is not None:
            self.bind_cooldown_store(cooldown_store)

    def bind_cooldown_store(self, store: CooldownStore) -> None:
        with self._lock:
            if self._store is not None:
                return
            records = store.load()
            for site_id, (retry_at, status) in records.items():
                if not math.isfinite(retry_at) or retry_at < 0:
                    raise ValueError("invalid persisted site cooldown timestamp")
                site = self._site(site_id)
                site.retry_at = max(site.retry_at, retry_at)
                site.cooldown_until = max(
                    site.cooldown_until, self._clock() + max(0, retry_at - self._wall_clock())
                )
                site.status = status
            self._store = store

    def policy(self, site_id: str) -> RequestPolicy:
        return self._policies.get(site_id, self._default)

    def _site(self, site_id: str) -> _Site:
        return self._sites.setdefault(site_id, _Site(self.policy(site_id)))

    async def acquire(self, site_id: str, context: RequestContext | None = None) -> RequestPermit:
        waiter = _Waiter(
            context or current_request_context(), asyncio.get_running_loop(), asyncio.Event()
        )
        with self._lock:
            site = self._site(site_id)
            self._check_running()
            site.waiting.append(waiter)
        released_item = False
        control = waiter.context if waiter.context.owner_task is asyncio.current_task() else None
        try:
            while True:
                waiter.event.clear()
                if control is not None and control.checkpoint is not None:
                    with self._lock:
                        waiter.eligible = False
                        cooling = site.cooldown_until > self._clock()
                        if len(site.active) < site.policy.max_concurrency and self._clock() >= max(
                            site.next_start, site.cooldown_until
                        ):
                            next_waiter = site.first()
                            if next_waiter is not None:
                                next_waiter.wake()
                    if cooling and not released_item and control.release_item is not None:
                        await control.release_item()
                        released_item = True
                    await control.checkpoint()
                    if not cooling and released_item and control.resume_item is not None:
                        await control.resume_item()
                        released_item = False
                    with self._lock:
                        waiter.eligible = True
                with self._lock:
                    self._check_running()
                    gate = waiter.context.gate
                    waiter.eligible = gate is None or not gate.blocked
                    now = self._clock()
                    ready_at = max(site.next_start, site.cooldown_until)
                    if (
                        site.first() is waiter
                        and len(site.active) < site.policy.max_concurrency
                        and now >= ready_at
                        and not released_item
                    ):
                        identity = uuid4().hex
                        site.active[identity] = waiter.context
                        site.last_root = waiter.context.root_id
                        site.next_start = now + site.policy.min_interval_seconds
                        return RequestPermit(self, site_id, identity)
                    delay = ready_at - now if ready_at > now else None
                    if control is not None and control.checkpoint is not None:
                        delay = min(delay, 0.1) if delay is not None else 0.1
                try:
                    if delay is None:
                        await waiter.event.wait()
                    else:
                        await asyncio.wait_for(waiter.event.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            with self._lock:
                site.waiting.remove(waiter)
                site.wake()

    def release(self, site_id: str, identity: str) -> None:
        with self._lock:
            site = self._site(site_id)
            self._transports.discard(identity)
            if site.active.pop(identity, None) is not None:
                site.wake()

    def start_transport(self, permit: RequestPermit, cancelled: Event) -> None:
        """Recheck spacing immediately in the thread that invokes the transport.

        Executor scheduling may delay two separately admitted coroutines until
        the same instant. A second start check prevents that delayed burst.
        """
        while not cancelled.is_set():
            with self._lock:
                self._check_running()
                site = self._site(permit.site_id)
                if site.cooldown_until > self._clock():
                    raise TransportDeferred
                if permit.identity not in site.active:
                    raise RuntimeError("HTTP permit was released before transport start")
                now = self._clock()
                ready = max(
                    site.last_transport_start + site.policy.min_interval_seconds,
                    site.cooldown_until,
                )
                if now >= ready:
                    self._transports.add(permit.identity)
                    site.last_transport_start = now
                    site.next_start = max(site.next_start, now + site.policy.min_interval_seconds)
                    return
                delay = ready - now
            cancelled.wait(min(delay, 0.1))
        raise RuntimeError("HTTP transport start was cancelled")

    def defer(self, site_id: str, seconds: float, *, status: int | None = None) -> float:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("invalid site cooldown duration")
        with self._lock:
            site = self._site(site_id)
            retry_at = self._wall_clock() + seconds
            extended = retry_at >= site.retry_at
            if extended:
                site.retry_at = retry_at
                site.status = status
            site.cooldown_until = max(site.cooldown_until, self._clock() + seconds)
            site.wake()
            # Keep the in-memory deadline even if durable storage fails; report
            # that failure rather than admitting other callers prematurely.
            if extended and self._store is not None:
                self._store.extend(site_id, retry_at, status)
            return site.retry_at

    def gate_idle(self, gate: RequestGate) -> bool:
        with self._lock:
            return not any(
                context.gate is gate
                for site in self._sites.values()
                for context in site.active.values()
            )

    def cooling(self, site_id: str) -> bool:
        with self._lock:
            return self._site(site_id).cooldown_until > self._clock()

    def wake(self) -> None:
        with self._lock:
            for site in self._sites.values():
                site.wake()

    def stop(self) -> None:
        """Fence new attempts before the owner lease can be handed over."""
        with self._lock:
            self._stopped = True
            for site in self._sites.values():
                site.wake()

    def _check_running(self) -> None:
        if self._stopped:
            raise OwnerShutdownRequested("HTTP owner is stopping")

    @property
    def active_transport_count(self) -> int:
        with self._lock:
            return len(self._transports)

    def observe_error(self, site_id: str, error: Exception, attempt: int) -> float | None:
        delay = retry_delay(error, attempt, self.policy(site_id), wall_clock=self._wall_clock)
        if delay is not None:
            return self.defer(site_id, delay, status=http_status(error))
        return None

    def snapshot(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            for site_id in self._policies:
                self._site(site_id)
            now = self._clock()
            return tuple(
                {
                    "site_id": site_id,
                    "in_flight": len(site.active),
                    "limit": site.policy.max_concurrency,
                    "min_interval_seconds": site.policy.min_interval_seconds,
                    "waiting_by_phase": dict(Counter(w.context.phase for w in site.waiting)),
                    "cooldown_until": _iso(site.retry_at) if site.cooldown_until > now else None,
                    "cooldown_seconds": max(0.0, site.cooldown_until - now),
                    "http_status": site.status if site.cooldown_until > now else None,
                    "waiting": tuple(
                        {
                            "root_id": w.context.root_id,
                            "task_id": w.context.task_id,
                            "phase": w.context.phase,
                            "reason": (
                                "site_cooldown"
                                if site.cooldown_until > now
                                else "request_capacity"
                                if len(site.active) >= site.policy.max_concurrency
                                else "request_interval"
                                if site.next_start > now
                                else "request_capacity"
                            ),
                        }
                        for w in site.waiting
                    ),
                }
                for site_id, site in sorted(self._sites.items())
            )


def http_status(error: Exception) -> int | None:
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        status = getattr(error, "code", getattr(error, "status_code", None))
        if isinstance(status, int):
            return status
        if not isinstance(error.__cause__, Exception):
            break
        error = error.__cause__
    return None


def retry_after_seconds(
    error: Exception, *, wall_clock: Callable[[], float] = time
) -> float | None:
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        headers = getattr(error, "headers", None)
        value = headers.get("Retry-After") if headers is not None else None
        if value is not None:
            try:
                seconds = float(value)
                return max(0.0, seconds) if math.isfinite(seconds) else None
            except (TypeError, ValueError):
                try:
                    date = parsedate_to_datetime(str(value))
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=UTC)
                    return max(0.0, date.timestamp() - wall_clock())
                except (TypeError, ValueError, OverflowError):
                    return None
        if not isinstance(error.__cause__, Exception):
            break
        error = error.__cause__
    return None


def retry_delay(
    error: Exception, attempt: int, policy: RequestPolicy, *, wall_clock: Callable[[], float] = time
) -> float | None:
    status = http_status(error)
    after = retry_after_seconds(error, wall_clock=wall_clock)
    if status not in policy.retryable_status_codes and not (status == 503 and after is not None):
        return None
    if after is not None:
        return after
    fallback = min(policy.max_backoff_seconds, 2.0 ** min(attempt - 1, 30))
    return min(policy.max_backoff_seconds, fallback + random.uniform(0, min(1, fallback * 0.1)))


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()
