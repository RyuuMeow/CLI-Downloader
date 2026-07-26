from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.core.crash_log import write_crash_log


class CrashLogTest(TestCase):
    def test_appends_context_and_traceback(self) -> None:
        with TemporaryDirectory() as directory:
            logs_dir = Path(directory) / "logs"
            try:
                raise RuntimeError("terminal transition failed")
            except RuntimeError as error:
                target = write_crash_log(
                    error,
                    context="shell background command: operation",
                    logs_dir=logs_dir,
                )

            self.assertEqual(target, logs_dir / "crash.log")
            text = target.read_text(encoding="utf-8")
            self.assertIn("shell background command: operation", text)
            self.assertIn("RuntimeError: terminal transition failed", text)
            self.assertIn("test_appends_context_and_traceback", text)

    def test_logging_failure_does_not_raise(self) -> None:
        with TemporaryDirectory() as directory:
            occupied = Path(directory) / "occupied"
            occupied.write_text("not a directory", encoding="utf-8")

            target = write_crash_log(
                ValueError("failed"),
                context="test",
                logs_dir=occupied,
            )

            self.assertIsNone(target)
