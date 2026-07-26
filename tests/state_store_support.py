"""Test-only constructors for the unified state storage adapters."""

from __future__ import annotations

from pathlib import Path

from cli_downloader.core.batch import BatchParent
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.state_batch_store import StateBatchStore as _StateBatchStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_task_store import StateTaskStore as _StateTaskStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus


class StateTaskStore(_StateTaskStore):
    """Construct a State task adapter directly from the test database path."""

    def __init__(self, database: StateDatabase | Path) -> None:
        super().__init__(_database_from(database))


class StateBatchStore(_StateBatchStore):
    """Seed the required authoritative Task root for isolated Batch-store tests."""

    def __init__(self, database: StateDatabase | Path) -> None:
        resolved_database = _database_from(database)
        super().__init__(resolved_database)
        self._tasks = _StateTaskStore(resolved_database)

    def create_batch(self, parent: BatchParent, entries: tuple) -> None:
        if self._tasks.get(parent.parent_id) is None:
            self._tasks.upsert(
                TaskRecord(
                    task_id=parent.parent_id,
                    status=TaskStatus.PENDING,
                    kind=TaskKind.BATCH,
                    title=parent.title,
                )
            )
        super().create_batch(parent, entries)


def _database_from(database: StateDatabase | Path) -> StateDatabase:
    return database if isinstance(database, StateDatabase) else StateDatabase(database)
