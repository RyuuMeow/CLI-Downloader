from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import uuid4

from cli_downloader.application.batch_pipeline import (
    BatchPackageResolver,
    BatchPipeline,
    BatchRunResult,
)
from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    AttemptData,
    BatchEntry,
    BatchEntryStatus,
    Checkpoint,
    CheckpointData,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureStage,
    RetryPlan,
    RetryScope,
)
from cli_downloader.core.events import TaskEvent
from cli_downloader.storage.history_store import HistoryStore
from cli_downloader.storage.state_batch_store import StateBatchStore


@dataclass(frozen=True)
class ParentRetryPlan:
    parent_id: str
    plans: tuple[RetryPlan, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "parent_id": self.parent_id,
            "plans": [plan.to_dict() for plan in self.plans],
        }


@dataclass(frozen=True)
class BatchRetryResult:
    retry: ParentRetryPlan
    attempts: tuple[AttemptData, ...]
    run: BatchRunResult

    def to_dict(self) -> dict[str, object]:
        return {
            "retry": self.retry.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "batch": self.run.snapshot.to_dict(),
        }


class BatchRetryService:
    def __init__(
        self,
        *,
        store: StateBatchStore,
        resolver: BatchPackageResolver,
        history_store: HistoryStore | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._history_store = history_store

    def plan_parent(
        self,
        parent_id: str,
        *,
        now: datetime | None = None,
    ) -> ParentRetryPlan:
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("retry planning time must be timezone-aware")
        plans: list[RetryPlan] = []
        for entry in self._store.list_entries(parent_id):
            if entry.status in {
                BatchEntryStatus.RESOLUTION_FAILED,
                BatchEntryStatus.PACKAGE_REJECTED,
                BatchEntryStatus.CANCELLED,
                # Operational states left behind by an interrupted run
                # (e.g. pipeline pause + app restart).
                BatchEntryStatus.PENDING_RESOLUTION,
                BatchEntryStatus.RESOLVING,
            } and (entry.failure is None or entry.failure.retryable):
                plans.append(
                    RetryPlan(
                        plan_id=uuid4().hex,
                        start_at=(
                            entry.failure.stage
                            if entry.failure is not None
                            else FailureStage.RESOLVE
                        ),
                        scope=RetryScope.ENTRY,
                        target_ids=(entry.entry_id,),
                        reason="retry_entry_failure",
                    )
                )

        for child in self._store.list_children(parent_id):
            if child.status is DownloadChildStatus.COMPLETED:
                continue
            assets = self._store.list_assets(child.child_id)
            if child.status not in {
                DownloadChildStatus.FAILED,
                DownloadChildStatus.PARTIALLY_COMPLETED,
                DownloadChildStatus.CANCELLED,
                DownloadChildStatus.QUEUED,
                DownloadChildStatus.DOWNLOADING,
            }:
                continue
            refresh_reason = _refresh_reason(child, assets, current_time)
            if refresh_reason is not None:
                plans.append(
                    RetryPlan(
                        plan_id=uuid4().hex,
                        start_at=FailureStage.RESOLVE,
                        scope=RetryScope.CHILD,
                        target_ids=(child.child_id,),
                        reason=refresh_reason,
                        package_version=child.package.version,
                        automatic_refresh_count=1,
                    )
                )
                continue
            if child.status in {
                DownloadChildStatus.CANCELLED,
                DownloadChildStatus.QUEUED,
                DownloadChildStatus.DOWNLOADING,
            }:
                plans.append(
                    RetryPlan(
                        plan_id=uuid4().hex,
                        start_at=FailureStage.SCHEDULE,
                        scope=RetryScope.CHILD,
                        target_ids=(child.child_id,),
                        reason=f"retry_{child.status.value}_child",
                        package_version=child.package.version,
                    )
                )
                continue

            by_stage: dict[FailureStage, list[str]] = {}
            for record in assets:
                if record.status not in {
                    AssetStatus.FAILED,
                    AssetStatus.CANCELLED,
                    AssetStatus.DOWNLOADING,
                }:
                    continue
                if record.failure is not None and not record.failure.retryable:
                    continue
                stage = record.failure.stage if record.failure is not None else FailureStage.FETCH
                by_stage.setdefault(stage, []).append(record.asset.asset_id)
            for stage, asset_ids in by_stage.items():
                plans.append(
                    RetryPlan(
                        plan_id=uuid4().hex,
                        start_at=stage,
                        scope=RetryScope.ASSET,
                        target_ids=tuple(asset_ids),
                        reason="retry_incomplete_assets",
                        package_version=child.package.version,
                    )
                )
        return ParentRetryPlan(parent_id=parent_id, plans=tuple(plans))

    async def retry_parent(
        self,
        parent_id: str,
        *,
        pipeline: BatchPipeline,
        now: datetime | None = None,
    ) -> BatchRetryResult:
        retry = self.plan_parent(parent_id, now=now)
        return await self._retry(retry, pipeline)

    async def retry_task(
        self,
        task_id: str,
        *,
        pipeline: BatchPipeline,
        now: datetime | None = None,
    ) -> BatchRetryResult:
        entry = self._store.get_entry(task_id)
        if entry is None:
            raise ValueError(f"batch item not found: {task_id}")
        child = self._store.get_child(entry.child_id) if entry.child_id is not None else None
        target_ids = {entry.entry_id}
        if child is not None:
            target_ids.add(child.child_id)
            target_ids.update(
                record.asset.asset_id for record in self._store.list_assets(child.child_id)
            )
        parent_retry = self.plan_parent(entry.parent_id, now=now)
        retry = ParentRetryPlan(
            parent_id=entry.parent_id,
            plans=tuple(
                plan for plan in parent_retry.plans if target_ids.intersection(plan.target_ids)
            ),
        )
        return await self._retry(retry, pipeline)

    async def _retry(
        self,
        retry: ParentRetryPlan,
        pipeline: BatchPipeline,
    ) -> BatchRetryResult:
        parent_id = retry.parent_id
        if not retry.plans:
            raise ValueError(f"batch has no retryable work: {parent_id}")

        attempts: list[AttemptData] = []
        for plan in retry.plans:
            target = self._attempt_target(parent_id, plan)
            failure = _attempt_failure(plan, target, self._store)
            await self._wait_for_cooldown(parent_id, failure)
            attempt = AttemptData(
                attempt_id=uuid4().hex,
                target_id=target.entry_id if isinstance(target, BatchEntry) else target.child_id,
                number=len(
                    self._store.list_attempts(
                        target.entry_id if isinstance(target, BatchEntry) else target.child_id
                    )
                )
                + 1,
                checkpoints=_checkpoints(target, self._store),
                retry_plan=plan,
                failure=failure,
            )
            self._store.append_attempt(attempt)
            attempts.append(attempt)
            await self._prepare(plan, target)

        run = await pipeline.run_batch(parent_id, retry_plans=retry.plans)
        return BatchRetryResult(retry=retry, attempts=tuple(attempts), run=run)

    async def _wait_for_cooldown(
        self,
        parent_id: str,
        failure: FailureData | None,
    ) -> None:
        if failure is None:
            return
        value = failure.details.get("next_retry_at")
        if not isinstance(value, str):
            return
        try:
            retry_at = datetime.fromisoformat(value)
        except ValueError:
            return
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        if delay <= 0:
            return
        if self._history_store is not None:
            self._history_store.append(
                TaskEvent(
                    task_id=parent_id,
                    name="retry_cooldown",
                    reason="retry_waiting_for_site_cooldown",
                    payload={"next_retry_at": value, "delay_seconds": round(delay, 3)},
                )
            )
        await asyncio.sleep(delay)

    def _attempt_target(
        self,
        parent_id: str,
        plan: RetryPlan,
    ) -> BatchEntry | DownloadChild:
        if plan.scope is RetryScope.ENTRY:
            entry = self._store.get_entry(plan.target_ids[0])
            if entry is None or entry.parent_id != parent_id:
                raise ValueError("retry entry does not belong to the parent")
            return entry
        if plan.scope is RetryScope.CHILD:
            child = self._store.get_child(plan.target_ids[0])
            if child is None or child.parent_id != parent_id:
                raise ValueError("retry child does not belong to the parent")
            return child
        targets = set(plan.target_ids)
        for child in self._store.list_children(parent_id):
            if targets <= {
                record.asset.asset_id for record in self._store.list_assets(child.child_id)
            }:
                return child
        raise ValueError("retry assets do not belong to one child")

    async def _prepare(
        self,
        plan: RetryPlan,
        target: BatchEntry | DownloadChild,
    ) -> None:
        if isinstance(target, BatchEntry):
            if target.status is not BatchEntryStatus.RESOLVING:
                self._store.update_entry(
                    replace(target, status=BatchEntryStatus.RESOLVING, failure=None)
                )
            return

        child = target
        if plan.scope is RetryScope.CHILD and plan.start_at is FailureStage.RESOLVE:
            entry = self._store.get_entry(child.entry_id)
            if entry is None:
                raise ValueError(f"batch entry not found: {child.entry_id}")
            retry = getattr(self._resolver, "retry", None)
            if callable(retry):
                package = await retry(
                    entry,
                    plan,
                    asyncio.Event(),
                    lambda _reason, _data: None,
                )
            else:
                package = await self._resolver.resolve(
                    entry,
                    asyncio.Event(),
                    lambda _reason, _data: None,
                )
            package = _next_package(child.package, package)
            child = self._store.replace_child_package(child.child_id, package)

        asset_ids = (
            set(plan.target_ids)
            if plan.scope is RetryScope.ASSET
            else {
                record.asset.asset_id
                for record in self._store.list_assets(child.child_id)
                if record.status is not AssetStatus.COMPLETED
            }
        )
        for record in self._store.list_assets(child.child_id):
            if record.asset.asset_id not in asset_ids:
                continue
            if record.status in {
                AssetStatus.FAILED,
                AssetStatus.CANCELLED,
                AssetStatus.DOWNLOADING,
            }:
                self._store.update_asset(
                    child.child_id,
                    replace(record, status=AssetStatus.QUEUED, failure=None),
                )
        current = self._store.get_child(child.child_id)
        if current is None:
            raise ValueError(f"download child not found: {child.child_id}")
        if current.status in {
            DownloadChildStatus.FAILED,
            DownloadChildStatus.PARTIALLY_COMPLETED,
            DownloadChildStatus.CANCELLED,
        }:
            self._store.update_child(
                replace(current, status=DownloadChildStatus.QUEUED, failure=None)
            )


def _refresh_reason(
    child: DownloadChild,
    assets: tuple[AssetRecord, ...],
    now: datetime,
) -> str | None:
    expires_at = child.package.expires_at
    if expires_at is not None and expires_at <= now:
        return "refresh_expired_package"
    statuses = set(child.package.refresh_on_status)
    if not statuses:
        return None
    failures = [child.failure, *(record.failure for record in assets)]
    if any(
        failure is not None and failure.details.get("http_status") in statuses
        for failure in failures
    ):
        return "refresh_http_status"
    return None


def _next_package(current: DownloadPackage, refreshed: DownloadPackage) -> DownloadPackage:
    if (
        refreshed.plugin_id,
        refreshed.site_id,
        refreshed.gallery_id,
    ) != (
        current.plugin_id,
        current.site_id,
        current.gallery_id,
    ):
        raise ValueError("refreshed package changed gallery identity")
    return replace(
        refreshed,
        package_id=current.package_id,
        version=current.version + 1,
    )


def _checkpoints(
    target: BatchEntry | DownloadChild,
    store: StateBatchStore,
) -> tuple[CheckpointData, ...]:
    if isinstance(target, BatchEntry):
        return (CheckpointData(Checkpoint.SELECTED, target.entry_id),)
    checkpoints = [
        CheckpointData(
            Checkpoint.CHILD_CREATED,
            target.child_id,
            {"package_version": target.package.version},
        )
    ]
    checkpoints.extend(
        CheckpointData(Checkpoint.ASSET_WRITTEN, record.asset.asset_id)
        for record in store.list_assets(target.child_id)
        if record.status is AssetStatus.COMPLETED
    )
    return tuple(checkpoints)


def _attempt_failure(
    plan: RetryPlan,
    target: BatchEntry | DownloadChild,
    store: StateBatchStore,
) -> FailureData | None:
    if plan.scope is RetryScope.ASSET and isinstance(target, DownloadChild):
        target_ids = set(plan.target_ids)
        for record in store.list_assets(target.child_id):
            if record.asset.asset_id in target_ids and record.failure is not None:
                return record.failure
    return target.failure
