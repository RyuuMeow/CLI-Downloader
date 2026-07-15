"""Shell discovery, visible launch, and readiness control.

The Shell is a long-lived owner of the Scheduler.  This module deliberately
keeps status and launch policy outside the CLI parser so a packaged executable,
the source checkout, and a future Agent client all use the same rules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from cli_downloader.core.environment import application_root
from cli_downloader.core.errors import (
    OwnerAlreadyRunningError,
    OwnerDiscoveryError,
    OwnerError,
    OwnerLaunchError,
    OwnerLockError,
    OwnerScopeMismatchError,
)
from cli_downloader.core.owner_protocol import (
    OwnerLifecycleState,
    OwnerMode,
)
from cli_downloader.core.owner_scope import (
    OwnerDiscoveryMetadata,
    OwnerScope,
    local_owner_lock_held,
)


class ShellStatusKind(StrEnum):
    ABSENT = "absent"
    STARTING = "starting"
    RECOVERING = "recovering"
    READY = "ready"
    STOPPING = "stopping"
    FOREGROUND_BUSY = "foreground-busy"
    STALE = "stale"
    INCOMPATIBLE = "incompatible"


_STATUS_CODES = {
    ShellStatusKind.ABSENT: "shell_absent",
    ShellStatusKind.STARTING: "shell_starting",
    ShellStatusKind.RECOVERING: "shell_recovering",
    ShellStatusKind.READY: "shell_ready",
    ShellStatusKind.STOPPING: "shell_stopping",
    ShellStatusKind.FOREGROUND_BUSY: "owner_busy",
    ShellStatusKind.STALE: "owner_stale",
    ShellStatusKind.INCOMPATIBLE: "owner_incompatible",
}


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...


ProcessLauncher = Callable[["ShellLaunchSpec"], ProcessHandle]
ProcessAliveProbe = Callable[[int], bool]
Clock = Callable[[], float]
Sleeper = Callable[[float], None]


@dataclass(frozen=True)
class ShellStatusSnapshot:
    """A safe, machine-readable view of the current owner advertisement."""

    scope_fingerprint: str
    kind: ShellStatusKind
    running: bool
    ready: bool
    message: str
    metadata: OwnerDiscoveryMetadata | None = None
    error_code: str | None = None

    @classmethod
    def pending_launch(cls, scope: OwnerScope, message: str) -> "ShellStatusSnapshot":
        return cls(
            scope_fingerprint=scope.scope_fingerprint,
            kind=ShellStatusKind.STARTING,
            running=True,
            ready=False,
            message=message,
        )

    def to_dict(self) -> dict[str, object]:
        metadata = self.metadata
        return {
            "status": self.kind.value,
            "code": self.error_code or _STATUS_CODES[self.kind],
            "running": self.running,
            "ready": self.ready,
            "message": self.message,
            "scope_fingerprint": self.scope_fingerprint,
            "owner_mode": metadata.owner_mode.value if metadata else None,
            "pid": metadata.pid if metadata else None,
            "process_start_identity": metadata.process_start_identity if metadata else None,
            "version": metadata.application_version if metadata else None,
            "protocol_version": metadata.protocol_version if metadata else None,
            "session_id": metadata.session_id if metadata else None,
            "endpoint_identity": metadata.endpoint_identity if metadata else None,
            "capabilities": list(metadata.capabilities) if metadata else [],
            "lifecycle_state": metadata.lifecycle_state.value if metadata else None,
            "recovery_state": metadata.lifecycle_state.value if metadata else None,
        }


@dataclass(frozen=True)
class ShellLaunchSpec:
    """The complete command needed to start a visible Shell."""

    command: tuple[str, ...]
    cwd: Path
    visible: bool
    creationflags: int = 0
    start_new_session: bool = False


@dataclass(frozen=True)
class ShellControlResult:
    """Result of ``shell start`` or ``shell ensure``."""

    action: str
    status: ShellStatusSnapshot
    launched: bool
    waited: bool
    message: str
    process_id: int | None = None
    exit_code: int = 0

    def to_dict(self) -> dict[str, object]:
        payload = self.status.to_dict()
        payload.update(
            {
                "action": self.action,
                "launched": self.launched,
                "waited": self.waited,
                "message": self.message,
                "process_id": self.process_id,
                "exit_code": self.exit_code,
            }
        )
        return payload


def resolve_shell_launch(
    *,
    extra_args: Sequence[str] = (),
    frozen: bool | None = None,
    executable: str | os.PathLike[str] | None = None,
    platform_name: str | None = None,
    source_root: Path | None = None,
) -> ShellLaunchSpec:
    """Resolve the current source or packaged Shell command.

    Source mode pins the checkout as ``cwd`` so ``python -m cli_downloader``
    works even when the caller was launched from another directory.  Frozen
    mode pins the executable directory for the same reason.
    """

    is_frozen = bool(getattr(sys, "frozen", False) if frozen is None else frozen)
    executable_path = Path(executable or sys.executable).expanduser().resolve()
    platform_value = platform_name or os.name
    arguments = tuple(str(value) for value in extra_args)
    if is_frozen:
        command = (str(executable_path), *arguments, "shell")
        cwd = executable_path.parent
    else:
        command = (str(executable_path), "-m", "cli_downloader", *arguments, "shell")
        cwd = (source_root or application_root()).resolve()
    creationflags = (
        int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0)) if platform_value == "nt" else 0
    )
    return ShellLaunchSpec(
        command=command,
        cwd=cwd,
        visible=True,
        creationflags=creationflags,
        start_new_session=platform_value != "nt",
    )


def launch_visible_shell(
    spec: ShellLaunchSpec,
    *,
    popen_factory: Callable[..., ProcessHandle] = subprocess.Popen,
) -> ProcessHandle:
    """Launch a Shell with a new visible Windows console when available."""

    kwargs: dict[str, object] = {"cwd": str(spec.cwd)}
    if spec.creationflags:
        kwargs["creationflags"] = spec.creationflags
    elif spec.start_new_session:
        kwargs["start_new_session"] = True
    try:
        return popen_factory(spec.command, **kwargs)
    except (OSError, ValueError) as error:
        raise OwnerLaunchError(f"cannot launch Shell: {error}") from error


def is_process_alive(pid: int) -> bool:
    """Return whether a process ID currently exists without exposing secrets."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ShellStatusService:
    """Read discovery metadata and verify that the OS lock is still held."""

    def __init__(
        self,
        scope: OwnerScope,
        *,
        process_alive: ProcessAliveProbe = is_process_alive,
    ) -> None:
        self.scope = scope
        self._process_alive = process_alive

    def inspect(self) -> ShellStatusSnapshot:
        store = self.scope.discovery()
        try:
            metadata = store.read_for_scope()
        except OwnerScopeMismatchError as error:
            return self._incompatible(error.code, str(error))
        except OwnerDiscoveryError as error:
            return self._incompatible(error.code, str(error))

        lock_held = self._lock_held()
        if lock_held is None:
            return self._incompatible("owner_lock_error", "owner lock could not be inspected")

        if metadata is None:
            if lock_held:
                return ShellStatusSnapshot(
                    self.scope.scope_fingerprint,
                    ShellStatusKind.STARTING,
                    running=True,
                    ready=False,
                    message="Shell is starting and has not published readiness yet",
                )
            return ShellStatusSnapshot(
                self.scope.scope_fingerprint,
                ShellStatusKind.ABSENT,
                running=False,
                ready=False,
                message="No Shell owner is running",
            )

        if not self._metadata_process_identity_is_consistent(metadata):
            return self._incompatible(
                "owner_metadata_error",
                "owner metadata process identity does not match its PID",
            )
        if not self._process_alive(metadata.pid) or not lock_held:
            return ShellStatusSnapshot(
                self.scope.scope_fingerprint,
                ShellStatusKind.STALE,
                running=False,
                ready=False,
                message="Owner metadata is stale and can be replaced",
                metadata=metadata,
            )

        kind = self._kind_for_metadata(metadata)
        running = kind not in {ShellStatusKind.ABSENT, ShellStatusKind.STALE}
        ready = kind is ShellStatusKind.READY
        return ShellStatusSnapshot(
            self.scope.scope_fingerprint,
            kind,
            running=running,
            ready=ready,
            message=self._message_for_kind(kind),
            metadata=metadata,
        )

    def _lock_held(self) -> bool | None:
        if local_owner_lock_held(self.scope.lock_path):
            return True
        try:
            lease = self.scope.lock().acquire()
        except OwnerAlreadyRunningError:
            return True
        except (OwnerLockError, OwnerError):
            return None
        lease.release()
        return False

    @staticmethod
    def _metadata_process_identity_is_consistent(metadata: OwnerDiscoveryMetadata) -> bool:
        prefix, separator, _ = metadata.process_start_identity.partition("-")
        return bool(separator) and prefix == str(metadata.pid)

    @staticmethod
    def _kind_for_metadata(metadata: OwnerDiscoveryMetadata) -> ShellStatusKind:
        if metadata.lifecycle_state is OwnerLifecycleState.STARTING:
            return ShellStatusKind.STARTING
        if metadata.lifecycle_state is OwnerLifecycleState.RECOVERING:
            return ShellStatusKind.RECOVERING
        if metadata.lifecycle_state is OwnerLifecycleState.STOPPING:
            return ShellStatusKind.STOPPING
        if (
            metadata.lifecycle_state is OwnerLifecycleState.BUSY
            and metadata.owner_mode is OwnerMode.FOREGROUND
        ):
            return ShellStatusKind.FOREGROUND_BUSY
        if metadata.lifecycle_state is OwnerLifecycleState.INCOMPATIBLE:
            return ShellStatusKind.INCOMPATIBLE
        return ShellStatusKind.READY

    @staticmethod
    def _message_for_kind(kind: ShellStatusKind) -> str:
        return {
            ShellStatusKind.STARTING: "Shell is starting",
            ShellStatusKind.RECOVERING: "Shell is recovering persisted work",
            ShellStatusKind.READY: "Shell is ready to accept commands",
            ShellStatusKind.STOPPING: "Shell is stopping",
            ShellStatusKind.FOREGROUND_BUSY: "A foreground command owns the Scheduler",
            ShellStatusKind.INCOMPATIBLE: "Owner metadata is incompatible with this CLI",
        }.get(kind, "Shell owner is not available")

    def _incompatible(self, code: str, message: str) -> ShellStatusSnapshot:
        snapshot = ShellStatusSnapshot(
            self.scope.scope_fingerprint,
            ShellStatusKind.INCOMPATIBLE,
            running=False,
            ready=False,
            message=message,
            error_code=code,
        )
        return snapshot


