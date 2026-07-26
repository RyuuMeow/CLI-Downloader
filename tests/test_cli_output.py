from contextlib import redirect_stdout
from datetime import UTC
from io import StringIO
from unittest import TestCase
from unittest.mock import patch

from cli_downloader.cli._output import (
    _asset_to_dict,
    _plugin_roots,
    _print_generic_download_error,
    _print_task_list,
    _report_progress,
    _task_created_at,
    _task_progress_text,
    _task_summary,
)
from cli_downloader.cli.formatting import abbreviate_summary_title, display_width, truncate_display
from cli_downloader.cli.i18n import TranslationService
from cli_downloader.cli.rendering import RenderMode
from cli_downloader.core.errors import CLIDownloaderError
from cli_downloader.core.models import Asset, AssetKind
from cli_downloader.core.operations import GalleryData
from cli_downloader.core.task_tree import TaskNode, TaskState, TaskProgress, TaskKind


def _make_asset(
    url: str = "https://example.test/file.jpg",
    kind: AssetKind = AssetKind.IMAGE,
    filename: str = "file.jpg",
    metadata: dict | None = None,
) -> Asset:
    return Asset(
        url=url,
        kind=kind,
        filename=filename,
        metadata=metadata or {},
    )


def _make_task_node(
    task_id: str = "task-1",
    title: str = "Test Gallery",
    state: TaskState = TaskState.COMPLETED,
    kind: TaskKind = TaskKind.ITEM,
    completed_files: int = 10,
    total_files: int | None = 10,
    preparing_items: int = 0,
    completed_bytes: int = 0,
    total_bytes: int | None = None,
    created_at: str | None = "2025-06-15T12:00:00+00:00",
) -> TaskNode:
    progress = TaskProgress(
        completed_files=completed_files,
        total_files=total_files,
        preparing_items=preparing_items,
        completed_bytes=completed_bytes,
        total_bytes=total_bytes,
    )
    details: dict[str, object] = {}
    if created_at is not None:
        details["created_at"] = created_at
    return TaskNode(
        task_id=task_id,
        kind=kind,
        title=title,
        state=state,
        progress=progress,
        details=details,
    )


class TestAssetToDict(TestCase):
    def test_basic_asset(self) -> None:
        asset = _make_asset()
        result = _asset_to_dict(asset)
        self.assertEqual(result["url"], "https://example.test/file.jpg")
        self.assertEqual(result["kind"], "image")
        self.assertEqual(result["filename"], "file.jpg")

    def test_asset_with_metadata(self) -> None:
        asset = _make_asset(metadata={"asset_stage": "original", "index": 3})
        result = _asset_to_dict(asset)
        self.assertEqual(result["stage"], "original")
        self.assertEqual(result["index"], 3)

    def test_asset_without_optional_metadata(self) -> None:
        asset = _make_asset()
        result = _asset_to_dict(asset)
        self.assertIsNone(result["stage"])
        self.assertIsNone(result["index"])


class TestTaskSummary(TestCase):
    def test_summary_includes_basic_fields(self) -> None:
        node = _make_task_node()
        result = _task_summary(node)
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["type"], "item")
        self.assertEqual(result["name"], "Test Gallery")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["created_at"], "2025-06-15T12:00:00+00:00")

    def test_summary_progress_text(self) -> None:
        node = _make_task_node(completed_files=5, total_files=10)
        result = _task_summary(node)
        self.assertEqual(result["progress"], "5/10")


