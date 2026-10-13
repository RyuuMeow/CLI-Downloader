"""Shared SQLite state infrastructure for the P3-18 direct cutover.

It establishes the versioned database and transaction boundary that the Task,
Execution, Completion, and History repositories share.
"""

from __future__ import annotations

import sqlite3
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.task_store import TaskDesiredControl, TaskRecord, TaskStatus


class StateSchemaVersionError(RuntimeError):
    """The database schema cannot be safely opened by this application version."""


class StateUnitOfWorkError(RuntimeError):
    """A repository was used outside its active shared transaction."""


@dataclass(frozen=True)
class StateMigration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class StateMigrationRecord:
    version: int
    name: str
    applied_at: datetime


def _bootstrap_schema(_connection: sqlite3.Connection) -> None:
    """Reserve schema version 1 for shared metadata and migration journaling."""


def _create_completion_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE completion_ledger (
            site_id TEXT NOT NULL,
            content_key TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            PRIMARY KEY (site_id, content_key)
        )
        """
    )


def _create_tasks(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, status TEXT NOT NULL, kind TEXT NOT NULL,
            desired_control TEXT NOT NULL, revision INTEGER NOT NULL,
            active_run_id TEXT, run_generation INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            site_id TEXT, source_url TEXT, title TEXT, metadata_json TEXT NOT NULL
        )
        """
    )


