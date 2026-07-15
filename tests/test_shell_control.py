from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from cli_downloader.core.errors import OwnerLaunchError
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerLifecycleState,
    OwnerMode,
)
from cli_downloader.core.owner_scope import (
    OwnerDiscoveryMetadata,
    OwnerManager,
    OwnerScope,
    current_process_start_identity,
)
from cli_downloader.core.shell_control import (
    ShellController,
    ShellLaunchSpec,
    ShellStatusKind,
    ShellStatusService,
    ShellStatusSnapshot,
    launch_visible_shell,
    resolve_shell_launch,
)
from cli_downloader.cli.app import main


def _metadata(
    scope: OwnerScope,
    *,
    mode: OwnerMode = OwnerMode.SHELL,
    lifecycle: OwnerLifecycleState = OwnerLifecycleState.READY,
) -> OwnerDiscoveryMetadata:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    return OwnerDiscoveryMetadata(
        scope_fingerprint=scope.scope_fingerprint,
        protocol_version=OWNER_PROTOCOL_VERSION,
        application_version="0.1.5",
        owner_mode=mode,
        lifecycle_state=lifecycle,
        pid=os.getpid(),
        process_start_identity=current_process_start_identity(),
        session_id="shell-session",
        endpoint_identity=scope.endpoint_identity,
        capabilities=("download", "shell_status"),
        created_at=now,
        updated_at=now,
    )


class ShellStatusServiceTests(unittest.TestCase):
    def test_absent_owner_is_distinguished_from_starting_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            service = ShellStatusService(scope)
            self.assertEqual(service.inspect().kind, ShellStatusKind.ABSENT)

            with scope.lock().acquire():
                self.assertEqual(service.inspect().kind, ShellStatusKind.STARTING)

    def test_ready_recovering_foreground_and_stale_states_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            service = ShellStatusService(scope, process_alive=lambda _pid: True)

            with OwnerManager(scope).acquire(_metadata(scope)):
                ready = service.inspect()
                self.assertEqual(ready.kind, ShellStatusKind.READY)
                self.assertTrue(ready.ready)
                self.assertEqual(ready.to_dict()["pid"], os.getpid())

            with OwnerManager(scope).acquire(
                _metadata(scope, lifecycle=OwnerLifecycleState.RECOVERING)
            ):
                self.assertEqual(service.inspect().kind, ShellStatusKind.RECOVERING)

            with OwnerManager(scope).acquire(
                _metadata(scope, lifecycle=OwnerLifecycleState.STOPPING)
            ):
                self.assertEqual(service.inspect().kind, ShellStatusKind.STOPPING)

            with OwnerManager(scope).acquire(
                _metadata(scope, mode=OwnerMode.FOREGROUND, lifecycle=OwnerLifecycleState.BUSY)
            ):
                busy = service.inspect()
                self.assertEqual(busy.kind, ShellStatusKind.FOREGROUND_BUSY)
                self.assertEqual(busy.to_dict()["code"], "owner_busy")

            stale_metadata = _metadata(scope)
            scope.discovery().write(stale_metadata)
            stale = ShellStatusService(scope, process_alive=lambda _pid: False).inspect()
            self.assertEqual(stale.kind, ShellStatusKind.STALE)

    def test_incompatible_metadata_has_stable_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory, user_identity="user-one")
            other = OwnerScope.from_data_root(directory, user_identity="user-two")
            payload = _metadata(other).to_dict()
            scope.discovery_path.parent.mkdir(parents=True)
            scope.discovery_path.write_text(json.dumps(payload), encoding="utf-8")

            snapshot = ShellStatusService(scope).inspect()
            self.assertEqual(snapshot.kind, ShellStatusKind.INCOMPATIBLE)
            self.assertEqual(snapshot.to_dict()["code"], "owner_scope_mismatch")


class ShellLaunchResolutionTests(unittest.TestCase):
    def test_source_launch_is_pinned_to_active_python_and_source_root(self) -> None:
        spec = resolve_shell_launch(
            executable=r"C:\Python312\python.exe",
            frozen=False,
            platform_name="nt",
            source_root=Path(r"C:\repo\CLI-Downloader"),
        )
        self.assertEqual(
            spec.command,
            (r"C:\Python312\python.exe", "-m", "cli_downloader", "shell"),
        )
        self.assertEqual(spec.cwd, Path(r"C:\repo\CLI-Downloader"))
        self.assertEqual(spec.creationflags, int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0)))
        self.assertFalse(spec.start_new_session)

    def test_packaged_launch_uses_executable_directory_and_forwards_global_args(self) -> None:
        spec = resolve_shell_launch(
            executable=r"C:\Apps\CLI-Downloader\CLI-Downloader.exe",
            frozen=True,
            platform_name="nt",
            extra_args=("--plugin-root", r"C:\Plugins"),
        )
        self.assertEqual(
            spec.command,
            (
                r"C:\Apps\CLI-Downloader\CLI-Downloader.exe",
                "--plugin-root",
                r"C:\Plugins",
                "shell",
            ),
        )
        self.assertEqual(spec.cwd, Path(r"C:\Apps\CLI-Downloader"))

    def test_visible_launch_uses_new_console_on_windows(self) -> None:
        process = Mock(pid=1234)
        popen = Mock(return_value=process)
        spec = ShellLaunchSpec(
            command=("CLI-Downloader.exe", "shell"),
            cwd=Path(r"C:\Apps\CLI-Downloader"),
            visible=True,
            creationflags=42,
        )

        self.assertIs(launch_visible_shell(spec, popen_factory=popen), process)
        self.assertEqual(
            popen.call_args.kwargs,
            {"cwd": str(spec.cwd), "creationflags": 42},
        )


