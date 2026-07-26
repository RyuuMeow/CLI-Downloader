from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from unittest import TestCase

from cli_downloader.cli.history import (
    history_record_to_dict,
    history_records_to_dicts,
    print_history_record_detail,
    print_history_record_summary,
)
from cli_downloader.cli.i18n import TranslationService
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind


def _record(
    record_id: str,
    *,
    kind: HistoryKind = HistoryKind.ITEM,
    parent_record_id: str | None = None,
    title: str = "Artwork",
    created_at: datetime = datetime(2026, 7, 9, tzinfo=UTC),
) -> DownloadHistoryRecord:
    return DownloadHistoryRecord(
        record_id=record_id,
        task_id=record_id,
        kind=kind,
        parent_record_id=parent_record_id,
        plugin_id="pixiv_plugin",
        site_id="pixiv",
        title=title,
        source_url="https://pixiv.example/art/1",
        status="completed",
        created_at=created_at,
        finished_at=created_at,
        completed_files=2,
        total_files=2,
        output_files=("C:/downloads/art-1.jpg", "C:/downloads/art-2.jpg"),
    )


class HistoryProjectionSerializationTest(TestCase):
    def test_text_and_json_use_the_same_identity_fields(self) -> None:
        record = _record("child-1", kind=HistoryKind.CHILD, parent_record_id="batch-1")
        payload = history_record_to_dict(record)

        stream = StringIO()
        with redirect_stdout(stream):
            print_history_record_detail(record)
        output = stream.getvalue()

        self.assertIn("Child (pixiv)", output)
        self.assertIn(record.title, output)
        self.assertIn(payload["download_location"], output)
        self.assertEqual(history_records_to_dicts((record,))[0], payload)

    def test_json_contains_stable_record_fields(self) -> None:
        payload = history_records_to_dicts((_record("item-1"),))[0]
        self.assertEqual(payload["task_id"], "item-1")
        self.assertEqual(payload["kind"], "item")
        self.assertEqual(payload["completed_files"], 2)
        self.assertEqual(len(payload["output_files"]), 2)


class HistoryProjectionSummaryTest(TestCase):
    def test_summary_translates_human_labels(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            print_history_record_summary(
                (_record("item-1"),),
                1,
                20,
                "",
                translator=TranslationService("zh-TW"),
            )

        output = stream.getvalue()
        self.assertIn("下載紀錄", output)
        self.assertIn("任務 ID", output)
        self.assertIn("紀錄總數: 1", output)

    def test_child_records_are_hidden_unless_requested(self) -> None:
        record = _record("child-1", kind=HistoryKind.CHILD)
        hidden = StringIO()
        with redirect_stdout(hidden):
            print_history_record_summary((record,), 1, 20, "artwork")
        shown = StringIO()
        with redirect_stdout(shown):
            print_history_record_summary((record,), 1, 20, "artwork", show_child=True)

        self.assertIn("Total entries: 0", hidden.getvalue())
        self.assertIn("Child (pixiv)", shown.getvalue())

    def test_sorting_and_pagination_are_deterministic(self) -> None:
        timestamp = datetime(2026, 7, 9, tzinfo=UTC)
        records = (
            _record("b", title="B", created_at=timestamp),
            _record("a", title="A", created_at=timestamp),
            _record("c", title="C", created_at=timestamp),
        )
        stream = StringIO()
        with redirect_stdout(stream):
            print_history_record_summary(records, 1, 2, "")

        output = stream.getvalue()
        self.assertIn("Page 1/2", output)
        self.assertIn("Total entries: 3", output)

    def test_summary_title_is_abbreviated(self) -> None:
        title = "A" * 20 + "B"
        stream = StringIO()
        with redirect_stdout(stream):
            print_history_record_summary((_record("long", title=title),), 1, 20, "")

        output = stream.getvalue()
        self.assertIn("A" * 20 + "...", output)
        self.assertNotIn(title, output)

    def test_detail_title_is_not_abbreviated(self) -> None:
        title = "A" * 20 + "B"
        stream = StringIO()
        with redirect_stdout(stream):
            print_history_record_detail(_record("long", title=title))

        self.assertIn(title, stream.getvalue())