class TestSummaryTitleFormatting(TestCase):
    def test_title_at_limit_is_unchanged(self) -> None:
        title = "A" * 20
        self.assertEqual(abbreviate_summary_title(title), title)

    def test_long_title_keeps_first_twenty_characters(self) -> None:
        title = "A" * 20 + "B" * 5
        self.assertEqual(abbreviate_summary_title(title), "A" * 20 + "...")

    def test_unicode_title_keeps_summary_character_limit(self) -> None:
        title = "畫" * 21
        self.assertEqual(abbreviate_summary_title(title), "畫" * 20 + "...")

    def test_display_width_handles_wide_and_combining_text(self) -> None:
        self.assertEqual(display_width("畫"), 2)
        self.assertEqual(display_width("e\u0301"), 1)
        self.assertEqual(truncate_display("畫" * 8, 7), "畫" * 3 + "…")

    def test_task_summary_table_abbreviates_title(self) -> None:
        title = "A" * 20 + "B"
        stream = StringIO()
        from rich.console import Console

        with redirect_stdout(stream):
            with patch("rich.console.Console", side_effect=lambda: Console(width=200)):
                _print_task_list((_make_task_node(title=title),))

        output = stream.getvalue()
        self.assertIn("A" * 20 + "...", output)
        self.assertNotIn(title, output)

    def test_task_summary_table_translates_human_labels(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            _print_task_list(
                (_make_task_node(state=TaskState.COMPLETED),),
                translator=TranslationService("zh-TW"),
            )

        output = stream.getvalue()
        self.assertIn("任務", output)
        self.assertIn("任務 ID", output)
        self.assertIn("已完成", output)
        self.assertIn("總計: 1", output)


class TestTaskProgressText(TestCase):
    def test_complete_progress(self) -> None:
        node = _make_task_node(completed_files=10, total_files=10)
        self.assertEqual(_task_progress_text(node), "10/10")

    def test_partial_progress(self) -> None:
        node = _make_task_node(completed_files=3, total_files=10)
        self.assertEqual(_task_progress_text(node), "3/10")

    def test_unknown_total(self) -> None:
        node = _make_task_node(completed_files=5, total_files=None)
        self.assertEqual(_task_progress_text(node), "5/?")

    def test_with_preparing_items(self) -> None:
        node = _make_task_node(completed_files=5, total_files=10, preparing_items=3)
        self.assertEqual(_task_progress_text(node), "5/10 +3 preparing")

    def test_byte_progress_with_known_total(self) -> None:
        node = _make_task_node(
            completed_files=0,
            total_files=1,
            completed_bytes=512,
            total_bytes=1024,
        )
        self.assertEqual(_task_progress_text(node), "0/1 (512 B / 1.0 KB 50%)")

    def test_byte_progress_with_unknown_total(self) -> None:
        node = _make_task_node(completed_bytes=2048, total_bytes=None)
        self.assertEqual(_task_progress_text(node), "10/10 (2.0 KB)")

    def test_byte_progress_percent_never_exceeds_total(self) -> None:
        node = _make_task_node(
            completed_files=0,
            total_files=1,
            completed_bytes=2048,
            total_bytes=1024,
        )
        self.assertEqual(_task_progress_text(node), "0/1 (1.0 KB / 1.0 KB 100%)")


class TestTaskCreatedAt(TestCase):
    def test_valid_iso_datetime(self) -> None:
        node = _make_task_node(created_at="2025-06-15T12:00:00+00:00")
        result = _task_created_at(node)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2025)  # type: ignore[union-attr]
        self.assertEqual(result.month, 6)  # type: ignore[union-attr]
        self.assertEqual(result.day, 15)  # type: ignore[union-attr]
        self.assertEqual(result.tzinfo, UTC)  # type: ignore[union-attr]

    def test_naive_datetime_gets_utc(self) -> None:
        node = _make_task_node(created_at="2025-06-15T12:00:00")
        result = _task_created_at(node)
        self.assertIsNotNone(result)
        self.assertEqual(result.tzinfo, UTC)  # type: ignore[union-attr]

    def test_invalid_datetime_string(self) -> None:
        node = _make_task_node(created_at="not-a-date")
        self.assertIsNone(_task_created_at(node))

    def test_missing_created_at(self) -> None:
        node = _make_task_node(created_at=None)
        self.assertIsNone(_task_created_at(node))

    def test_non_string_created_at(self) -> None:
        node = _make_task_node()
        node = TaskNode(
            task_id="task-1",
            kind=TaskKind.ITEM,
            title="Test",
            state=TaskState.COMPLETED,
            progress=TaskProgress(completed_files=0, total_files=0, preparing_items=0),
            details={"created_at": 12345},
        )
        self.assertIsNone(_task_created_at(node))