def _create_execution_roots(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE execution_batches (
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
            title TEXT
        );
        CREATE TABLE execution_entries (
            entry_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES execution_batches(task_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE(task_id, position)
        );
        """
    )


def _create_execution_details(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE execution_packages (
            package_id TEXT NOT NULL, version INTEGER NOT NULL,
            payload_json TEXT NOT NULL, PRIMARY KEY (package_id, version)
        );
        CREATE TABLE execution_children (
            child_id TEXT PRIMARY KEY,
            entry_id TEXT NOT NULL UNIQUE REFERENCES execution_entries(entry_id) ON DELETE CASCADE,
            package_id TEXT NOT NULL, package_version INTEGER NOT NULL, status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY(package_id, package_version) REFERENCES execution_packages(package_id, version)
        );
        CREATE TABLE execution_assets (
            child_id TEXT NOT NULL REFERENCES execution_children(child_id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY(child_id, asset_id)
        );
        CREATE TABLE execution_attempts (
            attempt_id TEXT PRIMARY KEY, child_id TEXT REFERENCES execution_children(child_id) ON DELETE CASCADE,
            target_id TEXT NOT NULL, number INTEGER NOT NULL, payload_json TEXT NOT NULL,
            UNIQUE(target_id, number)
        );
        """
    )


def _create_history_events(connection: sqlite3.Connection) -> None:
    """Create an audit table with soft Task references and deterministic ordering."""
    connection.executescript(
        """
        CREATE TABLE task_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            task_sequence INTEGER NOT NULL,
            run_id TEXT,
            task_revision INTEGER,
            name TEXT NOT NULL,
            reason TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(task_id, task_sequence)
        );
        CREATE INDEX task_events_created_at ON task_events(created_at);
        """
    )


def _add_execution_identity_columns(connection: sqlite3.Connection) -> None:
    """Add queryable execution identity fields to the initial graph tables."""
    connection.executescript(
        """
        ALTER TABLE execution_entries ADD COLUMN plugin_id TEXT;
        ALTER TABLE execution_entries ADD COLUMN site_id TEXT;
        ALTER TABLE execution_entries ADD COLUMN candidate_id TEXT;
        ALTER TABLE execution_entries ADD COLUMN child_id TEXT;
        ALTER TABLE execution_packages ADD COLUMN plugin_id TEXT;
        ALTER TABLE execution_packages ADD COLUMN site_id TEXT;
        ALTER TABLE execution_packages ADD COLUMN gallery_id TEXT;
        ALTER TABLE execution_children ADD COLUMN task_id TEXT;
        CREATE INDEX execution_entries_task_status ON execution_entries(task_id, status);
        CREATE UNIQUE INDEX execution_entries_child
            ON execution_entries(child_id) WHERE child_id IS NOT NULL;
        CREATE INDEX execution_entries_site_candidate
            ON execution_entries(site_id, candidate_id, status);
        CREATE INDEX execution_children_task_status ON execution_children(task_id, status);
        CREATE INDEX execution_attempts_target ON execution_attempts(target_id, number);
        """
    )


def _create_download_history(connection: sqlite3.Connection) -> None:
    """Create History-owned display records without Task/Execution foreign keys."""
    connection.executescript(
        """
        CREATE TABLE download_history (
            record_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            parent_record_id TEXT REFERENCES download_history(record_id) ON DELETE CASCADE,
            entry_id TEXT,
            plugin_id TEXT,
            site_id TEXT,
            title TEXT,
            source_url TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            completed_files INTEGER NOT NULL,
            total_files INTEGER,
            output_files_json TEXT NOT NULL,
            run_generation INTEGER NOT NULL,
            UNIQUE(task_id, run_generation)
        );
        CREATE INDEX download_history_task ON download_history(task_id);
        CREATE INDEX download_history_parent ON download_history(parent_record_id);
        CREATE INDEX download_history_order
            ON download_history(finished_at, created_at, record_id);
        """
    )


def _backfill_download_history(connection: sqlite3.Connection) -> None:
    """Project current audit events into History records without Task/Execution reads."""
    rows = connection.execute(
        "SELECT task_id, name, reason, payload_json, created_at FROM task_events ORDER BY event_id"
    ).fetchall()
    if not rows:
        return

    events: list[tuple[str, str, str, dict[str, object], datetime]] = []
    for task_id, name, reason, payload_json, created_at in rows:
        timestamp = datetime.fromisoformat(created_at)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        events.append((task_id, name, reason, json.loads(payload_json), timestamp))

    entries: dict[str, dict[str, object]] = {}
    child_links: dict[str, tuple[str, str]] = {}
    records: dict[str, DownloadHistoryRecord] = {}

    def _entry_snapshot(entry_id: str, parent_id: str) -> dict[str, object]:
        entry = entries.get(entry_id)
        if entry is None:
            entry = {
                "parent_id": parent_id,
                "entry_id": entry_id,
                "plugin_id": None,
                "site_id": None,
                "title": None,
                "source_url": None,
            }
            entries[entry_id] = entry
        return entry

    def _record_from_event(
        record_id: str,
        *,
        task_id: str,
        kind: HistoryKind,
        timestamp: datetime,
        parent_id: str | None = None,
        entry_id: str | None = None,
        plugin_id: str | None = None,
        site_id: str | None = None,
        title: str | None = None,
        source_url: str | None = None,
        status: str = "pending",
        total_files: int | None = None,
    ) -> DownloadHistoryRecord:
        return DownloadHistoryRecord(
            record_id=record_id,
            task_id=task_id,
            kind=kind,
            parent_record_id=parent_id,
            entry_id=entry_id,
            plugin_id=plugin_id,
            site_id=site_id,
            title=title,
            source_url=source_url,
            status=status,
            created_at=timestamp,
            total_files=total_files,
        )

    # Identity pass: creation and package handoff events carry the durable names.
    for task_id, name, _reason, payload, timestamp in events:
        if name == "batch_created":
            records.setdefault(
                task_id,
                _record_from_event(
                    task_id,
                    task_id=task_id,
                    kind=HistoryKind.BATCH,
                    timestamp=timestamp,
                    plugin_id=payload.get("plugin_id"),
                    site_id=payload.get("site_id"),
                    title=payload.get("title"),
                    status="pending",
                ),
            )
        elif name == "task_persisted":
            parent_id = payload.get("batch_id")
            if isinstance(parent_id, str):
                entry = _entry_snapshot(task_id, parent_id)
                entry.update(
                    plugin_id=payload.get("plugin_id"),
                    site_id=payload.get("site_id"),
                    title=payload.get("title"),
                    source_url=payload.get("source_url"),
                )
            else:
                records.setdefault(
                    task_id,
                    _record_from_event(
                        task_id,
                        task_id=task_id,
                        kind=HistoryKind.ITEM,
                        timestamp=timestamp,
                        site_id=payload.get("site_id"),
                        title=payload.get("title"),
                        source_url=payload.get("source_url"),
                        status=str(payload.get("status") or "planned"),
                    ),
                )
        elif name == "batch_progress" and isinstance(payload.get("child_id"), str):
            child_id = str(payload["child_id"])
            entry_id = str(payload.get("entry_id") or "")
            if entry_id:
                child_links[child_id] = (task_id, entry_id)
            entry = entries.get(entry_id) if entry_id else None
            if entry is None and entry_id:
                entry = _entry_snapshot(entry_id, task_id)
            if child_id not in records:
                records[child_id] = _record_from_event(
                    child_id,
                    task_id=child_id,
                    kind=HistoryKind.CHILD,
                    timestamp=timestamp,
                    parent_id=task_id,
                    entry_id=entry_id or None,
                    plugin_id=entry.get("plugin_id") if entry else payload.get("plugin_id"),
                    site_id=entry.get("site_id") if entry else payload.get("site_id"),
                    title=entry.get("title") if entry else payload.get("title"),
                    source_url=entry.get("source_url") if entry else payload.get("source_url"),
                    status="queued",
                    total_files=_non_negative_int(payload.get("total_assets")),
                )
        elif name == "package_persisted":
            child_id = payload.get("child_id")
            entry_id = payload.get("entry_id")
            if isinstance(child_id, str) and isinstance(entry_id, str):
                child_links[child_id] = (task_id, entry_id)
                entry = _entry_snapshot(entry_id, task_id)
                records.setdefault(
                    child_id,
                    _record_from_event(
                        child_id,
                        task_id=child_id,
                        kind=HistoryKind.CHILD,
                        timestamp=timestamp,
                        parent_id=task_id,
                        entry_id=entry_id,
                        plugin_id=entry.get("plugin_id"),
                        site_id=entry.get("site_id"),
                        title=entry.get("title"),
                        source_url=entry.get("source_url"),
                        status="queued",
                        total_files=_non_negative_int(payload.get("asset_count")),
                    ),
                )

    terminal_reasons = {
        "task_completed": "completed",
        "child_completed": "completed",
        "task_partially_completed": "partially_completed",
        "child_partially_completed": "partially_completed",
        "task_failed": "failed",
        "child_failed": "failed",
        "task_cancelled": "canceled",
        "child_cancelled": "canceled",
        "direct_assets_written": "completed",
    }
    # Outcome pass: update counts, paths, and terminal timestamps.
    for task_id, name, reason, payload, timestamp in events:
        target_id = task_id
        child_id = payload.get("child_id")
        if name == "batch_progress" and isinstance(child_id, str):
            target_id = child_id
        record = records.get(target_id)
        if record is None:
            continue
        status = terminal_reasons.get(reason)
        if reason == "batch_finished":
            status = str(payload.get("status") or record.status)
        completed = _non_negative_int(payload.get("completed_assets"))
        if completed is None:
            completed = _non_negative_int(payload.get("asset_count"))
        total = _non_negative_int(payload.get("total_assets"))
        if total is None:
            total = _non_negative_int(payload.get("total_files"))
        output_files = _history_output_files(payload)
        records[target_id] = replace(
            record,
            status=status or record.status,
            completed_files=max(record.completed_files, completed or 0),
            total_files=max(record.total_files or 0, total or 0) or record.total_files,
            output_files=tuple(dict.fromkeys((*record.output_files, *output_files))),
            finished_at=timestamp if status is not None else record.finished_at,
        )

    # Parent totals are derived only from the projected child records.
    for parent_id, parent in tuple(records.items()):
        if parent.kind is not HistoryKind.BATCH:
            continue
        children = tuple(
            child for child in records.values() if child.parent_record_id == parent.record_id
        )
        if not children:
            continue
        records[parent_id] = replace(
            parent,
            completed_files=sum(child.completed_files for child in children),
            total_files=sum(child.total_files or 0 for child in children) or None,
            output_files=tuple(path for child in children for path in child.output_files),
        )

    ordered = sorted(
        records.values(),
        key=lambda record: (
            0 if record.kind is HistoryKind.BATCH else 2 if record.kind is HistoryKind.CHILD else 1,
            record.record_id,
        ),
    )
    for record in ordered:
        connection.execute(
            """
            INSERT OR IGNORE INTO download_history (
                record_id, task_id, kind, parent_record_id, entry_id, plugin_id, site_id,
                title, source_url, status, created_at, started_at, finished_at,
                completed_files, total_files, output_files_json, run_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _history_record_values(record),
        )


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _history_output_files(payload: dict[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    output_files = payload.get("output_files")
    if isinstance(output_files, list):
        values.extend(path for path in output_files if isinstance(path, str) and path)
    path = payload.get("path")
    if isinstance(path, str) and path:
        values.append(path)
    return tuple(dict.fromkeys(values))


_MIGRATIONS = (
    StateMigration(1, "bootstrap_state_database", _bootstrap_schema),
    StateMigration(2, "create_completion_ledger", _create_completion_ledger),
    StateMigration(4, "create_tasks", _create_tasks),
    StateMigration(5, "create_execution_roots", _create_execution_roots),
    StateMigration(6, "create_execution_details", _create_execution_details),
    StateMigration(8, "create_history_events", _create_history_events),
    StateMigration(9, "add_execution_identity_columns", _add_execution_identity_columns),
    StateMigration(10, "create_download_history", _create_download_history),
    StateMigration(11, "backfill_download_history", _backfill_download_history),
)


class StateDatabase:
    """Own the physical ``state.sqlite3`` file and its versioned schema."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._read_only = False
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def read_only(self) -> bool:
        """Whether schema migration was skipped because the existing DB is read-only."""
        return self._read_only

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return self._schema_version(connection)

    def migration_records(self) -> tuple[StateMigrationRecord, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT version, name, applied_at FROM migration_journal ORDER BY version"
            ).fetchall()
        return tuple(
            StateMigrationRecord(
                version=row[0],
                name=row[1],
                applied_at=datetime.fromisoformat(row[2]),
            )
            for row in rows
        )

    @contextmanager
    def transaction(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        """Run one all-or-nothing state transition on a dedicated connection."""
        connection = self._connect()
        try:
            if read_only:
                connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
                )
                connection.execute(
                    """
                CREATE TABLE IF NOT EXISTS migration_journal (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
                )
                current_version = self._schema_version(connection)
                newest_supported = _MIGRATIONS[-1].version
                if current_version > newest_supported:
                    raise StateSchemaVersionError(
                        f"state schema version {current_version} is newer than supported "
                        f"version {newest_supported}"
                    )
                for migration in _MIGRATIONS:
                    if migration.version <= current_version:
                        continue
                    migration.apply(connection)
                    connection.execute(
                        "INSERT INTO migration_journal (version, name, applied_at) VALUES (?, ?, ?)",
                        (migration.version, migration.name, datetime.now(UTC).isoformat()),
                    )
                    connection.execute(
                        """
                    INSERT INTO schema_metadata (key, value) VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                        (str(migration.version),),
                    )
        except sqlite3.OperationalError as error:
            if self._db_path.exists() and "readonly" in str(error).lower():
                self._read_only = True
                return
            raise

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            version = int(row[0])
        except (TypeError, ValueError) as error:
            raise StateSchemaVersionError("state schema version is invalid") from error
        if version < 0:
            raise StateSchemaVersionError("state schema version cannot be negative")
        return version

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()


class _StateRepository:
    """Base for logical repositories that share one Unit of Work connection."""

    def __init__(self, unit_of_work: StateUnitOfWork) -> None:
        self._unit_of_work = unit_of_work

    @property
    def connection(self) -> sqlite3.Connection:
        return self._unit_of_work.connection


class TaskRepository(_StateRepository):
    """Future owner of public Task identity and lifecycle rows."""

    def upsert(self, record: TaskRecord) -> None:
        self.connection.execute(
            """INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, kind=excluded.kind,
            desired_control=excluded.desired_control, revision=excluded.revision,
            active_run_id=excluded.active_run_id, run_generation=excluded.run_generation,
            created_at=excluded.created_at, updated_at=excluded.updated_at, site_id=excluded.site_id,
            source_url=excluded.source_url, title=excluded.title, metadata_json=excluded.metadata_json""",
            _task_values(record),
        )

    def get(self, task_id: str) -> TaskRecord | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _task_from_row(row) if row else None

    def list(self) -> tuple[TaskRecord, ...]:
        rows = self.connection.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def list_root_page(self, *, offset: int, limit: int) -> tuple[tuple[TaskRecord, ...], int]:
        predicate = """NOT EXISTS (
            SELECT 1 FROM execution_entries e WHERE e.entry_id = tasks.task_id
        ) AND (kind = 'item' OR EXISTS (
            SELECT 1 FROM execution_batches b WHERE b.task_id = tasks.task_id
        ))"""
        total = self.connection.execute(f"SELECT COUNT(*) FROM tasks WHERE {predicate}").fetchone()[
            0
        ]
        rows = self.connection.execute(
            f"""SELECT task_id, status, kind, desired_control, revision,
                active_run_id, run_generation, created_at, updated_at, site_id,
                source_url, title,
                json_object(
                    'total_files', json_extract(metadata_json, '$.total_files'),
                    'asset_count', json_extract(metadata_json, '$.asset_count'),
                    'completed_files', COALESCE(json_extract(metadata_json, '$.completed_files'),
                        json_array_length(metadata_json, '$.output_files'), 0),
                    'failure', json_extract(metadata_json, '$.failure'),
                    'torrent_checkpoints', json_extract(metadata_json, '$.torrent_checkpoints')
                )
                FROM tasks WHERE {predicate} ORDER BY rowid DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return tuple(_task_from_row(row) for row in rows), int(total)

    def delete(self, task_id: str) -> None:
        cursor = self.connection.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        if cursor.rowcount == 0:
            raise KeyError(task_id)

    def compare_and_swap(self, record: TaskRecord, expected_revision: int) -> bool:
        cursor = self.connection.execute(
            "UPDATE tasks SET status=?, kind=?, desired_control=?, revision=?, active_run_id=?, "
            "run_generation=?, created_at=?, updated_at=?, site_id=?, source_url=?, title=?, "
            "metadata_json=? WHERE task_id=? AND revision=?",
            (*_task_values(record)[1:], record.task_id, expected_revision),
        )
        return cursor.rowcount == 1


def _task_values(record: TaskRecord) -> tuple[object, ...]:
    return (
        record.task_id,
        record.status.value,
        record.kind.value,
        record.desired_control.value,
        record.revision,
        record.active_run_id,
        record.run_generation,
        record.created_at.isoformat(),
        record.updated_at.isoformat(),
        record.site_id,
        record.source_url,
        record.title,
        json.dumps(record.metadata, sort_keys=True),
    )


def _task_from_row(row: tuple[object, ...]) -> TaskRecord:
    return TaskRecord(
        task_id=row[0],
        status=TaskStatus(row[1]),
        kind=TaskKind(row[2]),
        desired_control=TaskDesiredControl(row[3]),
        revision=row[4],
        active_run_id=row[5],
        run_generation=row[6],
        created_at=datetime.fromisoformat(row[7]),
        updated_at=datetime.fromisoformat(row[8]),
        site_id=row[9],
        source_url=row[10],
        title=row[11],
        metadata=json.loads(row[12]),
    )


class ExecutionRepository(_StateRepository):
    """Future owner of Batch entries, runs, assets, attempts, and checkpoints."""

    def create_batch(self, task_id: str, title: str | None) -> None:
        self.connection.execute(
            "INSERT INTO execution_batches (task_id, title) VALUES (?, ?)", (task_id, title)
        )

    def add_entry(
        self,
        task_id: str,
        entry_id: str,
        position: int,
        status: str,
        payload: object,
        *,
        plugin_id: str,
        site_id: str,
        candidate_id: str,
        child_id: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO execution_entries
            (entry_id, task_id, position, status, payload_json, plugin_id, site_id, candidate_id, child_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id,
                task_id,
                position,
                status,
                json.dumps(payload, sort_keys=True),
                plugin_id,
                site_id,
                candidate_id,
                child_id,
            ),
        )

    def list_batch_ids(self) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT task_id FROM execution_batches ORDER BY rowid DESC"
        ).fetchall()
        return tuple(row[0] for row in rows)

    def get_batch_title(self, task_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT title FROM execution_batches WHERE task_id = ?", (task_id,)
        ).fetchone()
        return row[0] if row else None

    def list_entry_payloads(self, task_id: str) -> tuple[tuple[object, ...], ...]:
        rows = self.connection.execute(
            """SELECT entry_id, status, payload_json, child_id FROM execution_entries
            WHERE task_id = ? ORDER BY position""",
            (task_id,),
        ).fetchall()
        return tuple(rows)

    def get_entry_payload(self, entry_id: str) -> tuple[object, ...] | None:
        return self.connection.execute(
            """SELECT task_id, status, payload_json, child_id FROM execution_entries
            WHERE entry_id = ?""",
            (entry_id,),
        ).fetchone()

    def update_entry(
        self, entry_id: str, status: str, payload: object, child_id: str | None
    ) -> bool:
        cursor = self.connection.execute(
            """UPDATE execution_entries SET status = ?, payload_json = ?, child_id = ?
            WHERE entry_id = ?""",
            (status, json.dumps(payload, sort_keys=True), child_id, entry_id),
        )
        return cursor.rowcount == 1

    def delete_batch(self, task_id: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM execution_batches WHERE task_id = ?", (task_id,)
        )
        return cursor.rowcount == 1

    def add_package(
        self,
        package_id: str,
        version: int,
        payload: object,
        *,
        plugin_id: str,
        site_id: str,
        gallery_id: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO execution_packages
            (package_id, version, payload_json, plugin_id, site_id, gallery_id)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                package_id,
                version,
                json.dumps(payload, sort_keys=True),
                plugin_id,
                site_id,
                gallery_id,
            ),
        )

    def get_package_payload(self, package_id: str, version: int) -> object | None:
        row = self.connection.execute(
            """SELECT payload_json FROM execution_packages
            WHERE package_id = ? AND version = ?""",
            (package_id, version),
        ).fetchone()
        return row[0] if row else None

    def add_child(
        self,
        child_id: str,
        task_id: str,
        entry_id: str,
        package_id: str,
        package_version: int,
        status: str,
        payload: object,
    ) -> None:
        self.connection.execute(
            """INSERT INTO execution_children
            (child_id, entry_id, package_id, package_version, status, payload_json, task_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                child_id,
                entry_id,
                package_id,
                package_version,
                status,
                json.dumps(payload, sort_keys=True),
                task_id,
            ),
        )

    def get_child_payload(self, child_id: str) -> object | None:
        row = self.connection.execute(
            "SELECT payload_json FROM execution_children WHERE child_id = ?", (child_id,)
        ).fetchone()
        return row[0] if row else None

    def list_child_payloads(self, task_id: str) -> tuple[object, ...]:
        rows = self.connection.execute(
            """SELECT child.payload_json FROM execution_children AS child
            JOIN execution_entries AS entry ON entry.entry_id = child.entry_id
            WHERE child.task_id = ? ORDER BY entry.position""",
            (task_id,),
        ).fetchall()
        return tuple(row[0] for row in rows)

    def update_child(self, child_id: str, status: str, payload: object) -> bool:
        cursor = self.connection.execute(
            """UPDATE execution_children SET status = ?, payload_json = ?
            WHERE child_id = ?""",
            (status, json.dumps(payload, sort_keys=True), child_id),
        )
        return cursor.rowcount == 1

    def add_asset(self, child_id: str, asset_id: str, status: str, payload: object) -> None:
        self.connection.execute(
            """INSERT INTO execution_assets (child_id, asset_id, status, payload_json)
            VALUES (?, ?, ?, ?)""",
            (child_id, asset_id, status, json.dumps(payload, sort_keys=True)),
        )

    def list_asset_payloads(self, child_id: str) -> tuple[object, ...]:
        rows = self.connection.execute(
            """SELECT payload_json FROM execution_assets WHERE child_id = ? ORDER BY rowid""",
            (child_id,),
        ).fetchall()
        return tuple(row[0] for row in rows)

    def get_asset_payload(self, child_id: str, asset_id: str) -> object | None:
        row = self.connection.execute(
            """SELECT payload_json FROM execution_assets WHERE child_id = ? AND asset_id = ?""",
            (child_id, asset_id),
        ).fetchone()
        return row[0] if row else None

    def update_asset(self, child_id: str, asset_id: str, status: str, payload: object) -> bool:
        cursor = self.connection.execute(
            """UPDATE execution_assets SET status = ?, payload_json = ?
            WHERE child_id = ? AND asset_id = ?""",
            (status, json.dumps(payload, sort_keys=True), child_id, asset_id),
        )
        return cursor.rowcount == 1

    def replace_child_package(
        self,
        child_id: str,
        package_id: str,
        package_version: int,
        child_payload: object,
        asset_records: tuple[tuple[str, str, object], ...],
    ) -> None:
        self.connection.execute(
            """UPDATE execution_children
            SET package_version = ?, payload_json = ? WHERE child_id = ?""",
            (package_version, json.dumps(child_payload, sort_keys=True), child_id),
        )
        self.connection.execute("DELETE FROM execution_assets WHERE child_id = ?", (child_id,))
        for asset_id, status, payload in asset_records:
            self.add_asset(child_id, asset_id, status, payload)

    def append_attempt(
        self,
        attempt_id: str,
        target_id: str,
        number: int,
        payload: object,
        *,
        child_id: str | None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO execution_attempts
            (attempt_id, child_id, target_id, number, payload_json)
            VALUES (?, ?, ?, ?, ?)""",
            (attempt_id, child_id, target_id, number, json.dumps(payload, sort_keys=True)),
        )

    def list_attempt_payloads(self, target_id: str) -> tuple[object, ...]:
        rows = self.connection.execute(
            """SELECT payload_json FROM execution_attempts WHERE target_id = ?
            ORDER BY number, rowid""",
            (target_id,),
        ).fetchall()
        return tuple(row[0] for row in rows)


class CompletionRepository(_StateRepository):
    """Own durable duplicate-download identities inside the active Unit of Work."""

    def record_completed(self, site_id: str, content_key: str) -> None:
        _validate_completion_identity(site_id, content_key)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO completion_ledger (site_id, content_key, completed_at)
            VALUES (?, ?, ?)
            """,
            (site_id, content_key, datetime.now(UTC).isoformat()),
        )

    def is_completed(self, site_id: str, content_key: str) -> bool:
        _validate_completion_identity(site_id, content_key)
        row = self.connection.execute(
            """
            SELECT 1 FROM completion_ledger
            WHERE site_id = ? AND content_key = ?
            """,
            (site_id, content_key),
        ).fetchone()
        return row is not None

    def forget(self, site_id: str, content_key: str) -> bool:
        _validate_completion_identity(site_id, content_key)
        cursor = self.connection.execute(
            "DELETE FROM completion_ledger WHERE site_id = ? AND content_key = ?",
            (site_id, content_key),
        )
        return cursor.rowcount > 0


class HistoryRepository(_StateRepository):
    """Own append-only audit events with soft Task references."""

    def append(
        self,
        event: TaskEvent,
        *,
        run_id: str | None = None,
        task_revision: int | None = None,
    ) -> HistoryEventRecord:
        task_sequence = self._next_task_sequence(event.task_id)
        cursor = self.connection.execute(
            """
            INSERT INTO task_events
                (task_id, task_sequence, run_id, task_revision, name, reason, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.task_id,
                task_sequence,
                run_id,
                task_revision,
                event.name,
                event.reason,
                json.dumps(event.payload, sort_keys=True),
                event.created_at.isoformat(),
            ),
        )
        return HistoryEventRecord(
            event_id=cursor.lastrowid,
            task_sequence=task_sequence,
            run_id=run_id,
            task_revision=task_revision,
            event=event,
        )

    def append_many(self, events: tuple[TaskEvent, ...]) -> tuple[HistoryEventRecord, ...]:
        return tuple(self.append(event) for event in events)

    def list(self, *, task_id: str | None = None) -> tuple[TaskEvent, ...]:
        return tuple(record.event for record in self.list_records(task_id=task_id))

    def list_records(self, *, task_id: str | None = None) -> tuple[HistoryEventRecord, ...]:
        sql = """
            SELECT event_id, task_id, task_sequence, run_id, task_revision, name, reason,
                   payload_json, created_at
            FROM task_events
        """
        params: tuple[str, ...] = ()
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params = (task_id,)
        sql += " ORDER BY event_id"
        rows = self.connection.execute(sql, params).fetchall()
        return tuple(_history_event_from_row(row) for row in rows)

    def delete_task(self, task_id: str) -> None:
        self.connection.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))

    def delete_older_than(self, days: float) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        self.connection.execute("DELETE FROM task_events WHERE created_at <= ?", (cutoff,))

    def clear(self) -> None:
        self.connection.execute("DELETE FROM task_events")

    def _next_task_sequence(self, task_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(task_sequence), 0) + 1 FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0])


class HistoryRecordRepository(_StateRepository):
    """Own durable History projections independently from audit events and Tasks."""

    def upsert(self, record: DownloadHistoryRecord) -> None:
        self.connection.execute(
            """
            INSERT INTO download_history (
                record_id, task_id, kind, parent_record_id, entry_id, plugin_id, site_id,
                title, source_url, status, created_at, started_at, finished_at,
                completed_files, total_files, output_files_json, run_generation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id) DO UPDATE SET
                task_id=excluded.task_id,
                kind=excluded.kind,
                parent_record_id=excluded.parent_record_id,
                entry_id=excluded.entry_id,
                plugin_id=excluded.plugin_id,
                site_id=excluded.site_id,
                title=excluded.title,
                source_url=excluded.source_url,
                status=excluded.status,
                created_at=excluded.created_at,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                completed_files=excluded.completed_files,
                total_files=excluded.total_files,
                output_files_json=excluded.output_files_json,
                run_generation=excluded.run_generation
            """,
            _history_record_values(record),
        )

    def get(self, record_id: str) -> DownloadHistoryRecord | None:
        row = self.connection.execute(
            "SELECT * FROM download_history WHERE record_id = ?", (record_id,)
        ).fetchone()
        return _history_record_from_row(row) if row else None

    def get_latest_for_task(self, task_id: str) -> DownloadHistoryRecord | None:
        rows = self.list(task_id=task_id)
        return rows[0] if rows else None

    def list(
        self,
        *,
        task_id: str | None = None,
        parent_record_id: str | None = None,
    ) -> tuple[DownloadHistoryRecord, ...]:
        sql = "SELECT * FROM download_history"
        clauses: list[str] = []
        params: list[str] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if parent_record_id is not None:
            clauses.append("parent_record_id = ?")
            params.append(parent_record_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY finished_at IS NULL, finished_at DESC, created_at DESC, record_id"
        rows = self.connection.execute(sql, tuple(params)).fetchall()
        return tuple(_history_record_from_row(row) for row in rows)

    def delete(self, record_id: str) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM download_history WHERE record_id = ?", (record_id,)
        )
        return cursor.rowcount > 0

    def delete_aggregate(self, record_id: str) -> bool:
        """Delete one record and its History-owned descendants plus audit events."""
        rows = self.connection.execute(
            """
            SELECT record_id, task_id, entry_id FROM download_history
            WHERE record_id = ? OR parent_record_id = ?
            """,
            (record_id, record_id),
        ).fetchall()
        if not rows:
            return False
        identifiers = {str(row[1]) for row in rows}
        identifiers.update(str(row[2]) for row in rows if row[2])
        placeholders = ",".join("?" for _ in identifiers)
        self.connection.execute(
            f"DELETE FROM task_events WHERE task_id IN ({placeholders})", tuple(identifiers)
        )
        cursor = self.connection.execute(
            "DELETE FROM download_history WHERE record_id = ?", (record_id,)
        )
        return cursor.rowcount > 0

    def delete_older_than(self, cutoff: datetime) -> int:
        terminal_statuses = (
            "completed",
            "partially_completed",
            "failed",
            "canceled",
            "cancelled",
            "skipped",
        )
        placeholders = ",".join("?" for _ in terminal_statuses)
        rows = self.connection.execute(
            f"""
            SELECT record_id FROM download_history
            WHERE finished_at IS NOT NULL AND finished_at <= ?
              AND status IN ({placeholders})
            ORDER BY finished_at, record_id
            """,
            (cutoff.isoformat(), *terminal_statuses),
        ).fetchall()
        deleted = 0
        for (record_id,) in rows:
            deleted += int(self.delete_aggregate(record_id))
        return deleted

    def clear(self) -> None:
        self.connection.execute("DELETE FROM download_history")


@dataclass(frozen=True)
class HistoryEventRecord:
    """One persisted History row, including immutable execution context."""

    event_id: int
    task_sequence: int
    run_id: str | None
    task_revision: int | None
    event: TaskEvent


def _history_record_values(record: DownloadHistoryRecord) -> tuple[object, ...]:
    return (
        record.record_id,
        record.task_id,
        record.kind.value,
        record.parent_record_id,
        record.entry_id,
        record.plugin_id,
        record.site_id,
        record.title,
        record.source_url,
        record.status,
        record.created_at.isoformat(),
        record.started_at.isoformat() if record.started_at else None,
        record.finished_at.isoformat() if record.finished_at else None,
        record.completed_files,
        record.total_files,
        json.dumps(list(record.output_files), sort_keys=True),
        record.run_generation,
    )


def _history_record_from_row(row: tuple[object, ...]) -> DownloadHistoryRecord:
    return DownloadHistoryRecord(
        record_id=row[0],
        task_id=row[1],
        kind=HistoryKind(row[2]),
        parent_record_id=row[3],
        entry_id=row[4],
        plugin_id=row[5],
        site_id=row[6],
        title=row[7],
        source_url=row[8],
        status=row[9],
        created_at=datetime.fromisoformat(row[10]),
        started_at=datetime.fromisoformat(row[11]) if row[11] else None,
        finished_at=datetime.fromisoformat(row[12]) if row[12] else None,
        completed_files=row[13],
        total_files=row[14],
        output_files=tuple(json.loads(row[15])),
        run_generation=row[16],
    )


def _history_event_from_row(row: tuple[object, ...]) -> HistoryEventRecord:
    return HistoryEventRecord(
        event_id=row[0],
        task_sequence=row[2],
        run_id=row[3],
        task_revision=row[4],
        event=TaskEvent(
            task_id=row[1],
            name=row[5],
            reason=row[6],
            payload=json.loads(row[7]),
            created_at=datetime.fromisoformat(row[8]),
        ),
    )


class StateUnitOfWork:
    """Expose the four logical repositories within one SQLite transaction."""

    def __init__(self, database: StateDatabase, *, read_only: bool = False) -> None:
        self._database = database
        self._read_only = read_only
        self._transaction: AbstractContextManager[sqlite3.Connection] | None = None
        self._connection_value: sqlite3.Connection | None = None
        self.tasks = TaskRepository(self)
        self.execution = ExecutionRepository(self)
        self.completion = CompletionRepository(self)
        self.history = HistoryRepository(self)
        self.history_records = HistoryRecordRepository(self)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection_value is None:
            raise StateUnitOfWorkError("state unit of work is not active")
        return self._connection_value

    def __enter__(self) -> StateUnitOfWork:
        if self._transaction is not None:
            raise StateUnitOfWorkError("state unit of work is already active")
        transaction = self._database.transaction(read_only=self._read_only)
        self._transaction = transaction
        try:
            self._connection_value = transaction.__enter__()
        except BaseException:
            self._transaction = None
            raise
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None:
        transaction = self._transaction
        if transaction is None:
            raise StateUnitOfWorkError("state unit of work is not active")
        self._connection_value = None
        self._transaction = None
        return transaction.__exit__(exc_type, exc_value, traceback)


def _validate_completion_identity(site_id: str, content_key: str) -> None:
    if not site_id.strip():
        raise ValueError("site_id must not be empty")
    if not content_key.strip():
        raise ValueError("content_key must not be empty")
