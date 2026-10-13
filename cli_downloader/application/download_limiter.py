from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TypeVar

from cli_downloader.application.item_scheduler import ItemLease, ItemScheduler, SchedulerPolicy
from cli_downloader.core.http import interrupt_http_streams
from cli_downloader.application.site_request_coordinator import (
    RequestPolicy as RequestPolicy,
    RequestPermit,
    SiteRequestCoordinator,
    http_status,
)


ResultT = TypeVar("ResultT")
RetryReporter = Callable[[dict[str, object]], None]


class _RequestLease:
    def __init__(self, coordinator: SiteRequestCoordinator, site_id: str) -> None:
        self.coordinator = coordinator
        self.site_id = site_id
        self.task = asyncio.current_task()
        self.permit: RequestPermit | None = None

    async def acquire(self) -> None:
        if self.permit is None:
            self.permit = await self.coordinator.acquire(self.site_id)

    def release(self) -> None:
        if self.permit is not None:
            self.permit.release()
            self.permit = None


_current_request: ContextVar[_RequestLease | None] = ContextVar("current_request", default=None)


async def suspend_current_request() -> None:
    await interrupt_http_streams()
    lease = _current_request.get()
    if lease is not None and lease.task is asyncio.current_task():
        lease.release()


async def resume_current_request() -> None:
    lease = _current_request.get()
    if lease is not None and lease.task is asyncio.current_task():
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
        request_coordinator: SiteRequestCoordinator | None = None,
    ) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("gallery download limit must be a positive integer")
        self._scheduler = ItemScheduler(
            max_concurrent_downloads=limit,
            max_concurrent_downloads_per_site=item_site_limit,
            policy=item_policy,
        )
        self._legacy_leases: dict[asyncio.Task[object], ItemLease] = {}
        self.requests = request_coordinator or SiteRequestCoordinator(
            policies=request_policies, default_policy=default_request_policy
        )

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
        retry_allowed: bool = True,
    ) -> ResultT:
        # Compatibility admission for callbacks whose internal I/O is unknown.
        # Same-task nested legacy calls share the outer operation, but child
        # tasks must acquire their own permits despite ContextVar inheritance.
        current = _current_request.get()
        if (
            current is not None
            and current.task is asyncio.current_task()
            and current.site_id == site_id
        ):
            return await operation()
        policy = self.requests.policy(site_id)
        attempts = policy.max_attempts if retry_allowed else 1
        for attempt in range(1, attempts + 1):
            lease = _RequestLease(self.requests, site_id)
            await lease.acquire()
            token = _current_request.set(lease)
            try:
                return await operation()
            except Exception as error:
                retry_at = self.requests.observe_error(site_id, error, attempt)
                next_retry_at = (
                    datetime.fromtimestamp(retry_at, UTC).isoformat()
                    if retry_at is not None
                    else None
                )
                _annotate_failure(error, attempt, next_retry_at)
                if retry_at is None or attempt == attempts:
                    raise
                if report_retry is not None:
                    report_retry(
                        {
                            "site_id": site_id,
                            "attempt": attempt,
                            "max_attempts": attempts,
                            "next_retry_at": next_retry_at,
                            "http_status": http_status(error),
                        }
                    )
            finally:
                _current_request.reset(token)
                lease.release()
        raise RuntimeError("request retry loop exited unexpectedly")


def _annotate_failure(error: Exception, attempt: int, next_retry_at: str | None) -> None:
    try:
        setattr(error, "request_attempt_count", attempt)
        setattr(error, "next_retry_at", next_retry_at)
    except (AttributeError, TypeError):
        pass
