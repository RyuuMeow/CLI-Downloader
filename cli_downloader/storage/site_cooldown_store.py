"""Persist server cooldowns; active requests remain strictly owner-local."""

from __future__ import annotations

from cli_downloader.storage.state_database import StateDatabase


class SiteCooldownStore:
    def __init__(self, database: StateDatabase) -> None:
        self.database = database

    def load(self) -> dict[str, tuple[float, int | None]]:
        with self.database.transaction(read_only=True) as connection:
            return {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    "SELECT site_id, retry_at, status FROM site_cooldowns"
                )
            }

    def extend(self, site_id: str, retry_at: float, status: int | None) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO site_cooldowns(site_id, retry_at, status) VALUES (?, ?, ?)
                ON CONFLICT(site_id) DO UPDATE SET retry_at=excluded.retry_at, status=excluded.status
                WHERE excluded.retry_at >= site_cooldowns.retry_at""",
                (site_id, retry_at, status),
            )
