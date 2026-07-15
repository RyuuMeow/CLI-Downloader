from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cli_downloader.core.progress import normalise_byte_progress


class TaskKind(StrEnum):
    ITEM = "item"
    BATCH = "batch"


class TaskState(StrEnum):
    PREPARING = "preparing"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class TaskProgress:
    completed_files: int = 0
    total_files: int | None = None
    preparing_items: int = 0
    completed_bytes: int = 0
    total_bytes: int | None = None

    def __post_init__(self) -> None:
        for name in ("completed_files", "preparing_items"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.total_files is not None:
            if (
                isinstance(self.total_files, bool)
                or not isinstance(self.total_files, int)
                or self.total_files < 0
            ):
                raise ValueError("total_files must be a non-negative integer or None")
            if self.completed_files > self.total_files:
                raise ValueError("completed_files cannot exceed total_files")
        if self.total_bytes is not None:
            if (
                isinstance(self.total_bytes, bool)
                or not isinstance(self.total_bytes, int)
                or self.total_bytes < 0
            ):
                raise ValueError("total_bytes must be a non-negative integer or None")
        if isinstance(self.completed_bytes, bool) or not isinstance(self.completed_bytes, int):
            raise ValueError("completed_bytes must be a non-negative integer")
        if self.completed_bytes < 0:
            raise ValueError("completed_bytes must be a non-negative integer")
        completed_bytes, total_bytes = normalise_byte_progress(
            self.completed_bytes,
            self.total_bytes,
        )
        object.__setattr__(self, "completed_bytes", completed_bytes)
        object.__setattr__(self, "total_bytes", total_bytes)

    @property
    def preparing(self) -> bool:
        return self.total_files is None or self.preparing_items > 0

    def to_dict(self) -> dict[str, int | bool | None]:
        return {
            "completed_files": self.completed_files,
            "total_files": self.total_files,
            "preparing_items": self.preparing_items,
            "preparing": self.preparing,
            "completed_bytes": self.completed_bytes,
            "total_bytes": self.total_bytes,
        }


@dataclass(frozen=True)
class TaskNode:
    task_id: str
    kind: TaskKind
    state: TaskState
    title: str
    progress: TaskProgress
    parent_id: str | None = None
    site_id: str | None = None
    source_url: str | None = None
    output_files: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "title": self.title,
            "parent_id": self.parent_id,
            "site_id": self.site_id,
            "source_url": self.source_url,
            "progress": self.progress.to_dict(),
            "output_files": list(self.output_files),
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TaskRootSummary:
    """Compact, row-safe read model for the Tasks root projection."""

    task_id: str
    kind: TaskKind
    state: TaskState
    title: str
    progress: TaskProgress
    site_id: str | None = None
    source_url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_task_node(self) -> TaskNode:
        return TaskNode(
            task_id=self.task_id,
            kind=self.kind,
            state=self.state,
            title=self.title,
            progress=self.progress,
            site_id=self.site_id,
            source_url=self.source_url,
            details=dict(self.details),
        )


@dataclass(frozen=True)
class BatchChildSummary:
    """One bounded-page Batch child row without its execution graph."""

    task_id: str
    parent_id: str
    state: TaskState
    title: str
    progress: TaskProgress
    site_id: str | None = None
    source_url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_task_node(self) -> TaskNode:
        return TaskNode(
            task_id=self.task_id,
            kind=TaskKind.ITEM,
            state=self.state,
            title=self.title,
            progress=self.progress,
            parent_id=self.parent_id,
            site_id=self.site_id,
            source_url=self.source_url,
            details=dict(self.details),
        )


@dataclass(frozen=True)
class BatchChildPage:
    """Stable, ordered page of Batch child summaries."""

    items: tuple[BatchChildSummary, ...]
    total_count: int
    offset: int
    limit: int

    def __post_init__(self) -> None:
        if self.total_count < 0 or self.offset < 0 or self.limit <= 0:
            raise ValueError("BatchChildPage bounds must be non-negative and limit must be positive")
        if self.offset > self.total_count and self.items:
            raise ValueError("BatchChildPage offset cannot exceed total count with items")
        if len(self.items) > self.limit:
            raise ValueError("BatchChildPage contains more items than its limit")


@dataclass(frozen=True)
class TaskFileNode:
    file_id: str
    name: str
    state: str
    attempts: int = 0
    location: str | None = None
    failure: dict[str, Any] | None = None
    completed_bytes: int = 0
    total_bytes: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.completed_bytes, bool) or not isinstance(self.completed_bytes, int):
            raise ValueError("completed_bytes must be a non-negative integer")
        if self.completed_bytes < 0:
            raise ValueError("completed_bytes must be a non-negative integer")
        if self.total_bytes is not None and (
            isinstance(self.total_bytes, bool)
            or not isinstance(self.total_bytes, int)
            or self.total_bytes < 0
        ):
            raise ValueError("total_bytes must be a non-negative integer or None")
        completed_bytes, total_bytes = normalise_byte_progress(
            self.completed_bytes,
            self.total_bytes,
        )
        object.__setattr__(self, "completed_bytes", completed_bytes)
        object.__setattr__(self, "total_bytes", total_bytes)

    def to_dict(self) -> dict[str, object]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "state": self.state,
            "attempts": self.attempts,
            "location": self.location,
            "failure": dict(self.failure) if self.failure is not None else None,
            "completed_bytes": self.completed_bytes,
            "total_bytes": self.total_bytes,
        }
