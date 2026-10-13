import json
import unittest
from datetime import UTC, datetime

from cli_downloader.core.errors import ContractValidationError
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerDisconnectOutcome,
    OwnerEventKind,
    OwnerHandshake,
    InMemoryOwnerTransport,
    OwnerLifecycleState,
    OwnerLivenessTracker,
    OwnerMode,
    OwnerRequest,
    OwnerRequestLedger,
    OwnerResponseEvent,
    OwnerResponseMode,
    OwnerResponseStream,
    OwnerSource,
    OwnerTerminalStatus,
    OwnerWaitPhase,
    OwnerWaitPolicy,
    RequestDisposition,
    RequestState,
    negotiate_handshake,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def download_request(**changes: object) -> OwnerRequest:
    values: dict[str, object] = {
        "protocol_version": OWNER_PROTOCOL_VERSION,
        "request_id": "request-1",
        "source": OwnerSource.CLI,
        "command": OwnerCommand.DOWNLOAD,
        "response_mode": OwnerResponseMode.WAIT,
        "payload": {
            "url": "https://example.test/gallery/42",
            "params": {"quality": "original"},
            "secret_refs": {"session": "browser.session"},
        },
        "client_session_id": "client-1",
        "created_at": NOW,
    }
    values.update(changes)
    return OwnerRequest(**values)


