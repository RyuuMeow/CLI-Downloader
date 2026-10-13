"""Importable process workers for owner transport lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import traceback
from unittest.mock import patch

from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerHandshake,
    OwnerMode,
    OwnerResponseEvent,
)
from cli_downloader.core.owner_scope import OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeServer


def orphan_owner_process(directory: str, ready, accepted, hold) -> None:
    """Keep an accepted WAIT request alive until the parent simulates a crash."""

    scope = OwnerScope.from_data_root(directory)
    owner_session = "orphan-owner"

    def handler(request, emit):
        emit(
            OwnerResponseEvent.accepted(
                request.request_id,
                1,
                owner_session_id=owner_session,
                accepted_at=datetime.now(UTC),
                task_id="orphan-task",
                task_kind="item",
            )
        )
        accepted.set()
        hold.wait(30)

    server = OwnerPipeServer(
        scope,
        handshake=OwnerHandshake(
            OWNER_PROTOCOL_VERSION,
            owner_session,
            OwnerMode.SHELL,
            "0.1.5",
            ("download",),
            scope_fingerprint=scope.scope_fingerprint,
        ),
        handler=handler,
    )
    server.start()
    ready.set()
    try:
        hold.wait(30)
    finally:
        server.stop()


def shell_owner_process(directory: str, ready, stop, executed) -> None:
    """Run a minimal importable Shell owner for process-level restart tests."""

    from cli_downloader.cli._shell_wiring import build_interactive_shell
    from tests.example_site import build_example_runtime

    os.environ["CLI_DOWNLOADER_DATA_ROOT"] = directory
    plugins_dir = Path(directory) / "plugins"

    class FakeShell:
        def __init__(self, **kwargs) -> None:
            self._startup_ready = kwargs["startup_ready_callback"]

        def notify_external_tasks(self) -> None:
            return None

        def run(self) -> int:
            self._startup_ready()
            ready.set()
            stop.wait(30)
            return 0

    def background_runner(
        _argv, _cancellation, _progress, _limiter, on_accept=None, task_service=None
    ):
        executed.set()
        if on_accept is not None:
            on_accept("process-task", "item")
        return 0

    try:
        with patch("cli_downloader.cli._shell_wiring.InteractiveShell", FakeShell):
            build_interactive_shell(
                runtime=build_example_runtime(),
                plugins_dir=plugins_dir,
                runner=lambda _argv: 0,
                background_runner=background_runner,
            )
    except Exception:
        (Path(directory) / "process-owner-error.txt").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )
        raise


def operation_cli_process(directory, result_queue):
    """An actual CLI process must forward without loading a local site plugin."""
    from contextlib import redirect_stdout
    from io import StringIO
    from cli_downloader.cli.app import main

    os.environ["CLI_DOWNLOADER_DATA_ROOT"] = directory
    os.environ["CLI_DOWNLOADER_PORTABLE_ROOT"] = ""
    output = StringIO()
    with redirect_stdout(output):
        code = main(["operation", "--json", "example", "browse_list"])
    result_queue.put((code, output.getvalue()))