class ShellController:
    """Implement idempotent Shell start and ready waiting."""

    def __init__(
        self,
        scope: OwnerScope,
        *,
        status_service: ShellStatusService | None = None,
        launcher: ProcessLauncher = launch_visible_shell,
        clock: Clock = time.monotonic,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self.scope = scope
        self.status_service = status_service or ShellStatusService(scope)
        self._launcher = launcher
        self._clock = clock
        self._sleep = sleeper

    def status(self) -> ShellStatusSnapshot:
        return self.status_service.inspect()

    def start(
        self,
        *,
        wait_ready: bool = False,
        timeout: float = 10.0,
        extra_args: Sequence[str] = (),
        ensure: bool = False,
    ) -> ShellControlResult:
        before = self.status()
        if before.kind is ShellStatusKind.FOREGROUND_BUSY:
            return self._failure("start", before, "foreground owner is active")
        if before.kind is ShellStatusKind.INCOMPATIBLE:
            return self._failure("start", before, before.message)
        if before.kind is ShellStatusKind.READY:
            return self._success("start", before, "Shell is already ready")
        if before.kind in {ShellStatusKind.STARTING, ShellStatusKind.RECOVERING}:
            if wait_ready:
                return self._wait("start", before, None, timeout, ensure=ensure)
            return self._success("start", before, "Shell startup is already in progress")
        if before.kind is ShellStatusKind.STOPPING:
            if not ensure:
                return self._failure("start", before, "Shell is stopping; retry after it exits")
            waiting = self._wait_for_absent(timeout)
            if waiting is not None:
                return self._failure("ensure", waiting, waiting.message)
            before = self.status()
        if before.kind not in {ShellStatusKind.ABSENT, ShellStatusKind.STALE}:
            return self._failure("start", before, before.message)

        spec = resolve_shell_launch(extra_args=extra_args)
        try:
            process = self._launcher(spec)
        except OwnerLaunchError as error:
            snapshot = ShellStatusSnapshot(
                self.scope.scope_fingerprint,
                ShellStatusKind.INCOMPATIBLE,
                running=False,
                ready=False,
                message=str(error),
                error_code=error.code,
            )
            return ShellControlResult(
                action="ensure" if ensure else "start",
                status=snapshot,
                launched=False,
                waited=False,
                message=str(error),
                exit_code=1,
            )
        process_id = getattr(process, "pid", None)
        pending = ShellStatusSnapshot.pending_launch(self.scope, "Shell launch requested")
        if not wait_ready:
            return ShellControlResult(
                action="ensure" if ensure else "start",
                status=pending,
                launched=True,
                waited=False,
                message="Shell launch requested",
                process_id=process_id,
            )
        return self._wait(
            "ensure" if ensure else "start",
            pending,
            process,
            timeout,
            ensure=ensure,
            launched=True,
        )

    def ensure(
        self,
        *,
        timeout: float = 10.0,
        extra_args: Sequence[str] = (),
    ) -> ShellControlResult:
        return self.start(
            wait_ready=True,
            timeout=timeout,
            extra_args=extra_args,
            ensure=True,
        )

    def _wait(
        self,
        action: str,
        initial: ShellStatusSnapshot,
        process: ProcessHandle | None,
        timeout: float,
        *,
        ensure: bool,
        launched: bool = False,
    ) -> ShellControlResult:
        if timeout <= 0:
            timeout = 0.01
        deadline = self._clock() + timeout
        while True:
            current = self.status()
            if current.kind is ShellStatusKind.READY:
                return ShellControlResult(
                    action=action,
                    status=current,
                    launched=launched,
                    waited=True,
                    message="Shell is ready",
                    process_id=getattr(process, "pid", None),
                )
            if current.kind is ShellStatusKind.FOREGROUND_BUSY:
                return self._failure(action, current, current.message, launched=launched)
            if current.kind is ShellStatusKind.INCOMPATIBLE:
                return self._failure(action, current, current.message, launched=launched)
            if process is not None:
                exit_code = process.poll()
                if exit_code is not None and current.kind in {
                    ShellStatusKind.ABSENT,
                    ShellStatusKind.STALE,
                }:
                    failed = ShellStatusSnapshot(
                        self.scope.scope_fingerprint,
                        ShellStatusKind.INCOMPATIBLE,
                        running=False,
                        ready=False,
                        message=f"Shell exited before becoming ready (exit code {exit_code})",
                        error_code="owner_launch_error",
                    )
                    return ShellControlResult(
                        action=action,
                        status=failed,
                        launched=launched,
                        waited=True,
                        message=failed.message,
                        process_id=getattr(process, "pid", None),
                        exit_code=1,
                    )
            if self._clock() >= deadline:
                timed_out = ShellStatusSnapshot(
                    self.scope.scope_fingerprint,
                    ShellStatusKind.INCOMPATIBLE,
                    running=current.running,
                    ready=False,
                    message=f"Shell did not become ready within {timeout:g} seconds",
                    metadata=current.metadata,
                    error_code="owner_ready_timeout",
                )
                return ShellControlResult(
                    action=action,
                    status=timed_out,
                    launched=launched,
                    waited=True,
                    message=timed_out.message,
                    process_id=getattr(process, "pid", None),
                    exit_code=1,
                )
            self._sleep(min(0.05, max(0.001, deadline - self._clock())))

    def _wait_for_absent(self, timeout: float) -> ShellStatusSnapshot | None:
        deadline = self._clock() + max(timeout, 0.01)
        while True:
            current = self.status()
            if current.kind in {ShellStatusKind.ABSENT, ShellStatusKind.STALE}:
                return None
            if self._clock() >= deadline:
                return current
            self._sleep(min(0.05, max(0.001, deadline - self._clock())))

    @staticmethod
    def _success(
        action: str,
        status: ShellStatusSnapshot,
        message: str,
    ) -> ShellControlResult:
        return ShellControlResult(action, status, False, False, message)

    @staticmethod
    def _failure(
        action: str,
        status: ShellStatusSnapshot,
        message: str,
        *,
        launched: bool = False,
    ) -> ShellControlResult:
        return ShellControlResult(action, status, launched, False, message, exit_code=1)


__all__ = [
    "ShellControlResult",
    "ShellController",
    "ShellLaunchSpec",
    "ShellStatusKind",
    "ShellStatusService",
    "ShellStatusSnapshot",
    "is_process_alive",
    "launch_visible_shell",
    "resolve_shell_launch",
]