class OwnerRequestTests(unittest.TestCase):
    def test_request_round_trips_as_bounded_json(self) -> None:
        request = download_request()
        encoded = request.to_bytes()
        restored = OwnerRequest.from_bytes(encoded)

        self.assertEqual(restored, request)
        self.assertEqual(json.loads(encoded)["command"], "download")

    def test_download_request_carries_dispatch_options(self) -> None:
        request = download_request(
            payload={
                "url": "https://example.test/gallery/42",
                "profile": "pixiv-default",
                "strategy": "torrent",
                "select": ("item-1",),
                "selection_policy": "explicit",
                "force": True,
            }
        )
        restored = OwnerRequest.from_bytes(request.to_bytes())
        self.assertEqual(restored.payload["profile"], "pixiv-default")
        self.assertEqual(restored.payload["strategy"], "torrent")
        self.assertEqual(restored.payload["selection_policy"], "explicit")
        self.assertTrue(restored.payload["force"])

    def test_download_request_rejects_invalid_selection_policy(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            download_request(
                payload={
                    "url": "https://example.test/list",
                    "selection_policy": "guess",
                }
            )

        self.assertEqual(context.exception.code, "invalid_value")

    def test_request_rejects_raw_process_and_secret_values(self) -> None:
        for field in ("shell_command", "python", "cookies", "password"):
            with self.subTest(field=field):
                with self.assertRaises(ContractValidationError) as context:
                    download_request(payload={"url": "https://example.test", field: "unsafe"})
                self.assertEqual(context.exception.code, "forbidden_field")

    def test_request_rejects_unknown_command_payload(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            download_request(payload={"url": "https://example.test", "arbitrary": True})

        self.assertEqual(context.exception.code, "unknown_field")

    def test_request_frame_size_is_bounded(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            download_request(
                payload={"url": "https://example.test", "params": {"x": "a" * 200}}
            ).to_bytes(max_frame_bytes=64)

        self.assertEqual(context.exception.code, "frame_too_large")

    def test_request_rejects_protocol_mismatch_on_decode(self) -> None:
        payload = download_request().to_dict()
        payload["protocol_version"] = OWNER_PROTOCOL_VERSION + 1

        with self.assertRaises(ContractValidationError) as context:
            OwnerRequest.from_dict(payload)

        self.assertEqual(context.exception.code, "unsupported_protocol")


class HandshakeTests(unittest.TestCase):
    def test_capability_negotiation_returns_intersection(self) -> None:
        client = OwnerHandshake(
            1, "client-1", OwnerMode.FOREGROUND, "0.1.5", ("download", "status"), ("download",)
        )
        owner = OwnerHandshake(
            1, "owner-1", OwnerMode.SHELL, "0.1.5", ("download", "status", "history"), ()
        )

        result = negotiate_handshake(client, owner)

        self.assertEqual(result.owner_session_id, "owner-1")
        self.assertEqual(result.negotiated_capabilities, ("download", "status"))

    def test_missing_capability_is_structured(self) -> None:
        client = OwnerHandshake(1, "client-1", OwnerMode.FOREGROUND, "0.1.5", (), ("download",))
        owner = OwnerHandshake(1, "owner-1", OwnerMode.SHELL, "0.1.5", (), ())

        with self.assertRaises(ContractValidationError) as context:
            negotiate_handshake(client, owner)

        self.assertEqual(context.exception.code, "unsupported_capability")

    def test_scope_fingerprint_mismatch_is_rejected(self) -> None:
        client = OwnerHandshake(
            1,
            "client-1",
            OwnerMode.FOREGROUND,
            "0.1.5",
            ("download",),
            ("download",),
            "a" * 64,
        )
        owner = OwnerHandshake(
            1,
            "owner-1",
            OwnerMode.SHELL,
            "0.1.5",
            ("download",),
            (),
            "b" * 64,
        )
        with self.assertRaises(ContractValidationError) as context:
            negotiate_handshake(client, owner)
        self.assertEqual(context.exception.code, "scope_mismatch")

    def test_scope_fingerprint_cannot_be_omitted_by_only_one_peer(self) -> None:
        client = OwnerHandshake(
            1,
            "client-1",
            OwnerMode.FOREGROUND,
            "0.1.5",
            (),
            (),
            "a" * 64,
        )
        owner = OwnerHandshake(1, "owner-1", OwnerMode.SHELL, "0.1.5")

        with self.assertRaises(ContractValidationError) as context:
            negotiate_handshake(client, owner)
        self.assertEqual(context.exception.code, "scope_mismatch")

    def test_lifecycle_states_are_explicit(self) -> None:
        self.assertEqual(OwnerLifecycleState.READY.value, "ready")
        self.assertNotIn(
            OwnerLifecycleState.READY, {OwnerLifecycleState.STARTING, OwnerLifecycleState.BUSY}
        )


class ResponseStreamTests(unittest.TestCase):
    def test_control_event_carries_applied_receipt_fields(self) -> None:
        event = OwnerResponseEvent.control(
            "request-1",
            1,
            task_id="task-1",
            action="pause",
            acknowledged=True,
            requested=True,
            applied=True,
            actual_status="paused",
            revision=4,
            desired_control="pause",
            quiescent=True,
            phase="applied",
        )

        self.assertTrue(event.payload["applied"])
        self.assertEqual(event.payload["actual_status"], "paused")
        self.assertTrue(event.payload["quiescent"])

    def test_control_event_rejects_non_boolean_receipt_flags(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            OwnerResponseEvent(
                "request-1",
                1,
                OwnerEventKind.CONTROL,
                {
                    "task_id": "task-1",
                    "action": "pause",
                    "acknowledged": True,
                    "applied": "yes",
                },
            )

        self.assertEqual(context.exception.code, "invalid_boolean")

    def test_stream_requires_acceptance_and_one_terminal_event(self) -> None:
        stream = OwnerResponseStream("request-1")
        accepted = OwnerResponseEvent.accepted(
            "request-1", 1, owner_session_id="owner-1", accepted_at=NOW, task_id="task-1"
        )
        progress = OwnerResponseEvent.progress(
            "request-1", 2, task_id="task-1", status="running", progress=0.5
        )
        terminal = OwnerResponseEvent.terminal(
            "request-1", 3, status=OwnerTerminalStatus.COMPLETED, task_id="task-1"
        )

        for event in (accepted, progress, terminal):
            stream.push(event)

        self.assertTrue(stream.accepted)
        self.assertTrue(stream.terminal)
        with self.assertRaises(ContractValidationError) as context:
            stream.push(
                OwnerResponseEvent.heartbeat(
                    "request-1", 4, owner_session_id="owner-1", emitted_at=NOW
                )
            )
        self.assertEqual(context.exception.code, "event_after_terminal")

    def test_stream_rejects_gaps_and_pre_acceptance_progress(self) -> None:
        stream = OwnerResponseStream("request-1")
        with self.assertRaises(ContractValidationError) as context:
            stream.push(
                OwnerResponseEvent.progress("request-1", 1, task_id="task-1", status="queued")
            )
        self.assertEqual(context.exception.code, "invalid_lifecycle")

        stream.push(
            OwnerResponseEvent.accepted("request-1", 1, owner_session_id="owner-1", accepted_at=NOW)
        )
        with self.assertRaises(ContractValidationError) as context:
            stream.push(
                OwnerResponseEvent.heartbeat(
                    "request-1", 3, owner_session_id="owner-1", emitted_at=NOW
                )
            )
        self.assertEqual(context.exception.code, "invalid_sequence")

    def test_disconnect_has_deterministic_outcomes(self) -> None:
        not_accepted = OwnerResponseEvent.owner_disconnected(
            "request-1",
            1,
            phase=OwnerWaitPhase.ACCEPTANCE,
            outcome=OwnerDisconnectOutcome.NOT_ACCEPTED,
        )
        self.assertEqual(not_accepted.payload["outcome"], "not_accepted")

        lost = OwnerResponseEvent.owner_disconnected(
            "request-1",
            1,
            phase=OwnerWaitPhase.RESPONSE_IDLE,
            outcome=OwnerDisconnectOutcome.TERMINAL_RESULT_LOST,
            known_task_id="task-1",
            terminal_persisted=True,
        )
        self.assertEqual(lost.kind, OwnerEventKind.OWNER_DISCONNECTED)

        with self.assertRaises(ContractValidationError) as context:
            OwnerResponseEvent.owner_disconnected(
                "request-1",
                1,
                phase=OwnerWaitPhase.RESPONSE_IDLE,
                outcome=OwnerDisconnectOutcome.NOT_ACCEPTED,
            )
        self.assertEqual(context.exception.code, "invalid_disconnect_outcome")


class LedgerAndLivenessTests(unittest.TestCase):
    def test_request_id_deduplicates_same_payload_and_rejects_conflict(self) -> None:
        ledger = OwnerRequestLedger()
        first = ledger.register(download_request())
        duplicate = ledger.register(download_request())
        conflict = ledger.register(download_request(payload={"url": "https://example.test/other"}))

        self.assertEqual(first.disposition, RequestDisposition.NEW)
        self.assertEqual(duplicate.disposition, RequestDisposition.DUPLICATE)
        self.assertEqual(conflict.disposition, RequestDisposition.CONFLICT)
        self.assertEqual(
            ledger.mark_accepted("request-1", task_id="task-1").state, RequestState.ACCEPTED
        )
        self.assertEqual(
            ledger.mark_terminal("request-1", status=OwnerTerminalStatus.COMPLETED).state,
            RequestState.TERMINAL,
        )

    def test_heartbeat_extends_idle_window_but_not_acceptance_contract(self) -> None:
        policy = OwnerWaitPolicy(response_idle_timeout=10)
        tracker = OwnerLivenessTracker(policy, now=0)
        accepted = OwnerResponseEvent.accepted(
            "request-1", 1, owner_session_id="owner-1", accepted_at=NOW
        )
        heartbeat = OwnerResponseEvent.heartbeat(
            "request-1", 2, owner_session_id="owner-1", emitted_at=NOW
        )

        tracker.observe(accepted, now=1)
        tracker.observe(heartbeat, now=9)
        self.assertFalse(tracker.is_expired(now=18.9))
        self.assertTrue(tracker.is_expired(now=19))
        self.assertEqual(policy.timeout_for(OwnerWaitPhase.ACCEPTANCE), 30)

    def test_in_memory_transport_bounds_silent_owner_wait(self) -> None:
        transport = InMemoryOwnerTransport(
            policy=OwnerWaitPolicy(acceptance_timeout=3, response_idle_timeout=5)
        )
        transport.submit(download_request(), now=0)

        self.assertIsNone(transport.poll("request-1", now=2.9))
        disconnected = transport.poll("request-1", now=3)

        self.assertIsNotNone(disconnected)
        assert disconnected is not None
        self.assertEqual(disconnected.kind, OwnerEventKind.OWNER_DISCONNECTED)
        self.assertEqual(disconnected.payload["outcome"], OwnerDisconnectOutcome.NOT_ACCEPTED.value)

    def test_in_memory_transport_keeps_healthy_heartbeat_stream_alive(self) -> None:
        transport = InMemoryOwnerTransport(policy=OwnerWaitPolicy(response_idle_timeout=5))
        transport.submit(download_request(), now=0)
        transport.deliver(
            OwnerResponseEvent.accepted(
                "request-1", 1, owner_session_id="owner-1", accepted_at=NOW, task_id="task-1"
            ),
            now=0,
        )
        self.assertIsNotNone(transport.poll("request-1", now=0))
        transport.deliver(
            OwnerResponseEvent.heartbeat(
                "request-1", 2, owner_session_id="owner-1", emitted_at=NOW
            ),
            now=4,
        )
        self.assertIsNotNone(transport.poll("request-1", now=4))

        self.assertIsNone(transport.poll("request-1", now=8.9))
        disconnected = transport.poll("request-1", now=9)
        self.assertIsNotNone(disconnected)
        assert disconnected is not None
        self.assertEqual(disconnected.kind, OwnerEventKind.OWNER_DISCONNECTED)

    def test_in_memory_transport_reports_terminal_result_lost(self) -> None:
        transport = InMemoryOwnerTransport()
        transport.submit(download_request(), now=0)
        transport.deliver(
            OwnerResponseEvent.accepted(
                "request-1", 1, owner_session_id="owner-1", accepted_at=NOW, task_id="task-1"
            ),
            now=0,
        )
        transport.poll("request-1", now=0)

        disconnected = transport.disconnect(
            "request-1",
            phase=OwnerWaitPhase.RESPONSE_IDLE,
            outcome=OwnerDisconnectOutcome.TERMINAL_RESULT_LOST,
            known_task_id="task-1",
            terminal_persisted=True,
        )
        self.assertEqual(
            disconnected.payload["outcome"], OwnerDisconnectOutcome.TERMINAL_RESULT_LOST.value
        )


if __name__ == "__main__":
    unittest.main()
