from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import BoundedSemaphore, Lock
from time import monotonic
from typing import TypeVar

from cli_downloader.application.item_scheduler import ItemLease, ItemScheduler, SchedulerPolicy


ResultT = TypeVar("ResultT")
RetryReporter = Callable[[dict[str, object]], None]


@dataclass(frozen=True)
class RequestPolicy:
    max_concurrency: int = 2
    min_interval_seconds: float = 0.5
    retryable_status_codes: tuple[int, ...] = (429,)
    max_attempts: int = 5
    max_backoff_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_concurrency <= 0 or self.max_attempts <= 0:
            raise ValueError("request concurrency and attempts must be positive")
        if self.min_interval_seconds < 0 or self.max_backoff_seconds <= 0:
            raise ValueError("request timing values are invalid")


class _RequestGate:
    def __init__(self, policy: RequestPolicy) -> None:
        self.policy = policy
        self.capacity = BoundedSemaphore(policy.max_concurrency)
        self.lock = Lock()
        self.next_request_at = 0.0
        self.cooldown_until = 0.0

    async def acquire(self) -> None:
        # Never leave an uncancellable executor thread waiting for a permit.
        # Check shared timing and capacity atomically across worker event loops.
        while True:
            with self.lock:
                now = monotonic()
                ready_at = max(self.next_request_at, self.cooldown_until)
                if now >= ready_at and self.capacity.acquire(blocking=False):
                    self.next_request_at = now + self.policy.min_interval_seconds
                    return
            await asyncio.sleep(0.02)

    def defer(self, delay: float) -> None:
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, monotonic() + delay)


class _RequestLease:
    def __init__(self, gate: _RequestGate) -> None:
        self.gate = gate
        self.held = False

    async def acquire(self) -> None:
        if not self.held:
            await self.gate.acquire()
            self.held = True

    def release(self) -> None:
        if self.held:
            self.held = False
            self.gate.capacity.release()


_current_request: ContextVar[_RequestLease | None] = ContextVar("current_request", default=None)


def suspend_current_request() -> None:
    lease = _current_request.get()
    if lease is not None:
        lease.release()


async def resume_current_request() -> None:
    lease = _current_request.get()
    if lease is not None:
        await lease.acquire()


class GalleryDownloadLimiter:
    def __init__(
        self,
        limit: int = 2,
        *,
        request_policies: Mapping[str, RequestPolicy] | None = None,
        default_request_policy: RequestPolicy | None = None,
        item_policy: SchedulerPolicy | str = SchedulerPolicy.ROUND_ROBIN,
        item_site_limit: int | None = None,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("gallery download limit must be a positive integer")
        self._scheduler = ItemScheduler(
            max_concurrent_downloads=limit,
            max_concurrent_downloads_per_site=item_site_limit,
            policy=item_policy,
        )
        self._legacy_leases: dict[asyncio.Task[object], ItemLease] = {}
        self._default_request_policy = default_request_policy or RequestPolicy()
        self._request_policies = dict(request_policies or {})
        self._request_gates: dict[str, _RequestGate] = {}
        self._request_gates_lock = Lock()

    @property
    def max_concurrent_downloads(self) -> int:
        return self._scheduler.max_concurrent_downloads

    async def __aenter__(self) -> GalleryDownloadLimiter:
        task = asyncio.current_task()
        lease = await self._scheduler.acquire(root_id="anonymous", site_id="anonymous")
        if task is not None:
            self._legacy_leases[task] = lease
        return self

    async def __aexit__(self, *_error: object) -> None:
        task = asyncio.current_task()
        lease = self._legacy_leases.pop(task, None) if task is not None else None
        if lease is not None:
            await lease.release()

    @asynccontextmanager
    async def item(
        self,
        *,
        root_id: str,
        site_id: str,
        sequence: int = 0,
    ):
        lease = await self.acquire_item(
            root_id=root_id,
            site_id=site_id,
            sequence=sequence,
        )
        try:
            yield lease
        finally:
            await lease.release()

    async def acquire_item(
        self,
        *,
        root_id: str,
        site_id: str,
        sequence: int = 0,
    ) -> ItemLease:
        return await self._scheduler.acquire(
            root_id=root_id,
            site_id=site_id,
            sequence=sequence,
        )

    def suspend_root_sync(self, root_id: str) -> None:
        self._scheduler.suspend_root_sync(root_id)

    def resume_root_sync(self, root_id: str) -> None:
        self._scheduler.resume_root_sync(root_id)

    def cancel_root_sync(self, root_id: str) -> None:
        self._scheduler.cancel_root_sync(root_id)

    async def waiting_snapshot(self) -> tuple[dict[str, object], ...]:
        return await self._scheduler.waiting_snapshot()

    async def active_snapshot(self) -> tuple[dict[str, object], ...]:
        return await self._scheduler.active_snapshot()

    def waiting_snapshot_sync(self) -> tuple[dict[str, object], ...]:
        return self._scheduler.waiting_snapshot_sync()

    def active_snapshot_sync(self) -> tuple[dict[str, object], ...]:
        return self._scheduler.active_snapshot_sync()

    async def request(
        self,
        site_id: str,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        report_retry: RetryReporter | None = None,
    ) -> ResultT:
        gate = self._gate(site_id)
        policy = gate.policy
        for attempt in range(1, policy.max_attempts + 1):
            lease = _RequestLease(gate)
            await lease.acquire()
            token = _current_request.set(lease)
            try:
                return await operation()
            except Exception as error:
                delay = _retry_delay(error, attempt, policy)
                if delay is None:
                    _annotate_failure(error, attempt, None)
                    raise
                gate.defer(delay)
                next_retry_at = datetime.fromtimestamp(
                    datetime.now(UTC).timestamp() + delay,
                    UTC,
                ).isoformat()
                _annotate_failure(error, attempt, next_retry_at)
                if attempt == policy.max_attempts:
                    raise
                if report_retry is not None:
                    report_retry(
                        {
                            "site_id": site_id,
                            "attempt": attempt,
                            "max_attempts": policy.max_attempts,
                            "delay_seconds": round(delay, 3),
                            "next_retry_at": next_retry_at,
                            "http_status": getattr(error, "code", None),
                        }
                    )
            finally:
                _current_request.reset(token)
                lease.release()
        raise RuntimeError("request retry loop exited unexpectedly")

    def _gate(self, site_id: str) -> _RequestGate:
        with self._request_gates_lock:
            gate = self._request_gates.get(site_id)
            if gate is None:
                gate = _RequestGate(
                    self._request_policies.get(site_id, self._default_request_policy)
                )
                self._request_gates[site_id] = gate
            return gate


def _retry_delay(error: Exception, attempt: int, policy: RequestPolicy) -> float | None:
    status = getattr(error, "code", None)
    if status not in policy.retryable_status_codes:
        return None
    retry_after = _retry_after_seconds(error)
    if retry_after is not None:
        return retry_after
    fallback = min(policy.max_backoff_seconds, float(2 ** (attempt - 1)))
    return fallback + random.uniform(0.0, min(1.0, fallback * 0.1))


def _retry_after_seconds(error: Exception) -> float | None:
    headers = getattr(error, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _annotate_failure(error: Exception, attempt: int, next_retry_at: str | None) -> None:
    try:
        setattr(error, "request_attempt_count", attempt)
        setattr(error, "next_retry_at", next_retry_at)
    except (AttributeError, TypeError):
        pass
