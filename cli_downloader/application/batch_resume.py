from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from cli_downloader.application.batch_pipeline import (
    BatchPackageResolver,
    BatchPipeline,
    BatchRunResult,
)
from cli_downloader.core.batch import (
    BatchEntryStatus,
    DownloadChildStatus,
    FailureStage,
    RetryPlan,
    RetryScope,
)
from cli_downloader.storage.state_batch_store import StateBatchStore

from .batch_retry import ParentRetryPlan


@dataclass(frozen=True)
class BatchResumeResult:
    resume: ParentRetryPlan
    run: BatchRunResult

    def to_dict(self) -> dict[str, object]:
        return {
            "resume": self.resume.to_dict(),
            "batch": self.run.snapshot.to_dict(),
        }


class BatchResumeService:
    """Restore an interrupted batch from its operational state.

    Resume handles tasks that were paused mid-operation — nothing failed,
    the pipeline was just stopped.  Resume continues from where it left
    off, without the cooldown / retry-attempt machinery.
    """

    def __init__(
        self,
        *,
        store: StateBatchStore,
        resolver: BatchPackageResolver,
    ) -> None:
        self._store = store
        self._resolver = resolver

    def plan_resume(self, parent_id: str) -> ParentRetryPlan:
        plans: list[RetryPlan] = []

        # Entries stuck in operational states.
        for entry in self._store.list_entries(parent_id):
            if entry.status in {
                BatchEntryStatus.PENDING_RESOLUTION,
                BatchEntryStatus.RESOLVING,
            }:
                plans.append(
                    RetryPlan(
                        plan_id=uuid4().hex,
                        start_at=FailureStage.RESOLVE,
                        scope=RetryScope.ENTRY,
                        target_ids=(entry.entry_id,),
                        reason="resume_entry",
                    )
                )

        # Children stuck in operational states.
        for child in self._store.list_children(parent_id):
            if child.status is DownloadChildStatus.COMPLETED:
                continue
            if child.status in {
                DownloadChildStatus.QUEUED,
                DownloadChildStatus.DOWNLOADING,
            }:
                plans.append(
                    RetryPlan(
                        plan_id=uuid4().hex,
                        start_at=FailureStage.SCHEDULE,
                        scope=RetryScope.CHILD,
                        target_ids=(child.child_id,),
                        reason=f"resume_{child.status.value}_child",
                        package_version=child.package.version,
                    )
                )
                continue

        return ParentRetryPlan(parent_id=parent_id, plans=tuple(plans))

    async def resume_parent(
        self,
        parent_id: str,
        *,
        pipeline: BatchPipeline,
    ) -> BatchResumeResult:
        resume = self.plan_resume(parent_id)
        return await self._resume(resume, pipeline)

    async def resume_task(
        self,
        task_id: str,
        *,
        pipeline: BatchPipeline,
    ) -> BatchResumeResult:
        entry = self._store.get_entry(task_id)
        if entry is None:
            raise ValueError(f"batch item not found: {task_id}")
        parent_retry = self.plan_resume(entry.parent_id)
        resume = ParentRetryPlan(
            parent_id=entry.parent_id,
            plans=tuple(
                plan
                for plan in parent_retry.plans
                if plan.scope is RetryScope.ENTRY and task_id in plan.target_ids
            ),
        )
        return await self._resume(resume, pipeline)

    async def _resume(
        self,
        resume: ParentRetryPlan,
        pipeline: BatchPipeline,
    ) -> BatchResumeResult:
        parent_id = resume.parent_id
        if not resume.plans:
            raise ValueError(f"batch has no resumable work: {parent_id}")

        recovery_run = getattr(pipeline, "run_recovery_batch", pipeline.run_batch)
        run = await recovery_run(parent_id, retry_plans=resume.plans)
        return BatchResumeResult(resume=resume, run=run)