class TestPrintGenericDownloadError(TestCase):
    def setUp(self) -> None:
        self.data = GalleryData(
            source_url="https://example.test/gallery",
            site_id="example",
            gallery_id="123",
            title="Test Gallery",
        )

    def test_json_mode(self) -> None:
        error = CLIDownloaderError("download failed")
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            exit_code = _print_generic_download_error(
                self.data,
                render_mode=RenderMode.JSON,
                error=error,
                metadata={"detail": "test"},
            )
            output = mock_stdout.getvalue().strip()

        self.assertEqual(exit_code, 1)
        self.assertIn("download", output)
        self.assertIn("CLIDownloaderError", output)
        # JSON render mode outputs compact
        self.assertEqual(output.count("\n"), 0)

    def test_plain_mode(self) -> None:
        error = CLIDownloaderError("download failed")
        with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
            exit_code = _print_generic_download_error(
                self.data,
                render_mode=RenderMode.PLAIN,
                error=error,
                metadata={},
            )
            output = mock_stdout.getvalue().strip()

        self.assertEqual(exit_code, 1)
        self.assertIn("download", output)
        self.assertIn("CLIDownloaderError", output)
        # Plain render mode outputs indented
        self.assertGreater(output.count("\n"), 0)


class TestReportProgress(TestCase):
    def setUp(self) -> None:
        import cli_downloader.cli._output as _output

        _output._last_progress_emit.clear()

    def test_none_sink_is_silent(self) -> None:
        # Should not raise
        _report_progress(None, "stage_progress", {"stage": "fetching"})

    def test_sink_is_called(self) -> None:
        sink: list[tuple[str, dict]] = []

        def _collect(reason: str, payload: dict) -> None:
            sink.append((reason, payload))

        _report_progress(_collect, "stage_progress", {"stage": "fetching"})  # type: ignore[arg-type]
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0][0], "stage_progress")
        self.assertEqual(sink[0][1]["stage"], "fetching")

    def test_byte_progress_throttles(self) -> None:
        """Byte progress events for the same asset within 100ms should be dropped."""
        sink: list[tuple[str, dict]] = []

        def _collect(reason: str, payload: dict) -> None:
            sink.append((reason, payload))

        # First call should pass through
        _report_progress(  # type: ignore[arg-type]
            _collect,
            "batch_progress",
            {
                "progress_reason": "byte_progress",
                "asset_id": "asset-1",
                "bytes_received": 100,
                "total_bytes": 1000,
            },
        )
        self.assertEqual(len(sink), 1)

        # Second call within throttle window should be dropped
        _report_progress(  # type: ignore[arg-type]
            _collect,
            "batch_progress",
            {
                "progress_reason": "byte_progress",
                "asset_id": "asset-1",
                "bytes_received": 200,
                "total_bytes": 1000,
            },
        )
        self.assertEqual(len(sink), 1)

    def test_byte_progress_completion_not_throttled(self) -> None:
        """Byte progress at completion should always pass through."""
        sink: list[tuple[str, dict]] = []

        def _collect(reason: str, payload: dict) -> None:
            sink.append((reason, payload))

        # First call: in-progress
        _report_progress(  # type: ignore[arg-type]
            _collect,
            "batch_progress",
            {
                "progress_reason": "byte_progress",
                "asset_id": "asset-2",
                "bytes_received": 100,
                "total_bytes": 1000,
            },
        )
        self.assertEqual(len(sink), 1)

        # Second call: completion (bytes_received == total_bytes) always passes through
        # re-collect() should reset _last_progress_emit for this asset
        _report_progress(  # type: ignore[arg-type]
            _collect,
            "batch_progress",
            {
                "progress_reason": "byte_progress",
                "asset_id": "asset-2",
                "bytes_received": 1000,
                "total_bytes": 1000,
            },
        )
        self.assertEqual(len(sink), 2)  # completion passes through regardless of throttle


class TestPluginRoots(TestCase):
    def test_default_roots(self) -> None:
        result = _plugin_roots(())
        self.assertEqual(result, ("plugins",))

    def test_explicit_roots(self) -> None:
        result = _plugin_roots(["a", "b"])
        self.assertEqual(result, ("a", "b"))

    def test_empty_sequence(self) -> None:
        result = _plugin_roots([])
        self.assertEqual(result, ("plugins",))
