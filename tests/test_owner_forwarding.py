import unittest
from itertools import product

from cli_downloader.cli._shell_wiring import _handle_owner_download
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerEventKind,
    OwnerRequest,
    OwnerResponseMode,
    OwnerSource,
)


class OwnerForwardingTests(unittest.TestCase):
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
