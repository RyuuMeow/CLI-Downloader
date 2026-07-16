import json
import multiprocessing
import tempfile
import time
import unittest
from datetime import UTC, datetime
from threading import Thread

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
    OwnerWaitPolicy,
)
from cli_downloader.core.owner_scope import OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeClient, OwnerPipeServer, OwnerTransportError
from tests.owner_transport_worker import orphan_owner_process


class OwnerPipeTransportTests(unittest.TestCase):
    def test_missing_endpoint_metadata_fails_before_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)

            with self.assertRaisesRegex(OwnerTransportError, "metadata is missing"):
                OwnerPipeClient(scope)._load_endpoint()

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

    def test_duplicate_request_replays_cached_events_without_reinvoking_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            calls = []

            def handler(request, emit):
                calls.append(request.request_id)
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id="owner-session",
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
                    "duplicate-request",
                    OwnerSource.CLI,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.WAIT,
                    {"url": "https://example.test/gallery/1"},
                    session,
                )
                handshake = OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    session,
                    OwnerMode.FOREGROUND,
                    "0.1.5",
                    ("download",),
                    ("download",),
                )
                first = OwnerPipeClient(scope).request(request, handshake=handshake)
                second = OwnerPipeClient(scope).request(request, handshake=handshake)
            finally:
                server.stop()

            self.assertEqual(len(calls), 1)
            self.assertEqual([event.kind.value for event in first], ["accepted", "terminal"])
            self.assertEqual([event.kind.value for event in second], ["accepted", "terminal"])

    def test_duplicate_request_replays_after_owner_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            calls = []
            session = "client-session"
            request = OwnerRequest(
                OWNER_PROTOCOL_VERSION,
                "restart-request",
                OwnerSource.CLI,
                OwnerCommand.DOWNLOAD,
                OwnerResponseMode.WAIT,
                {"url": "https://example.test/gallery/3"},
                session,
            )
            handshake = OwnerHandshake(
                OWNER_PROTOCOL_VERSION,
                session,
                OwnerMode.FOREGROUND,
                "0.1.5",
                ("download",),
                ("download",),
            )

            def handler(request, emit):
                calls.append(request.request_id)
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id="owner-session",
                        accepted_at=datetime.now(UTC),
                        task_id="task-3",
                        task_kind="item",
                    )
                )
                emit(
                    OwnerResponseEvent.terminal(
                        request.request_id,
                        2,
                        status=OwnerTerminalStatus.COMPLETED,
                        task_id="task-3",
                        exit_code=0,
                    )
                )

            owner_handshake = OwnerHandshake(
                OWNER_PROTOCOL_VERSION,
                "owner-session",
                OwnerMode.SHELL,
                "0.1.5",
                ("download",),
            )
            first_server = OwnerPipeServer(scope, handshake=owner_handshake, handler=handler)
            first_server.start()
            try:
                OwnerPipeClient(scope).request(request, handshake=handshake)
            finally:
                first_server.stop()

            second_server = OwnerPipeServer(scope, handshake=owner_handshake, handler=handler)
            second_server.start()
            try:
                replay = OwnerPipeClient(scope).request(request, handshake=handshake)
            finally:
                second_server.stop()

            self.assertEqual(calls, ["restart-request"])
            self.assertEqual([event.kind.value for event in replay], ["accepted", "terminal"])

    def test_silent_owner_becomes_structured_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)

            def handler(request, emit):
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id="owner-session",
                        accepted_at=datetime.now(UTC),
                        task_id="task-silent",
                        task_kind="item",
                    )
                )
                time.sleep(0.2)

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
                    "silent-request",
                    OwnerSource.CLI,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.WAIT,
                    {"url": "https://example.test/gallery/4"},
                    session,
                )
                events = OwnerPipeClient(
                    scope,
                    wait_policy=OwnerWaitPolicy(response_idle_timeout=0.05),
                ).request(
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

            self.assertEqual(events[-1].kind.value, "owner_disconnected")
            self.assertEqual(events[-1].payload["outcome"], "accepted_completion_unknown")

    def test_pre_acceptance_wait_uses_acceptance_timeout_not_response_idle_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)

            def handler(request, emit):
                time.sleep(0.05)
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id="owner-session",
                        accepted_at=datetime.now(UTC),
                        task_id="slow-accept-task",
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
                events = OwnerPipeClient(
                    scope,
                    wait_policy=OwnerWaitPolicy(
                        acceptance_timeout=0.5,
                        response_idle_timeout=0.01,
                    ),
                ).request(
                    OwnerRequest(
                        OWNER_PROTOCOL_VERSION,
                        "slow-accept-request",
                        OwnerSource.CLI,
                        OwnerCommand.DOWNLOAD,
                        OwnerResponseMode.DETACH,
                        {"url": "https://example.test/list"},
                        session,
                    ),
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

        self.assertEqual([event.kind.value for event in events], ["accepted"])
        self.assertEqual(events[0].payload["task_id"], "slow-accept-task")

    def test_crashed_owner_does_not_leave_wait_client_blocked_and_replays_task_identity(self) -> None:
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            ready = context.Event()
            accepted = context.Event()
            hold = context.Event()
            process = context.Process(
                target=orphan_owner_process,
                args=(directory, ready, accepted, hold),
            )
            process.start()
            self.assertTrue(ready.wait(15), "owner process did not publish its endpoint")
            session = "orphan-client"
            request = OwnerRequest(
                OWNER_PROTOCOL_VERSION,
                "orphan-request",
                OwnerSource.CLI,
                OwnerCommand.DOWNLOAD,
                OwnerResponseMode.WAIT,
                {"url": "https://example.test/item/1"},
                session,
            )
            handshake = OwnerHandshake(
                OWNER_PROTOCOL_VERSION,
                session,
                OwnerMode.FOREGROUND,
                "0.1.5",
                ("download",),
                ("download",),
                scope.scope_fingerprint,
            )
            events: list[object] = []

            def wait_for_result() -> None:
                events.extend(
                    OwnerPipeClient(
                        scope,
                        wait_policy=OwnerWaitPolicy(response_idle_timeout=1.0),
                    ).request(request, handshake=handshake)
                )

            client_thread = Thread(target=wait_for_result, daemon=True)
            client_thread.start()
            self.assertTrue(accepted.wait(15), "owner did not accept the WAIT request")
            process.terminate()
            process.join(15)
            self.assertFalse(process.is_alive())
            client_thread.join(5)
            self.assertFalse(client_thread.is_alive(), "WAIT client remained blocked after owner crash")
            self.assertTrue(events)
            self.assertEqual(events[-1].kind.value, "owner_disconnected")
            self.assertEqual(events[-1].payload["known_task_id"], "orphan-task")

            # A new owner can start and the same request remains authoritative;
            # persistence replays acceptance instead of invoking a replacement.
            restarted = OwnerPipeServer(
                scope,
                handshake=OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    "restarted-owner",
                    OwnerMode.SHELL,
                    "0.1.5",
                    ("download",),
                    scope_fingerprint=scope.scope_fingerprint,
                ),
                handler=lambda _request, _emit: self.fail("replayed request was re-executed"),
            )
            restarted.start()
            try:
                replay = OwnerPipeClient(
                    scope,
                    wait_policy=OwnerWaitPolicy(response_idle_timeout=0.2),
                ).request(request, handshake=handshake)
            finally:
                restarted.stop()
            self.assertEqual(replay[0].kind.value, "accepted")
            self.assertEqual(replay[0].payload["task_id"], "orphan-task")

    def test_endpoint_metadata_scope_mismatch_is_rejected_before_connect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)

            server = OwnerPipeServer(
                scope,
                handshake=OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    "owner-session",
                    OwnerMode.SHELL,
                    "0.1.5",
                ),
                handler=lambda _request, _emit: None,
            )
            server.start()
            try:
                metadata = json.loads(server._metadata_path.read_text(encoding="utf-8"))
                metadata["scope_fingerprint"] = "f" * 64
                server._metadata_path.write_text(
                    json.dumps(metadata), encoding="utf-8"
                )
                with self.assertRaises(OwnerTransportError):
                    OwnerPipeClient(scope)._load_endpoint()
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
