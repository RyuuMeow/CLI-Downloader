import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerHandshake,
    OwnerLifecycleState,
    OwnerMode,
    OwnerRequest,
    OwnerResponseEvent,
    OwnerResponseMode,
    OwnerSource,
    OwnerTerminalStatus,
)
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.core.owner_scope import (
    OwnerDiscoveryMetadata,
    OwnerScope,
    new_owner_session_id,
)
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import TaskDesiredControl, TaskRecord, TaskStatus
from cli_downloader.core.owner_transport import OwnerPipeClient, OwnerPipeServer
from cli_downloader.core.shell_control import ShellStatusService


class AgentOwnerWorkflowTests(unittest.TestCase):
    def test_visible_owner_accepts_item_and_batch_without_duplicate_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            owner_session = new_owner_session_id()
            calls: list[str] = []
            task_ids = {"item": "agent-item", "batch": "agent-batch"}

            metadata = OwnerDiscoveryMetadata.for_current_process(
                scope,
                application_version="0.1.5",
                owner_mode=OwnerMode.SHELL,
                lifecycle_state=OwnerLifecycleState.READY,
                session_id=owner_session,
                capabilities=("download", "task_control"),
            )
            owner_handshake = OwnerHandshake(
                OWNER_PROTOCOL_VERSION,
                owner_session,
                OwnerMode.SHELL,
                "0.1.5",
                ("download", "task_control"),
                scope_fingerprint=scope.scope_fingerprint,
            )

            def handler(request, emit):
                calls.append(request.request_id)
                task_kind = "batch" if request.payload.get("select") else "item"
                task_id = task_ids[task_kind]
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id=owner_session,
                        accepted_at=datetime.now(UTC),
                        task_id=task_id,
                        task_kind=task_kind,
                    )
                )
                if request.response_mode is OwnerResponseMode.WAIT:
                    emit(
                        OwnerResponseEvent.terminal(
                            request.request_id,
                            2,
                            status=OwnerTerminalStatus.COMPLETED,
                            task_id=task_id,
                            exit_code=0,
                        )
                    )

            # The transport test owns the endpoint directly; the lock and
            # discovery lifecycle are covered by ``test_owner_scope``.  Do not
            # hold the Windows msvcrt lock while probing the status service,
            # because a second non-blocking lock attempt can block in the same
            # process.  Publishing the same discovery record still exercises
            # the visible-owner metadata consumed by agents.
            scope.discovery().write(metadata)
            server = OwnerPipeServer(scope, handshake=owner_handshake, handler=handler)
            server.start()
            try:
                self.assertEqual(scope.discovery().read_for_scope().lifecycle_state, OwnerLifecycleState.READY)

                item_session = "agent-item-session"
                item_request = OwnerRequest(
                    OWNER_PROTOCOL_VERSION,
                    "agent-item-request",
                    OwnerSource.AGENT,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.DETACH,
                    {"url": "https://example.test/item/1"},
                    item_session,
                )
                item_handshake = OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    item_session,
                    OwnerMode.FOREGROUND,
                    "0.1.5",
                    ("download",),
                    ("download",),
                    scope.scope_fingerprint,
                )
                accepted = OwnerPipeClient(scope).request(
                    item_request,
                    handshake=item_handshake,
                )
                self.assertEqual(accepted[0].payload["task_id"], "agent-item")
                self.assertEqual(len(calls), 1)

                # A client reconnect replays acceptance and does not invoke the
                # handler a second time.
                replay = OwnerPipeClient(scope).request(
                    item_request,
                    handshake=item_handshake,
                )
                self.assertEqual(replay[0].payload["task_id"], "agent-item")
                self.assertEqual(len(calls), 1)

                batch_session = "agent-batch-session"
                batch_request = OwnerRequest(
                    OWNER_PROTOCOL_VERSION,
                    "agent-batch-request",
                    OwnerSource.AGENT,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.WAIT,
                    {
                        "url": "https://example.test/list/1",
                        "select": ("item-1", "item-2"),
                    },
                    batch_session,
                )
                batch_handshake = OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    batch_session,
                    OwnerMode.FOREGROUND,
                    "0.1.5",
                    ("download",),
                    ("download",),
                    scope.scope_fingerprint,
                )
                completed = OwnerPipeClient(scope).request(
                    batch_request,
                    handshake=batch_handshake,
                )
                self.assertEqual(completed[-1].kind.value, "terminal")
                self.assertEqual(completed[-1].payload["task_id"], "agent-batch")
                self.assertEqual(len(calls), 2)
            finally:
                server.stop()
                scope.discovery().clear(expected_session_id=owner_session)

            # A new owner in the same data root can replay the same accepted work
            # after the original owner has exited.
            restarted = OwnerPipeServer(scope, handshake=owner_handshake, handler=handler)
            restarted.start()
            try:
                replay = OwnerPipeClient(scope).request(item_request, handshake=item_handshake)
                self.assertEqual(replay[0].payload["task_id"], "agent-item")
                self.assertEqual(len(calls), 2)
            finally:
                restarted.stop()

    def test_agent_status_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            snapshot = ShellStatusService(scope).inspect()
            self.assertEqual(snapshot.to_dict()["status"], "absent")
            self.assertEqual(json.loads(json.dumps(snapshot.to_dict()))["ready"], False)

    def test_item_controls_and_batch_completion_keep_durable_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            database = StateDatabase(Path(directory) / "state" / "state.sqlite3")
            task_store = StateTaskStore(database)
            history_store = StateHistoryStore(database)
            owner_session = new_owner_session_id()
            download_calls: list[str] = []
            owner_handshake = OwnerHandshake(
                OWNER_PROTOCOL_VERSION,
                owner_session,
                OwnerMode.SHELL,
                "0.1.5",
                ("download", "task_control"),
                scope_fingerprint=scope.scope_fingerprint,
            )
            metadata = OwnerDiscoveryMetadata.for_current_process(
                scope,
                application_version="0.1.5",
                owner_mode=OwnerMode.SHELL,
                lifecycle_state=OwnerLifecycleState.READY,
                session_id=owner_session,
                capabilities=("download", "task_control"),
            )

            def handler(request, emit):
                if request.command is OwnerCommand.DOWNLOAD:
                    task_id = "workflow-batch" if request.payload.get("select") else "workflow-item"
                    task_kind = TaskKind.BATCH if request.payload.get("select") else TaskKind.ITEM
                    download_calls.append(request.request_id)
                    task_store.upsert(
                        TaskRecord(
                            task_id,
                            TaskStatus.PENDING,
                            kind=task_kind,
                            source_url=str(request.payload["url"]),
                            title=task_id,
                        )
                    )
                    emit(
                        OwnerResponseEvent.accepted(
                            request.request_id,
                            1,
                            owner_session_id=owner_session,
                            accepted_at=datetime.now(UTC),
                            task_id=task_id,
                            task_kind=task_kind.value,
                        )
                    )
                    if request.response_mode is OwnerResponseMode.WAIT:
                        record = task_store.get(task_id)
                        assert record is not None
                        task_store.upsert(record.transition(status=TaskStatus.COMPLETED))
                        history_store.upsert_record(
                            DownloadHistoryRecord(
                                record_id=f"history-{task_id}",
                                task_id=task_id,
                                kind=(HistoryKind.BATCH if task_kind is TaskKind.BATCH else HistoryKind.ITEM),
                                title=task_id,
                                source_url=str(request.payload["url"]),
                                status="completed",
                                completed_files=2,
                                total_files=2,
                            )
                        )
                        emit(
                            OwnerResponseEvent.terminal(
                                request.request_id,
                                2,
                                status=OwnerTerminalStatus.COMPLETED,
                                task_id=task_id,
                                exit_code=0,
                            )
                        )
                    return

                task_id = request.payload["task_id"]
                record = task_store.get(task_id)
                assert record is not None
                action = request.payload["action"]
                if action == "pause":
                    record = record.transition(
                        status=TaskStatus.PAUSED,
                        desired_control=TaskDesiredControl.PAUSE,
                    )
                elif action == "resume":
                    record = record.transition(
                        status=TaskStatus.PENDING,
                        desired_control=TaskDesiredControl.RUN,
                    )
                else:
                    raise AssertionError(f"unexpected action: {action}")
                task_store.upsert(record)
                emit(
                    OwnerResponseEvent.accepted(
                        request.request_id,
                        1,
                        owner_session_id=owner_session,
                        accepted_at=datetime.now(UTC),
                        task_id=task_id,
                        task_kind=record.kind.value,
                        queued=False,
                    )
                )
                emit(
                    OwnerResponseEvent.control(
                        request.request_id,
                        2,
                        task_id=task_id,
                        action=action,
                        acknowledged=True,
                        revision=record.revision,
                        desired_control=record.desired_control.value,
                    )
                )
                emit(
                    OwnerResponseEvent.terminal(
                        request.request_id,
                        3,
                        status=OwnerTerminalStatus.COMPLETED,
                        task_id=task_id,
                        exit_code=0,
                    )
                )

            scope.discovery().write(metadata)
            server = OwnerPipeServer(scope, handshake=owner_handshake, handler=handler)
            server.start()
            try:
                item_session = "workflow-item-session"
                item_request = OwnerRequest(
                    OWNER_PROTOCOL_VERSION,
                    "workflow-item-request",
                    OwnerSource.AGENT,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.DETACH,
                    {"url": "https://example.test/item/1"},
                    item_session,
                )
                item_handshake = OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    item_session,
                    OwnerMode.FOREGROUND,
                    "0.1.5",
                    ("download", "task_control"),
                    ("download",),
                    scope.scope_fingerprint,
                )
                accepted = OwnerPipeClient(scope).request(
                    item_request,
                    handshake=item_handshake,
                )
                self.assertEqual(accepted[0].payload["task_id"], "workflow-item")
                self.assertEqual(task_store.get("workflow-item").status, TaskStatus.PENDING)

                for action, expected_status, expected_control in (
                    ("pause", TaskStatus.PAUSED, TaskDesiredControl.PAUSE),
                    ("resume", TaskStatus.PENDING, TaskDesiredControl.RUN),
                ):
                    control_session = f"workflow-{action}-session"
                    control_request = OwnerRequest(
                        OWNER_PROTOCOL_VERSION,
                        f"workflow-{action}-request",
                        OwnerSource.AGENT,
                        OwnerCommand.TASK_CONTROL,
                        OwnerResponseMode.WAIT,
                        {"task_id": "workflow-item", "action": action},
                        control_session,
                    )
                    control_handshake = OwnerHandshake(
                        OWNER_PROTOCOL_VERSION,
                        control_session,
                        OwnerMode.FOREGROUND,
                        "0.1.5",
                        ("task_control",),
                        ("task_control",),
                        scope.scope_fingerprint,
                    )
                    control_events = OwnerPipeClient(scope).request(
                        control_request,
                        handshake=control_handshake,
                    )
                    self.assertEqual(control_events[1].kind.value, "control")
                    self.assertEqual(task_store.get("workflow-item").status, expected_status)
                    self.assertEqual(
                        task_store.get("workflow-item").desired_control,
                        expected_control,
                    )

                batch_session = "workflow-batch-session"
                batch_request = OwnerRequest(
                    OWNER_PROTOCOL_VERSION,
                    "workflow-batch-request",
                    OwnerSource.AGENT,
                    OwnerCommand.DOWNLOAD,
                    OwnerResponseMode.WAIT,
                    {
                        "url": "https://example.test/list/1",
                        "select": ("item-1", "item-2"),
                    },
                    batch_session,
                )
                batch_handshake = OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    batch_session,
                    OwnerMode.FOREGROUND,
                    "0.1.5",
                    ("download",),
                    ("download",),
                    scope.scope_fingerprint,
                )
                completed = OwnerPipeClient(scope).request(
                    batch_request,
                    handshake=batch_handshake,
                )
                self.assertEqual(completed[-1].payload["status"], "completed")
                self.assertEqual(task_store.get("workflow-batch").status, TaskStatus.COMPLETED)
                history = history_store.get_latest_record("workflow-batch")
                self.assertIsNotNone(history)
                self.assertEqual(history.source_url, "https://example.test/list/1")
                task_store.delete("workflow-batch")
                self.assertEqual(history_store.get_latest_record("workflow-batch"), history)
            finally:
                server.stop()
                scope.discovery().clear(expected_session_id=owner_session)

            restarted = OwnerPipeServer(scope, handshake=owner_handshake, handler=handler)
            restarted.start()
            try:
                replay = OwnerPipeClient(scope).request(item_request, handshake=item_handshake)
                self.assertEqual(replay[0].payload["task_id"], "workflow-item")
                self.assertEqual(download_calls, ["workflow-item-request", "workflow-batch-request"])
            finally:
                restarted.stop()


if __name__ == "__main__":
    unittest.main()
