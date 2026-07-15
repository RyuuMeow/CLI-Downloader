"""Batch root storage adapter backed by the unified StateDatabase."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    AttemptData,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    BatchSnapshot,
    BatchSummary,
    BatchCounts,
    DuplicateKind,
    DownloadAsset,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureOrigin,
    FailureStage,
    Checkpoint,
    CheckpointData,
    RetryPlan,
    RetryScope,
    validate_asset_transition,
    validate_child_transition,
    derive_batch_snapshot,
    derive_batch_execution_status,
    validate_entry_transition,
)
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.errors import BatchConflictError, BatchNotFoundError
from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_records import DownloadHistoryRecord
from cli_downloader.core.task_tree import (
    BatchChildPage,
    BatchChildSummary,
    TaskProgress,
    TaskState,
)
from cli_downloader.storage.state_database import StateDatabase, StateUnitOfWork
from cli_downloader.storage.task_store import TaskRecord


class StateBatchStore:
    """Persist public Batch roots and ordered entries in the execution graph."""

    def __init__(self, database: StateDatabase) -> None:
        self._database = database

    @property
    def database(self) -> StateDatabase:
        return self._database

    def create_batch(self, parent: BatchParent, entries: tuple[BatchEntry, ...]) -> None:
        if type(parent) is not BatchParent or any(
            type(entry) is not BatchEntry for entry in entries
        ):
            raise TypeError("parent and entries must use core batch contracts")
        entries_by_id = {entry.entry_id: entry for entry in entries}
        if len(entries_by_id) != len(entries) or set(entries_by_id) != set(parent.entry_ids):
            raise BatchConflictError("entries must contain each parent entry exactly once")
        if any(entry.parent_id != parent.parent_id for entry in entries):
            raise BatchConflictError("entries must belong to the parent")
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                unit_of_work.execution.create_batch(parent.parent_id, parent.title)
                for position, entry_id in enumerate(parent.entry_ids):
                    entry = entries_by_id[entry_id]
                    unit_of_work.execution.add_entry(
                        parent.parent_id,
                        entry.entry_id,
                        position,
                        entry.status.value,
                        entry.to_dict(),
                        plugin_id=entry.plugin_id,
                        site_id=entry.site_id,
                        candidate_id=entry.candidate_id,
                        child_id=entry.child_id,
                    )
        except Exception as error:
            if isinstance(error, BatchConflictError):
                raise
            raise BatchConflictError(f"batch identity conflict: {parent.parent_id}") from error

    def list_parent_ids(self) -> tuple[str, ...]:
        with StateUnitOfWork(self._database) as unit_of_work:
            return unit_of_work.execution.list_batch_ids()

    def find_entry_ids(self, task_ids: tuple[str, ...]) -> frozenset[str]:
        """Return matching Batch entry IDs in one bounded SQL query.

        This is used only to hide legacy entry Task rows from the root view;
        it avoids one ``get_entry`` transaction per Task record.
        """

        if not task_ids:
            return frozenset()
        placeholders = ",".join("?" for _ in task_ids)
        with StateUnitOfWork(self._database) as unit_of_work:
            rows = unit_of_work.connection.execute(
                f"SELECT entry_id FROM execution_entries WHERE entry_id IN ({placeholders})",
                task_ids,
            ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def get_summary(self, parent_id: str) -> BatchSummary:
        """Read a Batch root aggregate without decoding its child graph."""

        with StateUnitOfWork(self._database) as unit_of_work:
            connection = unit_of_work.connection
            root = connection.execute(
                "SELECT title FROM execution_batches WHERE task_id = ?",
                (parent_id,),
            ).fetchone()
            if root is None:
                raise BatchNotFoundError(f"batch parent not found: {parent_id}")
            count_rows = connection.execute(
                """
                SELECT entry.status, child.status, COUNT(*)
                FROM execution_entries AS entry
                LEFT JOIN execution_children AS child ON child.entry_id = entry.entry_id
                WHERE entry.task_id = ?
                GROUP BY entry.status, child.status
                """,
                (parent_id,),
            ).fetchall()
            first = connection.execute(
                """
                SELECT entry_id, payload_json
                FROM execution_entries
                WHERE task_id = ?
                ORDER BY position
                LIMIT 1
                """,
                (parent_id,),
            ).fetchone()
            byte_row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN asset.status = ? THEN 1 ELSE 0 END),
                    COALESCE(SUM(CASE
                        WHEN json_type(asset.payload_json, '$.metadata.torrent_checkpoint.completed_bytes')
                             IN ('integer', 'real')
                        THEN CAST(json_extract(asset.payload_json,
                             '$.metadata.torrent_checkpoint.completed_bytes') AS INTEGER)
                        ELSE 0 END), 0),
                    COALESCE(SUM(CASE
                        WHEN json_type(asset.payload_json, '$.metadata.torrent_checkpoint.total_bytes')
                             IN ('integer', 'real')
                        THEN CAST(json_extract(asset.payload_json,
                             '$.metadata.torrent_checkpoint.total_bytes') AS INTEGER)
                        ELSE 0 END), 0),
                    SUM(CASE
                        WHEN json_type(asset.payload_json, '$.metadata.torrent_checkpoint.total_bytes')
                             IN ('integer', 'real')
                        THEN 0 ELSE 1 END)
                FROM execution_assets AS asset
                JOIN execution_children AS child ON child.child_id = asset.child_id
                WHERE child.task_id = ?
                """,
                (AssetStatus.COMPLETED.value, parent_id),
            ).fetchone()

        counts = _batch_counts_from_rows(count_rows)
        entry = _entry_from_payload(first[1]) if first is not None else None
        asset_count = int(byte_row[0] or 0)
        completed_asset_count = int(byte_row[1] or 0)
        missing_total = int(byte_row[4] or 0)
        return BatchSummary(
            parent_id=parent_id,
            title=root[0],
            first_entry_id=entry.entry_id if entry is not None else None,
            site_id=entry.site_id if entry is not None else None,
            source_url=entry.source_url if entry is not None else None,
            counts=counts,
            execution_status=derive_batch_execution_status(counts),
            completed_files=completed_asset_count,
            total_files=asset_count,
            completed_bytes=int(byte_row[2] or 0),
            total_bytes=(int(byte_row[3] or 0) if asset_count > 0 and missing_total == 0 else None),
        )

    def list_child_summaries(
        self,
        parent_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> BatchChildPage:
        """Return one bounded Batch child page using bulk aggregate queries."""

        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        limit = min(limit, 500)
        with StateUnitOfWork(self._database) as unit_of_work:
            connection = unit_of_work.connection
            exists = connection.execute(
                "SELECT 1 FROM execution_batches WHERE task_id = ?", (parent_id,)
            ).fetchone()
            if exists is None:
                raise BatchNotFoundError(f"batch parent not found: {parent_id}")
            total_row = connection.execute(
                "SELECT COUNT(*) FROM execution_entries WHERE task_id = ?", (parent_id,)
            ).fetchone()
            total_count = int(total_row[0] or 0)
            rows = connection.execute(
                """
                SELECT entry.entry_id, entry.position, entry.status, entry.payload_json,
                       entry.child_id, child.status, child.payload_json
                FROM execution_entries AS entry
                LEFT JOIN execution_children AS child ON child.entry_id = entry.entry_id
                WHERE entry.task_id = ?
                ORDER BY entry.position
                LIMIT ? OFFSET ?
                """,
                (parent_id, limit, offset),
            ).fetchall()
            child_ids = tuple(str(row[4]) for row in rows if row[4] is not None)
            asset_counts = _asset_summary_rows(connection, child_ids)
            attempt_counts = _attempt_summary_rows(connection, child_ids)

        summaries = tuple(
            _child_summary_from_row(row, asset_counts, attempt_counts)
            for row in rows
        )
        return BatchChildPage(
            items=summaries,
            total_count=total_count,
            offset=offset,
            limit=limit,
        )

    def create_task_and_batch(
        self,
        task: TaskRecord,
        parent: BatchParent,
        entries: tuple[BatchEntry, ...],
        history_events: tuple[TaskEvent, ...],
        history_records: tuple[DownloadHistoryRecord, ...] = (),
    ) -> BatchSnapshot:
        """Create Task, execution root, and audit events as one state transition."""
        self._validate_batch(parent, entries)
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                unit_of_work.tasks.upsert(task)
                unit_of_work.execution.create_batch(parent.parent_id, parent.title)
                for position, entry in enumerate(entries):
                    unit_of_work.execution.add_entry(
                        parent.parent_id,
                        entry.entry_id,
                        position,
                        entry.status.value,
                        entry.to_dict(),
                        plugin_id=entry.plugin_id,
                        site_id=entry.site_id,
                        candidate_id=entry.candidate_id,
                        child_id=entry.child_id,
                    )
                for history_record in history_records:
                    unit_of_work.history_records.upsert(history_record)
                unit_of_work.history.append_many(history_events)
                return derive_batch_snapshot(parent, entries, ())
        except Exception as error:
            if isinstance(error, BatchConflictError):
                raise
            raise BatchConflictError(f"batch identity conflict: {parent.parent_id}") from error

    def rebuild_task_and_batch(
        self,
        task: TaskRecord,
        parent: BatchParent,
        entries: tuple[BatchEntry, ...],
        history_events: tuple[TaskEvent, ...] = (),
        history_record: DownloadHistoryRecord | None = None,
    ) -> BatchSnapshot:
        """Replace execution checkpoints in place while preserving public identity."""
        self._validate_batch(parent, entries)
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                current = unit_of_work.tasks.get(task.task_id)
                if current is None or current.task_id != parent.parent_id:
                    raise BatchNotFoundError(f"batch parent not found: {parent.parent_id}")
                if current.active_run_id is not None:
                    raise BatchConflictError(f"batch is already running: {parent.parent_id}")
                if not unit_of_work.execution.delete_batch(parent.parent_id):
                    raise BatchNotFoundError(f"batch parent not found: {parent.parent_id}")
                unit_of_work.tasks.upsert(task)
                unit_of_work.execution.create_batch(parent.parent_id, parent.title)
                for position, entry in enumerate(entries):
                    unit_of_work.execution.add_entry(
                        parent.parent_id,
                        entry.entry_id,
                        position,
                        entry.status.value,
                        entry.to_dict(),
                        plugin_id=entry.plugin_id,
                        site_id=entry.site_id,
                        candidate_id=entry.candidate_id,
                        child_id=entry.child_id,
                    )
                if history_record is not None:
                    unit_of_work.history_records.upsert(history_record)
                unit_of_work.history.append_many(history_events)
                return derive_batch_snapshot(parent, entries, ())
        except Exception as error:
            if isinstance(error, (BatchConflictError, BatchNotFoundError)):
                raise
            raise BatchConflictError(f"batch rebuild conflict: {parent.parent_id}") from error

    def delete_batch(self, parent_id: str) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            if not unit_of_work.execution.delete_batch(parent_id):
                raise BatchNotFoundError(f"batch parent not found: {parent_id}")

    def delete_task_and_batch(self, parent_id: str) -> None:
        """Delete a Batch Task and its owned execution graph in one transaction.

        History and Completion use soft task references, so they deliberately
        remain after the Task foreign-key cascade removes execution rows.
        """
        with StateUnitOfWork(self._database) as unit_of_work:
            if parent_id not in unit_of_work.execution.list_batch_ids():
                raise BatchNotFoundError(f"batch parent not found: {parent_id}")
            unit_of_work.tasks.delete(parent_id)

    def get_snapshot(self, parent_id: str) -> BatchSnapshot:
        with StateUnitOfWork(self._database) as unit_of_work:
            title = unit_of_work.execution.get_batch_title(parent_id)
            if title is None and parent_id not in unit_of_work.execution.list_batch_ids():
                raise BatchNotFoundError(f"batch parent not found: {parent_id}")
            entries = tuple(
                _entry_from_payload(payload_json)
                for _entry_id, _status, payload_json, _child_id in unit_of_work.execution.list_entry_payloads(
                    parent_id
                )
            )
        children = self.list_children(parent_id)
        parent = BatchParent(
            parent_id=parent_id, entry_ids=tuple(entry.entry_id for entry in entries), title=title
        )
        return derive_batch_snapshot(parent, entries, children)

    def get_entry(self, entry_id: str) -> BatchEntry | None:
        with StateUnitOfWork(self._database) as unit_of_work:
            row = unit_of_work.execution.get_entry_payload(entry_id)
        return _entry_from_payload(row[2]) if row else None

    def list_entries(self, parent_id: str) -> tuple[BatchEntry, ...]:
        with StateUnitOfWork(self._database) as unit_of_work:
            rows = unit_of_work.execution.list_entry_payloads(parent_id)
        return tuple(_entry_from_payload(row[2]) for row in rows)

    def update_entry(self, entry: BatchEntry) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            row = unit_of_work.execution.get_entry_payload(entry.entry_id)
            if row is None:
                raise BatchNotFoundError(f"batch entry not found: {entry.entry_id}")
            current = _entry_from_payload(row[2])
            if _entry_identity(current) != _entry_identity(entry):
                raise BatchConflictError("entry identity fields cannot change")
            if entry.status is BatchEntryStatus.PACKAGE_READY:
                raise BatchConflictError("package_ready entries must use attach_child")
            validate_entry_transition(current.status, entry.status)
            unit_of_work.execution.update_entry(
                entry.entry_id, entry.status.value, entry.to_dict(), entry.child_id
            )

    def attach_child(
        self,
        entry_id: str,
        child: DownloadChild,
        *,
        history_record: DownloadHistoryRecord | None = None,
    ) -> None:
        if type(child) is not DownloadChild or child.status is not DownloadChildStatus.QUEUED:
            raise BatchConflictError("new children must be queued core DownloadChild values")
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                row = unit_of_work.execution.get_entry_payload(entry_id)
                if row is None:
                    raise BatchNotFoundError(f"batch entry not found: {entry_id}")
                task_id, _status, payload_json, _child_id = row
                entry = _entry_from_payload(payload_json)
                if (
                    child.entry_id != entry.entry_id
                    or child.parent_id != entry.parent_id
                    or child.package.plugin_id != entry.plugin_id
                    or child.package.site_id != entry.site_id
                ):
                    raise BatchConflictError("child and package must match the batch entry")
                validate_entry_transition(entry.status, BatchEntryStatus.PACKAGE_READY)
                package = child.package
                unit_of_work.execution.add_package(
                    package.package_id,
                    package.version,
                    package.to_dict(),
                    plugin_id=package.plugin_id,
                    site_id=package.site_id,
                    gallery_id=package.gallery_id,
                )
                unit_of_work.execution.add_child(
                    child.child_id,
                    task_id,
                    child.entry_id,
                    package.package_id,
                    package.version,
                    child.status.value,
                    child.to_dict(),
                )
                for asset in package.assets:
                    record = AssetRecord(asset=asset)
                    unit_of_work.execution.add_asset(
                        child.child_id, asset.asset_id, record.status.value, record.to_dict()
                    )
                if history_record is not None:
                    unit_of_work.history_records.upsert(history_record)
                ready_entry = replace(
                    entry, status=BatchEntryStatus.PACKAGE_READY, child_id=child.child_id
                )
                unit_of_work.execution.update_entry(
                    entry.entry_id,
                    ready_entry.status.value,
                    ready_entry.to_dict(),
                    ready_entry.child_id,
                )
        except Exception as error:
            if isinstance(error, (BatchConflictError, BatchNotFoundError)):
                raise
            raise BatchConflictError(f"child handoff conflict: {child.child_id}") from error

    def get_package(self, package_id: str, version: int) -> DownloadPackage | None:
        with StateUnitOfWork(self._database) as unit_of_work:
            payload = unit_of_work.execution.get_package_payload(package_id, version)
        return _package_from_payload(payload) if payload else None

    def get_child(self, child_id: str) -> DownloadChild | None:
        with StateUnitOfWork(self._database) as unit_of_work:
            payload = unit_of_work.execution.get_child_payload(child_id)
        return _child_from_payload(payload) if payload else None

    def list_children(self, parent_id: str) -> tuple[DownloadChild, ...]:
        with StateUnitOfWork(self._database) as unit_of_work:
            payloads = unit_of_work.execution.list_child_payloads(parent_id)
        return tuple(_child_from_payload(payload) for payload in payloads)

    def update_child(self, child: DownloadChild) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            payload = unit_of_work.execution.get_child_payload(child.child_id)
            if payload is None:
                raise BatchNotFoundError(f"download child not found: {child.child_id}")
            current = _child_from_payload(payload)
            if (current.parent_id, current.entry_id, current.package) != (
                child.parent_id,
                child.entry_id,
                child.package,
            ):
                raise BatchConflictError("child identity and package cannot change")
            validate_child_transition(current.status, child.status)
            unit_of_work.execution.update_child(child.child_id, child.status.value, child.to_dict())

    def list_assets(self, child_id: str) -> tuple[AssetRecord, ...]:
        with StateUnitOfWork(self._database) as unit_of_work:
            payloads = unit_of_work.execution.list_asset_payloads(child_id)
        return tuple(_asset_record_from_payload(payload) for payload in payloads)

    def update_asset(self, child_id: str, record: AssetRecord) -> None:
        with StateUnitOfWork(self._database) as unit_of_work:
            payload = unit_of_work.execution.get_asset_payload(child_id, record.asset.asset_id)
            if payload is None:
                raise BatchNotFoundError(f"download asset not found: {record.asset.asset_id}")
            current = _asset_record_from_payload(payload)
            if current.asset != record.asset:
                raise BatchConflictError("asset identity and request data cannot change")
            if current.status is not record.status:
                validate_asset_transition(current.status, record.status)
            unit_of_work.execution.update_asset(
                child_id, record.asset.asset_id, record.status.value, record.to_dict()
            )

    def replace_child_package(self, child_id: str, package: DownloadPackage) -> DownloadChild:
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                payload = unit_of_work.execution.get_child_payload(child_id)
                if payload is None:
                    raise BatchNotFoundError(f"download child not found: {child_id}")
                child = _child_from_payload(payload)
                current = child.package
                if (
                    package.package_id != current.package_id
                    or package.version != current.version + 1
                    or (package.plugin_id, package.site_id, package.gallery_id)
                    != (current.plugin_id, current.site_id, current.gallery_id)
                ):
                    raise BatchConflictError(
                        "replacement package must be the next version of the same gallery"
                    )
                old_records = {
                    record.asset.asset_id: record
                    for record in (
                        _asset_record_from_payload(payload)
                        for payload in unit_of_work.execution.list_asset_payloads(child_id)
                    )
                }
                new_assets = {asset.asset_id: asset for asset in package.assets}
                missing = sorted(
                    asset_id
                    for asset_id, record in old_records.items()
                    if record.status is not AssetStatus.COMPLETED and asset_id not in new_assets
                )
                if missing:
                    raise BatchConflictError(
                        "replacement package omitted unfinished assets: " + ", ".join(missing)
                    )
                records: list[AssetRecord] = []
                for asset in package.assets:
                    old = old_records.get(asset.asset_id)
                    if old is not None and old.status is AssetStatus.COMPLETED:
                        records.append(old)
                    elif old is not None:
                        records.append(
                            AssetRecord(
                                asset=asset,
                                attempt_count=old.attempt_count,
                                partial_path=old.partial_path,
                                content_sha256=old.content_sha256,
                            )
                        )
                    else:
                        records.append(AssetRecord(asset=asset))
                records.extend(
                    record
                    for asset_id, record in old_records.items()
                    if record.status is AssetStatus.COMPLETED and asset_id not in new_assets
                )
                unit_of_work.execution.add_package(
                    package.package_id,
                    package.version,
                    package.to_dict(),
                    plugin_id=package.plugin_id,
                    site_id=package.site_id,
                    gallery_id=package.gallery_id,
                )
                updated = replace(child, package=package)
                unit_of_work.execution.replace_child_package(
                    child_id,
                    package.package_id,
                    package.version,
                    updated.to_dict(),
                    tuple(
                        (record.asset.asset_id, record.status.value, record.to_dict())
                        for record in records
                    ),
                )
                return updated
        except Exception as error:
            if isinstance(error, (BatchConflictError, BatchNotFoundError)):
                raise
            raise BatchConflictError(
                f"package replacement conflict: {package.package_id}@{package.version}"
            ) from error

    def append_attempt(self, attempt: AttemptData) -> None:
        try:
            with StateUnitOfWork(self._database) as unit_of_work:
                child_id = (
                    attempt.target_id
                    if unit_of_work.execution.get_child_payload(attempt.target_id) is not None
                    else None
                )
                unit_of_work.execution.append_attempt(
                    attempt.attempt_id,
                    attempt.target_id,
                    attempt.number,
                    attempt.to_dict(),
                    child_id=child_id,
                )
        except Exception as error:
            raise BatchConflictError(f"attempt identity conflict: {attempt.attempt_id}") from error

    def list_attempts(self, target_id: str) -> tuple[AttemptData, ...]:
        with StateUnitOfWork(self._database) as unit_of_work:
            payloads = unit_of_work.execution.list_attempt_payloads(target_id)
        return tuple(_attempt_from_payload(payload) for payload in payloads)

    def finalize_child(
        self,
        child: DownloadChild,
        *,
        completion_identity: tuple[str, str] | None = None,
        history_events: tuple[TaskEvent, ...] = (),
        history_record: DownloadHistoryRecord | None = None,
    ) -> None:
        """Commit a terminal child outcome, duplicate evidence, and audit events together."""
        with StateUnitOfWork(self._database) as unit_of_work:
            if not unit_of_work.execution.update_child(
                child.child_id, child.status.value, child.to_dict()
            ):
                raise BatchNotFoundError(f"download child not found: {child.child_id}")
            if completion_identity is not None:
                unit_of_work.completion.record_completed(*completion_identity)
            if history_record is not None:
                unit_of_work.history_records.upsert(history_record)
            unit_of_work.history.append_many(history_events)

    def find_duplicates(
        self, site_id: str, candidate_ids: tuple[str, ...]
    ) -> dict[str, DuplicateKind]:
        if not candidate_ids:
            return {}
        placeholders = ",".join("?" for _ in candidate_ids)
        with StateUnitOfWork(self._database) as unit_of_work:
            rows = unit_of_work.connection.execute(
                f"""
                SELECT entry.candidate_id, entry.status, child.status
                FROM execution_entries AS entry
                LEFT JOIN execution_children AS child ON child.entry_id = entry.entry_id
                WHERE entry.site_id = ? AND entry.candidate_id IN ({placeholders}) AND entry.status != ?
                """,
                (site_id, *candidate_ids, BatchEntryStatus.DUPLICATE_SKIPPED.value),
            ).fetchall()
        active = {BatchEntryStatus.PENDING_RESOLUTION.value, BatchEntryStatus.RESOLVING.value}
        duplicates: dict[str, DuplicateKind] = {}
        for candidate_id, entry_status, child_status in rows:
            if entry_status in active or child_status in {
                DownloadChildStatus.QUEUED.value,
                DownloadChildStatus.DOWNLOADING.value,
            }:
                duplicates[candidate_id] = DuplicateKind.ACTIVE
            elif child_status == DownloadChildStatus.COMPLETED.value:
                duplicates.setdefault(candidate_id, DuplicateKind.COMPLETED)
        return duplicates

    @staticmethod
    def _validate_batch(parent: BatchParent, entries: tuple[BatchEntry, ...]) -> None:
        if type(parent) is not BatchParent or any(
            type(entry) is not BatchEntry for entry in entries
        ):
            raise TypeError("parent and entries must use core batch contracts")
        entries_by_id = {entry.entry_id: entry for entry in entries}
        if len(entries_by_id) != len(entries) or set(entries_by_id) != set(parent.entry_ids):
            raise BatchConflictError("entries must contain each parent entry exactly once")
        if any(entry.parent_id != parent.parent_id for entry in entries):
            raise BatchConflictError("entries must belong to the parent")


def _entry_from_payload(payload_json: object) -> BatchEntry:
    value = json.loads(payload_json)
    return BatchEntry(
        entry_id=value["entry_id"],
        parent_id=value["parent_id"],
        plugin_id=value["plugin_id"],
        site_id=value["site_id"],
        candidate_id=value["candidate_id"],
        source_operation_id=value["source_operation_id"],
        source_url=value["source_url"],
        title=value.get("title"),
        strategy=DownloadStrategy(value.get("strategy", DownloadStrategy.AUTO.value)),
        status=BatchEntryStatus(value["status"]),
        child_id=value["child_id"],
        failure=_failure_from_payload(value.get("failure")),
    )


def _failure_from_payload(value: dict[str, object] | None) -> FailureData | None:
    if value is None:
        return None
    return FailureData(
        origin=FailureOrigin(value["origin"]),
        stage=FailureStage(value["stage"]),
        code=value["code"],
        message=value["message"],
        retryable=value["retryable"],
        details=value["details"],
    )


def _asset_from_payload(value: dict[str, object]) -> DownloadAsset:
    return DownloadAsset(
        site_id=value["site_id"],
        gallery_id=value["gallery_id"],
        plugin_asset_key=value["plugin_asset_key"],
        url=value["url"],
        filename=value["filename"],
        index=value["index"],
        headers=value["headers"],
        referrer=value["referrer"],
        metadata=value["metadata"],
    )


def _package_from_payload(payload_json: object) -> DownloadPackage:
    value = json.loads(payload_json)
    return DownloadPackage(
        package_id=value["package_id"],
        version=value["version"],
        plugin_id=value["plugin_id"],
        site_id=value["site_id"],
        gallery_id=value["gallery_id"],
        source_operation_id=value["source_operation_id"],
        source_url=value["source_url"],
        strategy=DownloadStrategy(value["strategy"]),
        assets=tuple(_asset_from_payload(asset) for asset in value["assets"]),
        title=value.get("title"),
        expires_at=(
            datetime.fromisoformat(value["expires_at"]) if value["expires_at"] is not None else None
        ),
        refresh_on_status=tuple(value["refresh_on_status"]),
        secret_refs=value["secret_refs"],
        folder_template=value.get("folder_template"),
        folder_fields=value.get("folder_fields", {}),
    )


def _child_from_payload(payload_json: object) -> DownloadChild:
    value = json.loads(payload_json)
    return DownloadChild(
        child_id=value["child_id"],
        parent_id=value["parent_id"],
        entry_id=value["entry_id"],
        package=_package_from_payload(json.dumps(value["package"])),
        status=DownloadChildStatus(value["status"]),
        failure=_failure_from_payload(value["failure"]),
    )


def _asset_record_from_payload(payload_json: object) -> AssetRecord:
    value = json.loads(payload_json)
    return AssetRecord(
        asset=_asset_from_payload(value["asset"]),
        status=AssetStatus(value["status"]),
        attempt_count=value["attempt_count"],
        failure=_failure_from_payload(value["failure"]),
        partial_path=value["partial_path"],
        final_path=value["final_path"],
        content_sha256=value["content_sha256"],
        metadata=value.get("metadata", {}),
    )


def _retry_plan_from_payload(value: dict[str, object] | None) -> RetryPlan | None:
    if value is None:
        return None
    return RetryPlan(
        plan_id=value["plan_id"],
        start_at=FailureStage(value["start_at"]),
        scope=RetryScope(value["scope"]),
        target_ids=tuple(value["target_ids"]),
        reason=value["reason"],
        package_version=value["package_version"],
        automatic_refresh_count=value["automatic_refresh_count"],
    )


def _attempt_from_payload(payload_json: object) -> AttemptData:
    value = json.loads(payload_json)
    return AttemptData(
        attempt_id=value["attempt_id"],
        target_id=value["target_id"],
        number=value["number"],
        checkpoints=tuple(
            CheckpointData(
                checkpoint=Checkpoint(checkpoint["checkpoint"]),
                target_id=checkpoint["target_id"],
                details=checkpoint["details"],
            )
            for checkpoint in value["checkpoints"]
        ),
        retry_plan=_retry_plan_from_payload(value["retry_plan"]),
        failure=_failure_from_payload(value["failure"]),
    )


def _entry_identity(entry: BatchEntry) -> tuple[str, ...]:
    return (
        entry.entry_id,
        entry.parent_id,
        entry.plugin_id,
        entry.site_id,
        entry.candidate_id,
        entry.source_operation_id,
        entry.source_url,
    )


def _batch_counts_from_rows(rows: object) -> BatchCounts:
    counts = {name: 0 for name in BatchCounts.__dataclass_fields__}
    for entry_status_value, child_status_value, raw_count in rows:
        count = int(raw_count)
        entry_status = BatchEntryStatus(entry_status_value)
        if entry_status is BatchEntryStatus.PENDING_RESOLUTION:
            counts["unresolved"] += count
        elif entry_status is BatchEntryStatus.RESOLVING:
            counts["resolving"] += count
        elif entry_status in {
            BatchEntryStatus.RESOLUTION_FAILED,
            BatchEntryStatus.PACKAGE_REJECTED,
        }:
            counts["rejected"] += count
        elif entry_status is BatchEntryStatus.DUPLICATE_SKIPPED:
            counts["skipped"] += count
        elif entry_status is BatchEntryStatus.CANCELLED:
            counts["cancelled"] += count
        elif child_status_value is None:
            # A package-ready entry without a joined child is an incomplete
            # handoff. Keep it visible as preparing until the next refresh.
            counts["unresolved"] += count
        else:
            child_status = DownloadChildStatus(child_status_value)
            category = {
                DownloadChildStatus.QUEUED: "queued",
                DownloadChildStatus.DOWNLOADING: "downloading",
                DownloadChildStatus.COMPLETED: "completed",
                DownloadChildStatus.PARTIALLY_COMPLETED: "failed",
                DownloadChildStatus.FAILED: "failed",
                DownloadChildStatus.CANCELLED: "cancelled",
            }[child_status]
            counts[category] += count
    return BatchCounts(**counts)


def _asset_summary_rows(connection: object, child_ids: tuple[str, ...]) -> dict[str, tuple[int, int, int, int, int]]:
    if not child_ids:
        return {}
    placeholders = ",".join("?" for _ in child_ids)
    rows = connection.execute(
        f"""
        SELECT child_id,
               COUNT(*),
               SUM(CASE WHEN status = ? THEN 1 ELSE 0 END),
               COALESCE(SUM(CASE
                   WHEN json_type(payload_json, '$.metadata.torrent_checkpoint.completed_bytes')
                        IN ('integer', 'real')
                   THEN CAST(json_extract(payload_json,
                        '$.metadata.torrent_checkpoint.completed_bytes') AS INTEGER)
                   ELSE 0 END), 0),
               COALESCE(SUM(CASE
                   WHEN json_type(payload_json, '$.metadata.torrent_checkpoint.total_bytes')
                        IN ('integer', 'real')
                   THEN CAST(json_extract(payload_json,
                        '$.metadata.torrent_checkpoint.total_bytes') AS INTEGER)
                   ELSE 0 END), 0),
               SUM(CASE
                   WHEN json_type(payload_json, '$.metadata.torrent_checkpoint.total_bytes')
                        IN ('integer', 'real')
                   THEN 0 ELSE 1 END)
        FROM execution_assets
        WHERE child_id IN ({placeholders})
        GROUP BY child_id
        """,
        (AssetStatus.COMPLETED.value, *child_ids),
    ).fetchall()
    return {
        str(row[0]): (
            int(row[1] or 0),
            int(row[2] or 0),
            int(row[3] or 0),
            int(row[4] or 0),
            int(row[5] or 0),
        )
        for row in rows
    }


def _attempt_summary_rows(connection: object, child_ids: tuple[str, ...]) -> dict[str, int]:
    if not child_ids:
        return {}
    placeholders = ",".join("?" for _ in child_ids)
    rows = connection.execute(
        f"""
        SELECT child_id, COUNT(*)
        FROM execution_attempts
        WHERE child_id IN ({placeholders})
        GROUP BY child_id
        """,
        child_ids,
    ).fetchall()
    return {str(row[0]): int(row[1] or 0) for row in rows}


def _child_summary_from_row(
    row: tuple[object, ...],
    asset_counts: dict[str, tuple[int, int, int, int, int]],
    attempt_counts: dict[str, int],
) -> BatchChildSummary:
    entry = _entry_from_payload(row[3])
    child_id = str(row[4]) if row[4] is not None else None
    if child_id is None or row[5] is None or row[6] is None:
        state = {
            BatchEntryStatus.PENDING_RESOLUTION: TaskState.PREPARING,
            BatchEntryStatus.RESOLVING: TaskState.PREPARING,
            BatchEntryStatus.RESOLUTION_FAILED: TaskState.FAILED,
            BatchEntryStatus.PACKAGE_REJECTED: TaskState.FAILED,
            BatchEntryStatus.DUPLICATE_SKIPPED: TaskState.SKIPPED,
            BatchEntryStatus.CANCELLED: TaskState.CANCELLED,
            BatchEntryStatus.PACKAGE_READY: TaskState.PREPARING,
        }[entry.status]
        details: dict[str, object] = {"preparation_status": entry.status.value}
        if entry.failure is not None:
            details["failure"] = entry.failure.to_dict()
        return BatchChildSummary(
            task_id=entry.entry_id,
            parent_id=entry.parent_id,
            state=state,
            title=entry.title or entry.candidate_id,
            progress=TaskProgress(preparing_items=int(state is TaskState.PREPARING)),
            site_id=entry.site_id,
            source_url=entry.source_url,
            details=details,
        )

    child_status = DownloadChildStatus(row[5])
    child_payload = json.loads(row[6])
    asset_count, completed_count, completed_bytes, total_bytes, missing_total = asset_counts.get(
        child_id, (0, 0, 0, 0, 0)
    )
    package_payload = child_payload.get("package", {})
    state = {
        DownloadChildStatus.QUEUED: TaskState.QUEUED,
        DownloadChildStatus.DOWNLOADING: TaskState.RUNNING,
        DownloadChildStatus.COMPLETED: TaskState.COMPLETED,
        DownloadChildStatus.PARTIALLY_COMPLETED: TaskState.PARTIALLY_COMPLETED,
        DownloadChildStatus.FAILED: TaskState.FAILED,
        DownloadChildStatus.CANCELLED: TaskState.CANCELLED,
    }[child_status]
    details = {
        "preparation_status": entry.status.value,
        "child_id": child_id,
        "package_id": package_payload.get("package_id"),
        "package_version": package_payload.get("version"),
        "attempt_count": attempt_counts.get(child_id, 0),
    }
    failure = entry.failure or _failure_from_payload(child_payload.get("failure"))
    if failure is not None:
        details["failure"] = failure.to_dict()
    return BatchChildSummary(
        task_id=entry.entry_id,
        parent_id=entry.parent_id,
        state=state,
        title=entry.title or entry.candidate_id,
        progress=TaskProgress(
            completed_files=completed_count,
            total_files=asset_count,
            completed_bytes=completed_bytes,
            total_bytes=(total_bytes if asset_count > 0 and missing_total == 0 else None),
        ),
        site_id=entry.site_id,
        source_url=entry.source_url,
        details=details,
    )
