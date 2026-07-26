from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class HistoryKind(StrEnum):
    """User-visible kind of a durable download history record."""

    ITEM = "item"
    BATCH = "batch"
    CHILD = "child"


@dataclass(frozen=True)
class DownloadHistoryRecord:
    """Immutable identity plus the latest committed outcome for one download run.

    History owns this snapshot.  ``task_id`` is only a lookup/reference value and
    intentionally has no database foreign key, so deleting the active Task cannot
    erase or change a history record.
    """

    record_id: str
    task_id: str
    kind: HistoryKind
    parent_record_id: str | None = None
    entry_id: str | None = None
    plugin_id: str | None = None
    site_id: str | None = None
    title: str | None = None
    source_url: str | None = None
    status: str = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    completed_files: int = 0
    total_files: int | None = None
    output_files: tuple[str, ...] = ()
    run_generation: int = 0

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("record_id must not be empty")
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        for name in ("created_at", "started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        for name in ("completed_files", "run_generation"):
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
        if any(not path.strip() for path in self.output_files):
            raise ValueError("output_files cannot contain empty paths")
