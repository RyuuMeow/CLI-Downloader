import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from cli_downloader.cli._shell_wiring import build_interactive_shell
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerHandshake,
    OwnerLifecycleState,
    OwnerMode,
    OwnerRequest,
    OwnerResponseMode,
    OwnerSource,
)
from cli_downloader.core.owner_scope import OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeClient, OwnerTransportError
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus
from tests.example_site import build_example_runtime


class ShellWiringTest(TestCase):
    def test_build_wires_callbacks_and_lazily_reuses_task_service(self) -> None:
        runner = Mock(return_value=0)
        background_runner = Mock(return_value=0)
        operation_runner = Mock()

        with TemporaryDirectory() as directory:
            plugins_dir = Path(directory) / "plugins"
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                runtime = build_example_runtime()
                fake_shell = Mock()
                fake_shell.run.return_value = 17
                with patch(
                    "cli_downloader.cli._shell_wiring.InteractiveShell",
                    return_value=fake_shell,
                ) as shell_constructor:
                    exit_code = build_interactive_shell(
                        runtime=runtime,
                        plugins_dir=plugins_dir,
                        runner=runner,
                        background_runner=background_runner,
                        operation_runner=operation_runner,
                    )

        self.assertEqual(exit_code, 17)
        kwargs = shell_constructor.call_args.kwargs
        self.assertIs(kwargs["runner"], runner)
        self.assertIs(kwargs["operation_runner"], operation_runner)
        self.assertEqual(kwargs["plugins_dir"], plugins_dir)
        self.assertEqual(kwargs["startup_commands"], ())
        service_loader = kwargs["task_service_loader"]
        self.assertIs(service_loader(), service_loader())
        fake_shell.run.assert_called_once_with()

    def test_shutdown_timeout_rotates_running_task_execution_fence(self) -> None:
        runner = Mock(return_value=0)
        background_runner = Mock(return_value=0)

        with TemporaryDirectory() as directory:
            plugins_dir = Path(directory) / "plugins"
            store = StateTaskStore(
                StateDatabase(Path(directory) / "state" / "state.sqlite3")
            )
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                runtime = build_example_runtime()
                fake_shell = Mock()

                def run_shell():
                    store.upsert(
                        TaskRecord(
                            task_id="late-worker",
                            status=TaskStatus.RUNNING,
                            source_url="https://example.test/gallery/late-worker",
                            metadata={
                                "scheduler_managed": True,
                                "execution_fence": "old-owner",
                            },
                        )
                    )
                    return 0

                fake_shell.run.side_effect = run_shell
                with patch(
                    "cli_downloader.cli._shell_wiring.InteractiveShell",
                    return_value=fake_shell,
                ):
                    self.assertEqual(
                        build_interactive_shell(
                            runtime=runtime,
                            plugins_dir=plugins_dir,
                            runner=runner,
                            background_runner=background_runner,
                        ),
                        0,
                    )

            recovered = store.get("late-worker")
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.status, TaskStatus.PLANNED)
            self.assertNotEqual(recovered.metadata.get("execution_fence"), "old-owner")

    def test_fully_wired_owner_rejects_recovery_then_accepts_after_ready(self) -> None:
        runner = Mock(return_value=0)

        with TemporaryDirectory() as directory:
            plugins_dir = Path(directory) / "plugins"
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                runtime = build_example_runtime()
                fake_shell = Mock()

                def background_runner(
                    _argv,
                    _cancellation,
                    _progress,
                    _download_limiter,
                    on_accept=None,
                ) -> int:
                    if on_accept is not None:
                        on_accept("wired-task", "item")
                    return 0

                def run_shell() -> int:
                    scope = OwnerScope.from_data_root(directory)
                    self.assertEqual(
                        scope.discovery().read_for_scope().lifecycle_state,
                        OwnerLifecycleState.RECOVERING,
                    )
                    session = "wired-client"
                    handshake = OwnerHandshake(
                        OWNER_PROTOCOL_VERSION,
                        session,
                        OwnerMode.FOREGROUND,
                        "0.1.5",
                        ("download",),
                        scope_fingerprint=scope.scope_fingerprint,
                    )
                    request = OwnerRequest(
                        OWNER_PROTOCOL_VERSION,
                        "wired-recovery-request",
                        OwnerSource.CLI,
                        OwnerCommand.DOWNLOAD,
                        OwnerResponseMode.DETACH,
                        {"url": "https://example.test/gallery/wired"},
                        session,
                    )
                    client = OwnerPipeClient(scope)
                    with self.assertRaises(OwnerTransportError):
                        client.request(request, handshake=handshake)

                    callback = shell_constructor.call_args.kwargs["startup_ready_callback"]
                    callback()
                    self.assertEqual(
                        scope.discovery().read_for_scope().lifecycle_state,
                        OwnerLifecycleState.READY,
                    )
                    accepted = client.request(request, handshake=handshake)
                    self.assertEqual(accepted[-1].payload["task_id"], "wired-task")
                    self.assertEqual(accepted[-1].kind.value, "accepted")
                    return 0

                fake_shell.run.side_effect = run_shell
                with patch(
                    "cli_downloader.cli._shell_wiring.InteractiveShell",
                    return_value=fake_shell,
                ) as shell_constructor:
                    exit_code = build_interactive_shell(
                        runtime=runtime,
                        plugins_dir=plugins_dir,
                        runner=runner,
                        background_runner=background_runner,
                    )

            self.assertEqual(exit_code, 0)
