"""Local owner IPC transport built on the standard multiprocessing connection API.

The protocol models remain transport agnostic.  This module supplies the small
local connection used by a visible Shell owner: Windows uses a named pipe and
POSIX development environments use a private Unix socket under the data root.
The endpoint is derived from :class:`OwnerScope`, so two portable data roots do
not share a pipe name.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from multiprocessing.connection import Client, Listener
from pathlib import Path
from threading import Event, Lock, Thread
from typing import TypeAlias

from cli_downloader.core.errors import OwnerError
from cli_downloader.core.owner_protocol import (
    OwnerHandshake,
    OwnerRequest,
    OwnerResponseEvent,
    OwnerResponseMode,
    OwnerResponseStream,
    OwnerRequestLedger,
    RequestDisposition,
    negotiate_handshake,
)
from cli_downloader.core.owner_scope import OwnerScope


class OwnerTransportError(OwnerError):
    """The owner endpoint could not be reached or returned an invalid frame."""

    code = "owner_transport_error"


EventSink: TypeAlias = Callable[[OwnerResponseEvent], None]
RequestHandler: TypeAlias = Callable[[OwnerRequest, EventSink], None]


def endpoint_address(scope: OwnerScope) -> str:
    """Return the platform-local address derived from the owner scope."""

    if os.name == "nt":
        return rf"\\.\pipe\{scope.endpoint_identity}"
    socket_dir = scope.canonical_data_root / "state" / "ipc"
    socket_dir.mkdir(parents=True, exist_ok=True)
    return os.fspath(socket_dir / f"{scope.endpoint_identity}.sock")


def endpoint_metadata_path(scope: OwnerScope) -> Path:
    return scope.canonical_data_root / "state" / "ipc" / f"{scope.endpoint_identity}.json"


def _auth_key(scope: OwnerScope) -> bytes:
    return hashlib.sha256(scope.scope_fingerprint.encode("ascii")).digest()


def _frame(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


class OwnerPipeServer:
    """Serve owner requests on a local pipe/socket until ``stop`` is called."""

    def __init__(
        self,
        scope: OwnerScope,
        *,
        handshake: OwnerHandshake,
        handler: RequestHandler,
    ) -> None:
        self.scope = scope
        self.handshake = handshake
        self.handler = handler
        self.address = endpoint_address(scope)
        # The P5-16 hardening pass will add an explicit Windows Named Pipe ACL.
        # Until then loopback TCP is the reliable local transport on both
        # regular Windows and restricted test runners; the endpoint metadata is
        # still scoped to the data root and authenticated with the scope key.
        self.family = "AF_INET" if os.name == "nt" else "AF_UNIX"
        self._metadata_path = endpoint_metadata_path(scope)
        self._listener: Listener | None = None
        self._stop = Event()
        self._thread: Thread | None = None
        self._connection_threads: list[Thread] = []
        self.ledger = OwnerRequestLedger()
        self._response_cache: dict[str, tuple[OwnerResponseEvent, ...]] = {}
        self._response_lock = Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if os.name != "nt":
            Path(self.address).unlink(missing_ok=True)
        try:
            address = ("127.0.0.1", 0) if self.family == "AF_INET" else self.address
            self._listener = Listener(
                address,
                family=self.family,
                authkey=_auth_key(self.scope),
            )
            if self.family == "AF_INET":
                self.address = self._listener.address
        except (OSError, EOFError) as error:
            raise OwnerTransportError(f"cannot open owner endpoint: {error}") from error
        else:
            self._metadata_path.parent.mkdir(parents=True, exist_ok=True)
            self._metadata_path.write_text(
                json.dumps(
                    {
                        "family": self.family,
                        "address": self.address,
                    }
                ),
                encoding="utf-8",
            )
        self._stop.clear()
        self._thread = Thread(target=self._serve, name="owner-ipc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)
        for worker in tuple(self._connection_threads):
            worker.join(timeout=0.1)
        if os.name != "nt":
            Path(self.address).unlink(missing_ok=True)
        self._metadata_path.unlink(missing_ok=True)

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                break
            worker = Thread(target=self._handle, args=(connection,), daemon=True)
            self._connection_threads.append(worker)
            worker.start()

    def _handle(self, connection) -> None:
        send_lock = Lock()

        def send(payload: bytes) -> None:
            with send_lock:
                connection.send_bytes(payload)

        try:
            client_frame = json.loads(connection.recv_bytes().decode("utf-8"))
            client_handshake = OwnerHandshake.from_dict(client_frame)
            result = negotiate_handshake(client_handshake, self.handshake)
            send(_frame({"frame": "handshake_result", "payload": result.to_dict()}))
            request = OwnerRequest.from_bytes(connection.recv_bytes())
            if client_handshake.session_id != request.client_session_id:
                raise OwnerTransportError("request session does not match handshake session")
            registration = self.ledger.register(request)
            if registration.disposition is RequestDisposition.CONFLICT:
                raise OwnerTransportError("request_id is already used with a different payload")
            if registration.disposition is RequestDisposition.DUPLICATE:
                cached = self._response_cache.get(request.request_id, ())
                if not cached:
                    raise OwnerTransportError("request is already in progress; inspect by Task ID")
                for event in cached:
                    send(event.to_bytes())
                return
            stream = OwnerResponseStream(request.request_id)
            response_events: list[OwnerResponseEvent] = []

            def emit(event: OwnerResponseEvent) -> None:
                stream.push(event)
                response_events.append(event)
                if event.kind.value == "accepted":
                    task_id = event.payload.get("task_id")
                    self.ledger.mark_accepted(
                        event.request_id,
                        task_id=task_id if isinstance(task_id, str) else None,
                    )
                elif event.kind.value == "terminal":
                    status = event.payload.get("status")
                    if isinstance(status, str):
                        from cli_downloader.core.owner_protocol import OwnerTerminalStatus

                        self.ledger.mark_terminal(event.request_id, status=OwnerTerminalStatus(status))
                send(event.to_bytes())

            self.handler(request, emit)
            with self._response_lock:
                self._response_cache[request.request_id] = tuple(response_events)
        except Exception as error:  # endpoint failures must not take down Shell
            try:
                send(
                    _frame(
                        {
                            "frame": "transport_error",
                            "error": {"code": getattr(error, "code", "owner_transport_error"), "message": str(error)},
                        }
                    )
                )
            except (OSError, EOFError):
                pass
        finally:
            try:
                connection.close()
            except OSError:
                pass


class OwnerPipeClient:
    """Submit one request and collect its bounded response stream."""

    def __init__(self, scope: OwnerScope, *, connect_timeout: float = 5.0) -> None:
        self.scope = scope
        self.address = endpoint_address(scope)
        self.family = "AF_INET" if os.name == "nt" else "AF_UNIX"
        self._metadata_path = endpoint_metadata_path(scope)
        self.connect_timeout = max(0.01, float(connect_timeout))

    def request(
        self,
        request: OwnerRequest,
        *,
        handshake: OwnerHandshake,
        on_event: EventSink | None = None,
    ) -> tuple[OwnerResponseEvent, ...]:
        if request.client_session_id != handshake.session_id:
            raise OwnerTransportError("request session does not match handshake session")
        connection = self._connect()
        events: list[OwnerResponseEvent] = []
        stream = OwnerResponseStream(request.request_id)
        try:
            connection.send_bytes(_frame(handshake.to_dict()))
            result = json.loads(connection.recv_bytes().decode("utf-8"))
            if not isinstance(result, dict) or result.get("frame") != "handshake_result":
                raise OwnerTransportError("owner handshake did not return a result")
            connection.send_bytes(request.to_bytes())
            while True:
                raw = connection.recv_bytes()
                if raw.startswith(b'{'):
                    frame = json.loads(raw.decode("utf-8"))
                    if isinstance(frame, dict) and frame.get("frame") == "transport_error":
                        error = frame.get("error", {})
                        message = error.get("message", "owner transport failed") if isinstance(error, dict) else str(error)
                        raise OwnerTransportError(message)
                event = OwnerResponseEvent.from_bytes(raw)
                stream.push(event)
                events.append(event)
                if on_event is not None:
                    on_event(event)
                if event.kind.value in {"terminal", "owner_disconnected"}:
                    break
                if request.response_mode is OwnerResponseMode.DETACH and event.kind.value == "accepted":
                    break
        except (OSError, EOFError, ValueError) as error:
            raise OwnerTransportError(f"owner connection closed before a terminal response: {error}") from error
        finally:
            try:
                connection.close()
            except OSError:
                pass
        return tuple(events)

    def _connect(self):
        # ``multiprocessing.connection.Client`` has no timeout argument.  Keep
        # the connect operation bounded so a stale discovery record cannot hang
        # a CLI process forever.
        result: list[object] = []
        failure: list[BaseException] = []

        address = self.address
        family = self.family
        try:
            metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and metadata.get("family") == "AF_INET":
                if "host" in metadata and "port" in metadata:
                    address = (str(metadata["host"]), int(metadata["port"]))
                else:
                    raw_address = metadata.get("address")
                    if not isinstance(raw_address, (list, tuple)) or len(raw_address) != 2:
                        raise ValueError("invalid owner endpoint address")
                    address = (str(raw_address[0]), int(raw_address[1]))
                family = "AF_INET"
            elif isinstance(metadata, dict) and metadata.get("family") and metadata.get("address"):
                family = str(metadata["family"])
                address = str(metadata["address"])
        except (OSError, ValueError, TypeError, KeyError):
            pass

        def connect() -> None:
            try:
                result.append(
                    Client(
                        address,
                        family=family,
                        authkey=_auth_key(self.scope),
                    )
                )
            except BaseException as error:  # surfaced below as OwnerTransportError
                failure.append(error)

        thread = Thread(target=connect, name="owner-ipc-connect", daemon=True)
        thread.start()
        thread.join(timeout=self.connect_timeout)
        if thread.is_alive():
            raise OwnerTransportError("owner endpoint connection timed out")
        if failure:
            raise OwnerTransportError(f"cannot connect to owner endpoint: {failure[0]}") from failure[0]
        if not result:
            raise OwnerTransportError("owner endpoint returned no connection")
        return result[0]
