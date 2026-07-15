import unittest
from itertools import product

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
                self.assertEqual(len(terminal), 1)
                self.assertEqual(accepted[0].payload["task_id"], f"{kind}-task")
                self.assertEqual(terminal[0].payload["task_id"], f"{kind}-task")
                self.assertEqual(accepted[0].payload["owner_session_id"], "shell-session")

    def test_unselected_list_never_fabricates_a_task(self) -> None:
        events = []

        def run_background(argv, cancellation, progress, on_accept):
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

        self.assertEqual([event.kind for event in events], [OwnerEventKind.OWNER_DISCONNECTED])
        self.assertNotIn("task_id", events[0].payload)


if __name__ == "__main__":
    unittest.main()
