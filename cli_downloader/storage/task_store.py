from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from cli_downloader.core.task_tree import TaskKind


class TaskStatus(StrEnum):
    PENDING = "pending"
    PLANNED = "planned"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"


class TaskDesiredControl(StrEnum):
    RUN = "run"
    PAUSE = "pause"
    CANCEL = "cancel"


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    status: TaskStatus
    kind: TaskKind = TaskKind.ITEM
    desired_control: TaskDesiredControl = TaskDesiredControl.RUN
    revision: int = 0
    active_run_id: str | None = None
    run_generation: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    site_id: str | None = None
    source_url: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        if self.run_generation < 0:
            raise ValueError("run_generation must be non-negative")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("task timestamps must be timezone-aware")

    def transition(self, **changes: object) -> TaskRecord:
        """Create the next persisted Task revision for a lifecycle transition."""
        return replace(
            self,
            **changes,
            revision=self.revision + 1,
            updated_at=datetime.now(UTC),
        )


class TaskStore(Protocol):
    def upsert(self, record: TaskRecord) -> None: ...

    def compare_and_swap(self, record: TaskRecord, *, expected_revision: int) -> bool: ...

    def get(self, task_id: str) -> TaskRecord | None: ...

    def list(self) -> tuple[TaskRecord, ...]: ...

    def delete(self, task_id: str) -> None: ...


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}

    def upsert(self, record: TaskRecord) -> None:
        self._records[record.task_id] = record

    def compare_and_swap(self, record: TaskRecord, *, expected_revision: int) -> bool:
        current = self._records.get(record.task_id)
        if current is None or current.revision != expected_revision:
            return False
        self._records[record.task_id] = record
        return True

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def list(self) -> tuple[TaskRecord, ...]:
        return tuple(self._records.values())

    def delete(self, task_id: str) -> None:
        del self._records[task_id]
