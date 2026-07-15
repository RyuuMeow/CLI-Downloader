import tempfile
import unittest
from datetime import UTC, datetime

from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerHandshake,
    OwnerMode,
    OwnerRequest,
    OwnerResponseEvent,
    OwnerResponseMode,
    OwnerSource,
    OwnerTerminalStatus,
)
from cli_downloader.core.owner_scope import OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeClient, OwnerPipeServer


class OwnerPipeTransportTests(unittest.TestCase):
    def test_round_trip_uses_scope_specific_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            owner_session = "owner-session"
            client_session = "client-session"

            def handler(request, emit):
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id=owner_session,
                        accepted_at=datetime.now(UTC),
                        task_id="task-1",
                        task_kind="item",
                    )
                )
                emit(
                    OwnerResponseEvent.terminal(
                        request.request_id,
                        2,
                        status=OwnerTerminalStatus.COMPLETED,
                        task_id="task-1",
                        exit_code=0,
                    )
                )

            server = OwnerPipeServer(
                scope,
                handshake=OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    owner_session,
                    OwnerMode.SHELL,
                    "0.1.5",
                    ("download",),
                ),
                handler=handler,
            )
            server.start()
            try:
                request = OwnerRequest(
                    OWNER_PROTOCOL_VERSION,
                    "request-1",
                    OwnerSource.CLI,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.WAIT,
                    {"url": "https://example.test/gallery/1"},
                    client_session,
                )
                events = OwnerPipeClient(scope).request(
                    request,
                    handshake=OwnerHandshake(
                        OWNER_PROTOCOL_VERSION,
                        client_session,
                        OwnerMode.FOREGROUND,
                        "0.1.5",
                        ("download",),
                        ("download",),
                    ),
                )
            finally:
                server.stop()

            self.assertEqual([event.kind.value for event in events], ["accepted", "terminal"])
            self.assertEqual(events[-1].payload["status"], "completed")

    def test_detach_returns_after_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)

            def handler(request, emit):
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id="owner-session",
                        accepted_at=datetime.now(UTC),
                        task_id="task-1",
                        task_kind="batch",
                    )
                )

            server = OwnerPipeServer(
                scope,
                handshake=OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    "owner-session",
                    OwnerMode.SHELL,
                    "0.1.5",
                    ("download",),
                ),
                handler=handler,
            )
            server.start()
            try:
                session = "client-session"
                request = OwnerRequest(
                    OWNER_PROTOCOL_VERSION,
                    "request-2",
                    OwnerSource.CLI,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.DETACH,
                    {"url": "https://example.test/gallery/2"},
                    session,
                )
                events = OwnerPipeClient(scope).request(
                    request,
                    handshake=OwnerHandshake(
                        OWNER_PROTOCOL_VERSION,
                        session,
                        OwnerMode.FOREGROUND,
                        "0.1.5",
                        ("download",),
                        ("download",),
                    ),
                )
            finally:
                server.stop()

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind.value, "accepted")


if __name__ == "__main__":
    unittest.main()
