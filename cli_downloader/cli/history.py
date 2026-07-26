from __future__ import annotations

from pathlib import Path

from cli_downloader.cli.formatting import abbreviate_summary_title
from cli_downloader.cli.i18n import TranslationService, current_translator, translate_status
from cli_downloader.cli.table import TableColumn, TableSpec, print_rich_table
from cli_downloader.core.history_records import DownloadHistoryRecord


def history_record_to_dict(record: DownloadHistoryRecord) -> dict[str, object]:
    """Serialize the same durable projection used by text and JSON output."""
    output_files = list(record.output_files)
    location = str(Path(output_files[0]).parent) if output_files else None
    return {
        "record_id": record.record_id,
        "task_id": record.task_id,
        "kind": record.kind.value,
        "parent_history_id": record.parent_record_id,
        "entry_id": record.entry_id,
        "plugin_id": record.plugin_id,
        "site_id": record.site_id,
        "title": record.title,
        "source_url": record.source_url,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "completed_files": record.completed_files,
        "total_files": record.total_files,
        "output_files": output_files,
        "download_location": location,
        "run_generation": record.run_generation,
    }


def history_records_to_dicts(
    records: tuple[DownloadHistoryRecord, ...],
) -> list[dict[str, object]]:
    return [history_record_to_dict(record) for record in records]


def print_history_record_summary(
    records: tuple[DownloadHistoryRecord, ...],
    page: int,
    page_size: int,
    query: str,
    show_child: bool = False,
    *,
    translator: TranslationService | None = None,
) -> None:
    translator = translator or current_translator()
    query_lower = query.lower()
    filtered = tuple(
        record
        for record in records
        if (show_child or record.kind.value != "child")
        and (
            not query_lower
            or query_lower in (record.title or "").lower()
            or query_lower in (record.site_id or "").lower()
            or query_lower in (record.plugin_id or "").lower()
        )
    )
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    page_records = filtered[(page - 1) * page_size : page * page_size]

    rows: list[tuple[str, ...]] = []
    for record in page_records:
        timestamp = record.finished_at or record.created_at
        if timestamp.tzinfo is None:
            from datetime import timezone

            timestamp = timestamp.replace(tzinfo=timezone.utc)
        rows.append(
            (
                record.task_id,
                f"{record.kind.value.capitalize()} ({record.site_id or '-'})",
                abbreviate_summary_title(record.title or "-"),
                timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    print_rich_table(
        TableSpec(
            title=f"{translator('table.download_history')} (Page {page}/{total_pages})",
            columns=(
                TableColumn(translator("table.task_id"), style="cyan"),
                TableColumn(translator("table.type"), style="magenta"),
                TableColumn(translator("table.title"), style="green"),
                TableColumn(translator("table.time"), style="blue"),
            ),
            rows=tuple(rows),
        )
    )
    print(f"{translator('table.total_entries')}: {len(filtered)}")


def print_history_record_detail(
    record: DownloadHistoryRecord,
    *,
    translator: TranslationService | None = None,
) -> None:
    translator = translator or current_translator()
    timestamp = record.finished_at or record.created_at
    if timestamp.tzinfo is None:
        from datetime import timezone

        timestamp = timestamp.replace(tzinfo=timezone.utc)
    location = str(Path(record.output_files[0]).parent) if record.output_files else "-"
    rows = (
        (translator("table.task_id"), record.task_id),
        (translator("table.type"), f"{record.kind.value.capitalize()} ({record.site_id or '-'})"),
        (translator("table.title"), record.title or "-"),
        (translator("table.time"), timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")),
        (translator("table.status"), translate_status(record.status, translator)),
        (translator("table.file_count"), str(record.completed_files)),
        (translator("table.source_url"), record.source_url or "-"),
        (translator("table.download_location"), location),
    )
    print_rich_table(
        TableSpec(
            title=f"{translator('table.task_detail')}: {record.task_id}",
            show_header=False,
            columns=(
                TableColumn("Key", style="cyan", justify="right", no_wrap=False),
                TableColumn("Value", style="green", no_wrap=False, overflow="fold"),
            ),
            rows=rows,
        )
    )
