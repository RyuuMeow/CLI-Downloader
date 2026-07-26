"""TaskStore adapter backed by the unified StateDatabase."""

from __future__ import annotations

from cli_downloader.storage.state_database import StateDatabase, StateUnitOfWork
from cli_downloader.storage.task_store import TaskRecord


class StateTaskStore:
    """Expose TaskStore operations through short, durable Unit of Work transactions."""

    def __init__(self, database: StateDatabase) -> None:
        self._database = database

    @property
    def database(self) -> StateDatabase:
        return self._database

    def upsert(self, record: TaskRecord) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            unit_of_work.tasks.upsert(record)

    def compare_and_swap(self, record: TaskRecord, *, expected_revision: int) -> bool:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.tasks.compare_and_swap(record, expected_revision)

    def get(self, task_id: str) -> TaskRecord | None:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.tasks.get(task_id)

    def list(self) -> tuple[TaskRecord, ...]:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.tasks.list()

    def delete(self, task_id: str) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            unit_of_work.tasks.delete(task_id)
