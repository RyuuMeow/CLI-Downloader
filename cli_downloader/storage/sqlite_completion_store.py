from __future__ import annotations

from cli_downloader.storage.state_database import StateDatabase, StateUnitOfWork


class SQLiteCompletionStore:
    """Persist duplicate-download identities in the shared state database."""

    def __init__(self, database: StateDatabase) -> None:
        self._database = database

    @property
    def database(self) -> StateDatabase:
        return self._database

    def record_completed(self, site_id: str, content_key: str) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            unit_of_work.completion.record_completed(site_id, content_key)

    def is_completed(self, site_id: str, content_key: str) -> bool:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.completion.is_completed(site_id, content_key)

    def forget(self, site_id: str, content_key: str) -> bool:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.completion.forget(site_id, content_key)
