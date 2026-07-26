from __future__ import annotations

from cli_downloader.storage.task_store import TaskRecord


def task_record_to_dict(record: TaskRecord) -> dict[str, object]:
    return {
        "task_id": record.task_id,
        "status": record.status.value,
        "site_id": record.site_id,
        "title": record.title,
        "source_url": record.source_url,
        "metadata": record.metadata,
    }


def task_records_to_dicts(records: tuple[TaskRecord, ...]) -> list[dict[str, object]]:
    return [task_record_to_dict(record) for record in records]