class ShellControllerTests(unittest.TestCase):
    def _snapshot(self, scope: OwnerScope, kind: ShellStatusKind) -> ShellStatusSnapshot:
        metadata = None
        if kind in {
            ShellStatusKind.READY,
            ShellStatusKind.RECOVERING,
            ShellStatusKind.FOREGROUND_BUSY,
        }:
            lifecycle = {
                ShellStatusKind.READY: OwnerLifecycleState.READY,
                ShellStatusKind.RECOVERING: OwnerLifecycleState.RECOVERING,
                ShellStatusKind.FOREGROUND_BUSY: OwnerLifecycleState.BUSY,
            }[kind]
            mode = OwnerMode.FOREGROUND if kind is ShellStatusKind.FOREGROUND_BUSY else OwnerMode.SHELL
            metadata = _metadata(scope, mode=mode, lifecycle=lifecycle)
        return ShellStatusSnapshot(
            scope.scope_fingerprint,
            kind,
            running=kind not in {ShellStatusKind.ABSENT, ShellStatusKind.STALE},
            ready=kind is ShellStatusKind.READY,
            message=kind.value,
            metadata=metadata,
        )

    def test_ensure_is_idempotent_when_shell_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            service = Mock()
            service.inspect.return_value = self._snapshot(scope, ShellStatusKind.READY)
            launcher = Mock()
            result = ShellController(scope, status_service=service, launcher=launcher).ensure()

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.launched)
            launcher.assert_not_called()

    def test_start_waits_for_ready_and_launches_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            service = Mock()
            service.inspect.side_effect = [
                self._snapshot(scope, ShellStatusKind.ABSENT),
                self._snapshot(scope, ShellStatusKind.STARTING),
                self._snapshot(scope, ShellStatusKind.READY),
            ]
            process = Mock(pid=4321)
            process.poll.return_value = None
            launcher = Mock(return_value=process)
            result = ShellController(
                scope,
                status_service=service,
                launcher=launcher,
                sleeper=lambda _seconds: None,
            ).start(wait_ready=True)

            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.launched)
            self.assertTrue(result.waited)
            self.assertEqual(result.status.kind, ShellStatusKind.READY)
            launcher.assert_called_once()

    def test_start_reports_foreground_owner_without_launching_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            service = Mock()
            service.inspect.return_value = self._snapshot(scope, ShellStatusKind.FOREGROUND_BUSY)
            launcher = Mock()
            result = ShellController(scope, status_service=service, launcher=launcher).ensure()

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.status.to_dict()["code"], "owner_busy")
            self.assertEqual(result.status.to_dict()["pid"], os.getpid())
            self.assertEqual(result.status.to_dict()["session_id"], "shell-session")
            launcher.assert_not_called()

    def test_start_detects_early_exit_and_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            absent = self._snapshot(scope, ShellStatusKind.ABSENT)
            service = Mock()
            service.inspect.side_effect = [absent, absent]
            process = Mock(pid=99)
            process.poll.return_value = 7
            result = ShellController(
                scope,
                status_service=service,
                launcher=Mock(return_value=process),
            ).start(wait_ready=True)
            self.assertEqual(result.status.to_dict()["code"], "owner_launch_error")

            service = Mock()
            service.inspect.return_value = absent
            process.poll.return_value = None
            now = [0.0]
            result = ShellController(
                scope,
                status_service=service,
                launcher=Mock(return_value=process),
                clock=lambda: now[0],
                sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
            ).start(wait_ready=True, timeout=0.1)
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.status.to_dict()["code"], "owner_ready_timeout")

    def test_launcher_error_is_reported_as_stable_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            service = Mock()
            service.inspect.return_value = self._snapshot(scope, ShellStatusKind.ABSENT)

            def fail(_spec: ShellLaunchSpec):
                raise OwnerLaunchError("permission denied")

            result = ShellController(scope, status_service=service, launcher=fail).start()
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.status.to_dict()["code"], "owner_launch_error")


class ShellCommandTests(unittest.TestCase):
    def test_status_command_emits_machine_readable_absent_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with redirect_stdout(output):
                    exit_code = main(["shell", "status", "--json"])

            self.assertEqual(exit_code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "absent")
            self.assertEqual(payload["code"], "shell_absent")

    def test_ensure_command_passes_visible_wait_and_global_arguments_to_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            result = Mock()
            result.to_dict.return_value = {"status": "ready", "code": "shell_ready"}
            result.exit_code = 0
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with patch("cli_downloader.cli.app.ShellController") as controller_type:
                    controller_type.return_value.ensure.return_value = result
                    with redirect_stdout(output):
                        exit_code = main(
                            [
                                "--plugin-root",
                                "plugins",
                                "--enable-external-code",
                                "shell",
                                "ensure",
                                "--visible",
                                "--wait-ready",
                                "--timeout",
                                "3",
                                "--json",
                            ]
                        )

            self.assertEqual(exit_code, 0)
            controller_type.return_value.ensure.assert_called_once_with(
                timeout=3.0,
                extra_args=("--plugin-root", "plugins", "--enable-external-code"),
            )
            self.assertEqual(json.loads(output.getvalue())["status"], "ready")


if __name__ == "__main__":
    unittest.main()
