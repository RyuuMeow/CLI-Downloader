"""Typed stop signals shared by Shell workers and execution pipelines."""

from __future__ import annotations

from enum import StrEnum
from threading import Event, Lock


class OwnerShutdownRequested(Exception):
    """Internal signal that asks a pipeline to return a resumable checkpoint."""


class OwnerExecutionFenced(Exception):
    """The worker belongs to an older owner/run and must not write state."""


class ExecutionStopReason(StrEnum):
    USER_CANCEL = "user_cancel"
    OWNER_SHUTDOWN = "owner_shutdown"
    OWNER_LOST = "owner_lost"


class ExecutionStopSignal:
    """A backwards-compatible Event-like signal with an explicit stop reason."""

    def __init__(self) -> None:
        self._event = Event()
        self._reason: ExecutionStopReason | None = None
        self._lock = Lock()

    def set(self) -> None:
        """Request the normal user-cancel behavior."""

        self.request(ExecutionStopReason.USER_CANCEL)

    def request(self, reason: ExecutionStopReason) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason
            elif self._reason is ExecutionStopReason.USER_CANCEL:
                # An explicit user cancel always retains terminal-cancel semantics.
                pass
            self._event.set()

    def request_shutdown(self) -> None:
        self.request(ExecutionStopReason.OWNER_SHUTDOWN)

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    @property
    def reason(self) -> ExecutionStopReason | None:
        with self._lock:
            return self._reason

    def is_user_cancel(self) -> bool:
        return self.reason is ExecutionStopReason.USER_CANCEL

    def is_owner_shutdown(self) -> bool:
        return self.reason in {
            ExecutionStopReason.OWNER_SHUTDOWN,
            ExecutionStopReason.OWNER_LOST,
        }
