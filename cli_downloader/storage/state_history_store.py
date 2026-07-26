"""HistoryStore adapter backed by the unified StateDatabase."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_policy import should_persist_history_event
from cli_downloader.core.history_records import DownloadHistoryRecord
from cli_downloader.storage.state_database import StateDatabase, StateUnitOfWork


class StateHistoryStore:
    """Expose HistoryStore operations without giving History ownership of Task state."""

    def __init__(self, database: StateDatabase) -> None:
        self._database = database

    @property
    def database(self) -> StateDatabase:
        return self._database

    def append(self, event: TaskEvent) -> None:
        if not should_persist_history_event(event):
            return
        with StateUnitOfWork(self._database) as unit_of_work:
            unit_of_work.history.append(event)

    def append_many(self, events: tuple[TaskEvent, ...]) -> None:
        events = tuple(event for event in events if should_persist_history_event(event))
        if not events:
            return
        with StateUnitOfWork(self._database) as unit_of_work:
            unit_of_work.history.append_many(events)

    def list(self, *, task_id: str | None = None) -> tuple[TaskEvent, ...]:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.history.list(task_id=task_id)

    def delete_task(self, task_id: str) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            record = unit_of_work.history_records.get_latest_for_task(task_id)
            if record is not None:
                unit_of_work.history_records.delete_aggregate(record.record_id)
            else:
                unit_of_work.history.delete_task(task_id)

    def delete_older_than(self, days: float) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            cutoff = datetime.now(UTC) - timedelta(days=days)
            unit_of_work.history_records.delete_older_than(cutoff)

    def clear(self) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            unit_of_work.history.clear()
            unit_of_work.history_records.clear()

    def upsert_record(self, record: DownloadHistoryRecord) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            unit_of_work.history_records.upsert(record)

    def get_record(self, record_id: str) -> DownloadHistoryRecord | None:
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                return unit_of_work.history_records.get(record_id)
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return None
            raise

    def get_latest_record(self, task_id: str) -> DownloadHistoryRecord | None:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.history_records.get_latest_for_task(task_id)

    def list_records(
        self,
        *,
        task_id: str | None = None,
        parent_record_id: str | None = None,
    ) -> tuple[DownloadHistoryRecord, ...]:
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                return unit_of_work.history_records.list(
                    task_id=task_id,
                    parent_record_id=parent_record_id,
                )
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return ()
            raise

    def delete_record(self, record_id: str) -> bool:
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                return unit_of_work.history_records.delete(record_id)
        except sqlite3.OperationalError as error:
            if "no such table" in str(error).lower():
                return False
            raise
