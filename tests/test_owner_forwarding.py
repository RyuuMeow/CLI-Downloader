import unittest
from itertools import product
from types import SimpleNamespace

from cli_downloader.cli._shell_wiring import _handle_owner_control, _handle_owner_download
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerEventKind,
    OwnerRequest,
    OwnerResponseMode,
    OwnerSource,
)
from cli_downloader.core.task_tree import TaskKind, TaskNode, TaskProgress, TaskState


class OwnerForwardingTests(unittest.TestCase):
    def test_task_control_returns_acceptance_control_and_terminal(self) -> None:
        events = []

        class FakeTaskService:
            def get(self, task_id):
                return TaskNode(
                    task_id=task_id,
                    kind=TaskKind.ITEM,
                    state=TaskState.RUNNING,
                    title="fixture",
                    progress=TaskProgress(),
                )

            def pause(self, task_id):
                return {"task_id": task_id, "status": "paused"}

        request = OwnerRequest(
            OWNER_PROTOCOL_VERSION,
            "control-request",
            OwnerSource.CLI,
            OwnerCommand.TASK_CONTROL,
            OwnerResponseMode.WAIT,
            {"task_id": "task-1", "action": "pause"},
            "client-session",
        )
        _handle_owner_control(
            request,
            events.append,
            run_background=lambda *_args: 0,
            owner_session_id="shell-session",
            task_service=FakeTaskService(),
        )

        self.assertEqual(
            [event.kind for event in events],
            [OwnerEventKind.ACCEPTED, OwnerEventKind.CONTROL, OwnerEventKind.TERMINAL],
        )
        self.assertTrue(events[1].payload["acknowledged"])
        self.assertEqual(events[2].payload["status"], "completed")

    def test_task_control_reports_applied_receipt(self) -> None:
        events = []

        class FakeTaskService:
            def get(self, task_id):
                return TaskNode(
                    task_id=task_id,
                    kind=TaskKind.ITEM,
                    state=TaskState.RUNNING,
                    title="fixture",
                    progress=TaskProgress(),
                )

            def pause(self, task_id):
                return {"task_id": task_id, "status": "paused"}

            def wait_for_control_applied(self, task_id, action, *, timeout):
                self.wait_timeout = timeout
                return SimpleNamespace(
                    task_id=task_id,
                    action=action,
                    requested=True,
                    applied=True,
                    actual_status=SimpleNamespace(value="paused"),
                    desired_control=SimpleNamespace(value="pause"),
                    revision=9,
                    quiescent=True,
                    phase="applied",
                    to_dict=lambda: {
                        "task_id": task_id,
                        "action": action,
                        "requested": True,
                        "applied": True,
                        "actual_status": "paused",
                        "desired_control": "pause",
                        "revision": 9,
                        "quiescent": True,
                        "phase": "applied",
                    },
                )

        request = OwnerRequest(
            OWNER_PROTOCOL_VERSION,
            "control-receipt",
            OwnerSource.CLI,
            OwnerCommand.TASK_CONTROL,
            OwnerResponseMode.WAIT,
            {"task_id": "task-1", "action": "pause"},
            "client-session",
        )
        _handle_owner_control(
            request,
            events.append,
            run_background=lambda *_args: 0,
            owner_session_id="shell-session",
            task_service=FakeTaskService(),
        )

        control = events[1].payload
        self.assertTrue(control["requested"])
        self.assertTrue(control["applied"])
        self.assertTrue(control["quiescent"])
        self.assertEqual(control["phase"], "applied")
        self.assertEqual(events[-1].payload["status"], "completed")

    def test_pending_control_is_bounded_and_not_reported_as_success(self) -> None:
        events = []

        class FakeTaskService:
            def get(self, task_id):
                return TaskNode(
                    task_id=task_id,
                    kind=TaskKind.ITEM,
                    state=TaskState.RUNNING,
                    title="fixture",
                    progress=TaskProgress(),
                )

            def pause(self, task_id):
                return {"task_id": task_id, "status": "pausing"}

            def wait_for_control_applied(self, task_id, action, *, timeout):
                self.wait_timeout = timeout
                return SimpleNamespace(
                    task_id=task_id,
                    action=action,
                    requested=True,
                    applied=False,
                    actual_status=SimpleNamespace(value="running"),
                    desired_control=SimpleNamespace(value="pause"),
                    revision=10,
                    quiescent=False,
                    phase="pending",
                    to_dict=lambda: {
                        "task_id": task_id,
                        "action": action,
                        "requested": True,
                        "applied": False,
                        "actual_status": "running",
                        "desired_control": "pause",
                        "revision": 10,
                        "quiescent": False,
                        "phase": "pending",
                    },
                )

        request = OwnerRequest(
            OWNER_PROTOCOL_VERSION,
            "control-pending",
            OwnerSource.CLI,
            OwnerCommand.TASK_CONTROL,
            OwnerResponseMode.WAIT,
            {"task_id": "task-1", "action": "pause"},
            "client-session",
        )
        _handle_owner_control(
            request,
            events.append,
            run_background=lambda *_args: 0,
            owner_session_id="shell-session",
            task_service=FakeTaskService(),
        )

        self.assertFalse(events[1].payload["applied"])
        self.assertEqual(events[1].payload["phase"], "pending")
        self.assertEqual(events[-1].payload["status"], "failed")
        self.assertEqual(events[-1].payload["exit_code"], 2)

    def test_batch_resume_uses_owner_background_runner(self) -> None:
        events = []
        calls = []

        class FakeBatchService:
            def get(self, task_id):
                return TaskNode(
                    task_id=task_id,
                    kind=TaskKind.BATCH,
                    state=TaskState.PAUSED,
                    title="fixture batch",
                    progress=TaskProgress(),
                )

        def run_background(argv, cancellation, progress, on_accept):
            calls.append(tuple(argv))
            return 0

        request = OwnerRequest(
            OWNER_PROTOCOL_VERSION,
            "batch-control-request",
            OwnerSource.CLI,
            OwnerCommand.TASK_CONTROL,
            OwnerResponseMode.WAIT,
            {"task_id": "batch-1", "action": "resume"},
            "client-session",
        )
        _handle_owner_control(
            request,
            events.append,
            run_background=run_background,
            owner_session_id="shell-session",
            task_service=FakeBatchService(),
        )

        self.assertEqual(calls, [("batches", "resume", "batch-1", "--json")])
        self.assertEqual(events[-1].payload["status"], "completed")

    def test_shell_owner_matrix_keeps_one_task_id_per_request(self) -> None:
        for response_mode, kind in product(
            (OwnerResponseMode.WAIT, OwnerResponseMode.DETACH),
            ("item", "batch"),
        ):
            with self.subTest(response_mode=response_mode, kind=kind):
                events = []

                def run_background(argv, cancellation, progress, on_accept):
                    task_id = f"{kind}-task"
                    on_accept(task_id, kind)
                    progress("task_running", {"current": 1, "total": 2})
                    return 0

                payload = {"url": "https://example.test/gallery/1"}
                if kind == "batch":
                    payload["select"] = ("item-1",)
                request = OwnerRequest(
                    OWNER_PROTOCOL_VERSION,
                    f"request-{response_mode.value}-{kind}",
                    OwnerSource.CLI,
                    OwnerCommand.DOWNLOAD,
                    response_mode,
                    payload,
                    "client-session",
                )
                _handle_owner_download(
                    request,
                    events.append,
                    run_background=run_background,
                    owner_session_id="shell-session",
                )

                accepted = [event for event in events if event.kind is OwnerEventKind.ACCEPTED]
                terminal = [event for event in events if event.kind is OwnerEventKind.TERMINAL]
                self.assertEqual(len(accepted), 1)
                self.assertEqual(accepted[0].payload["task_id"], f"{kind}-task")
                self.assertEqual(accepted[0].payload["owner_session_id"], "shell-session")
                if response_mode is OwnerResponseMode.WAIT:
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0].payload["task_id"], f"{kind}-task")

    def test_external_download_notifies_tasks_after_durable_acceptance(self) -> None:
        events = []
        notifications: list[str] = []

        def run_background(argv, cancellation, progress, on_accept):
            on_accept("external-task", "item")
            return 0

        request = OwnerRequest(
            OWNER_PROTOCOL_VERSION,
            "request-refresh",
            OwnerSource.CLI,
            OwnerCommand.DOWNLOAD,
            OwnerResponseMode.DETACH,
            {"url": "https://example.test/gallery/refresh"},
            "client-session",
        )

        _handle_owner_download(
            request,
            events.append,
            run_background=run_background,
            owner_session_id="shell-session",
            notify_tasks=lambda: notifications.append("refresh"),
        )

        self.assertEqual(notifications, ["refresh"])
        self.assertEqual(events[0].kind, OwnerEventKind.ACCEPTED)
        self.assertEqual(events[0].payload["task_id"], "external-task")

    def test_unselected_list_forwards_all_selection_policy(self) -> None:
        events = []
        calls = []

        def run_background(argv, cancellation, progress, on_accept):
            calls.append(argv)
            on_accept("batch-all", "batch")
            return 0

        request = OwnerRequest(
            OWNER_PROTOCOL_VERSION,
            "request-unselected",
            OwnerSource.CLI,
            OwnerCommand.DOWNLOAD,
            OwnerResponseMode.WAIT,
            {"url": "https://example.test/list"},
            "client-session",
        )
        _handle_owner_download(
            request,
            events.append,
            run_background=run_background,
            owner_session_id="shell-session",
        )

        self.assertIn("--selection-policy", calls[0])
        self.assertEqual(calls[0][calls[0].index("--selection-policy") + 1], "all")
        self.assertEqual(events[0].kind, OwnerEventKind.ACCEPTED)
        self.assertEqual(events[0].payload["task_id"], "batch-all")


if __name__ == "__main__":
    unittest.main()
