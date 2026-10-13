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
import tempfile
import ctypes
from collections.abc import Callable
from multiprocessing.connection import Client, Listener, wait as wait_connections
from pathlib import Path
from threading import BoundedSemaphore, Event, Lock, Thread
from time import monotonic, sleep
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
    OwnerDisconnectOutcome,
    OwnerWaitPhase,
    OwnerWaitPolicy,
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


def _write_endpoint_metadata(
    path: Path, *, family: str, address: object, scope: OwnerScope
) -> None:
    """Publish only scoped, non-secret endpoint coordinates atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "family": family,
        "address": address,
        "protocol_version": scope.protocol_version,
        "scope_fingerprint": scope.scope_fingerprint,
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        # Windows may briefly deny replacing the discovery file while a
        # client is re-reading it during an AF_PIPE -> loopback fallback.
        # Keep the operation atomic, but retry the transient sharing violation
        # for a bounded interval instead of killing the owner listener thread.
        replaced = False
        for _attempt in range(20):
            try:
                os.replace(temporary, path)
            except PermissionError:
                sleep(0.01)
            else:
                replaced = True
                temporary = None
                break
        if not replaced:
            os.replace(temporary, path)
            temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class OwnerRequestPersistence:
    """Crash-safe, bounded response cache for request identity recovery."""

    def __init__(self, scope: OwnerScope, *, maximum_records: int = 2048) -> None:
        self.path = scope.canonical_data_root / "state" / "owner-requests.json"
        self.maximum_records = maximum_records
        self._lock = Lock()
        self._records: dict[str, dict[str, object]] = {}
        self._load()

    def get(self, request: OwnerRequest) -> tuple[OwnerResponseEvent, ...] | None:
        fingerprint = _request_fingerprint(request)
        with self._lock:
            record = self._records.get(request.request_id)
            if record is None:
                return None
            if record.get("fingerprint") != fingerprint:
                raise OwnerTransportError("request_id is already used with a different payload")
            events = record.get("events", ())
            if not isinstance(events, list):
                return ()
            return tuple(OwnerResponseEvent.from_dict(event) for event in events)

    def append(self, request: OwnerRequest, event: OwnerResponseEvent) -> None:
        fingerprint = _request_fingerprint(request)
        with self._lock:
            record = self._records.setdefault(
                request.request_id,
                {"fingerprint": fingerprint, "events": []},
            )
            if record.get("fingerprint") != fingerprint:
                raise OwnerTransportError("request_id is already used with a different payload")
            events = record.setdefault("events", [])
            if isinstance(events, list):
                events.append(event.to_dict())
            while len(self._records) > self.maximum_records:
                self._records.pop(next(iter(self._records)))
            self._write()

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for request_id, record in payload.items():
            if not isinstance(request_id, str) or not isinstance(record, dict):
                continue
            events = record.get("events", [])
            if not isinstance(events, list):
                continue
            try:
                tuple(OwnerResponseEvent.from_dict(event) for event in events)
            except Exception:
                continue
            self._records[request_id] = {
                "fingerprint": record.get("fingerprint"),
                "events": events,
            }

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    self._records,
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _request_fingerprint(request: OwnerRequest) -> str:
    return hashlib.sha256(request.to_bytes(max_frame_bytes=10 * 1024 * 1024)).hexdigest()


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
        self.family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        self._metadata_path = endpoint_metadata_path(scope)
        self._listener: Listener | None = None
        self._stop = Event()
        self._thread: Thread | None = None
        self._connection_threads: list[Thread] = []
        self.ledger = OwnerRequestLedger()
        self._response_cache: dict[str, tuple[OwnerResponseEvent, ...]] = {}
        self._response_lock = Lock()
        self._connection_slots = BoundedSemaphore(32)
        self.wait_policy = OwnerWaitPolicy()
        self.persistence = OwnerRequestPersistence(scope)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if os.name != "nt":
            Path(self.address).unlink(missing_ok=True)
        try:
            self._listener = Listener(
                self.address,
                family=self.family,
                authkey=_auth_key(self.scope),
            )
        except PermissionError as error:
            if os.name != "nt":
                raise OwnerTransportError(f"cannot open owner endpoint: {error}") from error
            # Some constrained runners deny CreateNamedPipe even for the
            # current user. Keep the fallback loopback-only, ephemeral, and
            # authenticated with the same scope key; normal Windows installs
            # remain on AF_PIPE.
            self._switch_to_loopback()
        except (OSError, EOFError) as error:
            raise OwnerTransportError(f"cannot open owner endpoint: {error}") from error
        else:
            _write_endpoint_metadata(
                self._metadata_path,
                family=self.family,
                address=self.address,
                scope=self.scope,
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
            except PermissionError:
                if self.family == "AF_PIPE" and os.name == "nt":
                    self._switch_to_loopback()
                    listener = self._listener
                    continue
                break
            except (OSError, EOFError):
                break
            if not self._connection_slots.acquire(blocking=False):
                try:
                    connection.close()
                except OSError:
                    pass
                continue
            worker = Thread(target=self._handle_with_slot, args=(connection,), daemon=True)
            self._connection_threads.append(worker)
            worker.start()

    def _switch_to_loopback(self) -> None:
        listener = self._listener
        if listener is not None:
            listener.close()
        self.family = "AF_INET"
        self._listener = Listener(
            ("127.0.0.1", 0),
            family=self.family,
            authkey=_auth_key(self.scope),
        )
        self.address = self._listener.address
        _write_endpoint_metadata(
            self._metadata_path,
            family=self.family,
            address=self.address,
            scope=self.scope,
        )

    def _handle_with_slot(self, connection) -> None:
        try:
            self._handle(connection)
        finally:
            self._connection_slots.release()

    def _handle(self, connection) -> None:
        send_lock = Lock()
        request: OwnerRequest | None = None
        stream: OwnerResponseStream | None = None

        def send(payload: bytes) -> None:
            with send_lock:
                connection.send_bytes(payload)

        try:
            client_frame = json.loads(
                self._recv_bytes(connection, self.wait_policy.handshake_timeout).decode("utf-8")
            )
            client_handshake = OwnerHandshake.from_dict(client_frame)
            result = negotiate_handshake(client_handshake, self.handshake)
            send(_frame({"frame": "handshake_result", "payload": result.to_dict()}))
            request = OwnerRequest.from_bytes(
                self._recv_bytes(connection, self.wait_policy.acceptance_timeout)
            )
            if client_handshake.session_id != request.client_session_id:
                raise OwnerTransportError("request session does not match handshake session")
            persisted = self.persistence.get(request)
            if persisted is not None:
                if not persisted:
                    raise OwnerTransportError(
                        "request is already persisted without a response; inspect the Task by ID"
                    )
                for event in persisted:
                    send(event.to_bytes())
                return
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

                        self.ledger.mark_terminal(
                            event.request_id, status=OwnerTerminalStatus(status)
                        )
                self.persistence.append(request, event)
                send(event.to_bytes())

            self.handler(request, emit)
            with self._response_lock:
                self._response_cache[request.request_id] = tuple(response_events)
        except Exception as error:  # endpoint failures must not take down Shell
            if request is not None and (stream is None or not stream.accepted):
                # No durable Task/acceptance event exists for this request;
                # allow the same request ID to be retried after a transient
                # admission state such as RECOVERING.
                self.ledger.discard(request.request_id)
            try:
                send(
                    _frame(
                        {
                            "frame": "transport_error",
                            "error": {
                                "code": getattr(error, "code", "owner_transport_error"),
                                "message": str(error),
                            },
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

    @staticmethod
    def _recv_bytes(connection, timeout: float) -> bytes:
        if not wait_connections([connection], timeout):
            raise OwnerTransportError("owner connection response timed out")
        return connection.recv_bytes()


class OwnerPipeClient:
    """Submit one request and collect its bounded response stream."""

    def __init__(
        self,
        scope: OwnerScope,
        *,
        connect_timeout: float = 5.0,
        wait_policy: OwnerWaitPolicy | None = None,
    ) -> None:
        self.scope = scope
        self.address = endpoint_address(scope)
        self.family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        self._metadata_path = endpoint_metadata_path(scope)
        self.connect_timeout = max(0.01, float(connect_timeout))
        self.wait_policy = wait_policy or OwnerWaitPolicy(connect_timeout=self.connect_timeout)

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
            result = json.loads(
                self._recv_bytes(connection, self.wait_policy.handshake_timeout).decode("utf-8")
            )
            if not isinstance(result, dict) or result.get("frame") != "handshake_result":
                raise OwnerTransportError("owner handshake did not return a result")
            connection.send_bytes(request.to_bytes())
            while True:
                wait_timeout = (
                    self.wait_policy.response_idle_timeout
                    if stream.accepted
                    else self.wait_policy.acceptance_timeout
                )
                raw = self._recv_bytes(connection, wait_timeout)
                if raw.startswith(b"{"):
                    frame = json.loads(raw.decode("utf-8"))
                    if isinstance(frame, dict) and frame.get("frame") == "transport_error":
                        error = frame.get("error", {})
                        message = (
                            error.get("message", "owner transport failed")
                            if isinstance(error, dict)
                            else str(error)
                        )
                        raise OwnerTransportError(message)
                event = OwnerResponseEvent.from_bytes(raw)
                stream.push(event)
                events.append(event)
                if on_event is not None:
                    on_event(event)
                if event.kind.value in {"terminal", "owner_disconnected"}:
                    break
                if (
                    request.response_mode is OwnerResponseMode.DETACH
                    and event.kind.value == "accepted"
                ):
                    break
        except (OSError, EOFError, ValueError, OwnerTransportError) as error:
            if stream.accepted:
                task_id = next(
                    (
                        event.payload.get("task_id")
                        for event in reversed(events)
                        if isinstance(event.payload.get("task_id"), str)
                    ),
                    None,
                )
                owner_session_id = next(
                    (
                        event.payload.get("owner_session_id")
                        for event in events
                        if isinstance(event.payload.get("owner_session_id"), str)
                    ),
                    "unknown-owner",
                )
                disconnected = OwnerResponseEvent.owner_disconnected(
                    request.request_id,
                    stream.last_sequence + 1,
                    phase=OwnerWaitPhase.RESPONSE_IDLE,
                    outcome=OwnerDisconnectOutcome.ACCEPTED_COMPLETION_UNKNOWN,
                    known_task_id=task_id if isinstance(task_id, str) else None,
                    owner_session_id=owner_session_id,
                    last_sequence=stream.last_sequence,
                    message="owner connection closed before terminal result; inspect the Task by ID",
                )
                stream.push(disconnected)
                events.append(disconnected)
                if on_event is not None:
                    on_event(disconnected)
                return tuple(events)
            raise OwnerTransportError(
                f"owner connection closed before acceptance or terminal response: {error}"
            ) from error
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
        deadline = monotonic() + self.connect_timeout
        last_failure: BaseException | None = None
        while monotonic() < deadline:
            address, family = self._load_endpoint()
            if family == "AF_PIPE":
                try:
                    self._wait_for_windows_pipe(str(address), deadline=deadline)
                except OwnerTransportError as error:
                    # A constrained Windows runner may reject AF_PIPE on its
                    # first accept and atomically republish an AF_INET fallback.
                    # Re-read endpoint metadata until the bounded deadline.
                    last_failure = error
                    sleep(0.05)
                    continue
                try:
                    return Client(address, family=family, authkey=_auth_key(self.scope))
                except OSError as error:
                    last_failure = error
                    sleep(0.05)
                    continue
            result: list[object] = []
            failure: list[BaseException] = []

            def connect() -> None:
                try:
                    result.append(Client(address, family=family, authkey=_auth_key(self.scope)))
                except BaseException as error:  # surfaced below as OwnerTransportError
                    failure.append(error)

            thread = Thread(target=connect, name="owner-ipc-connect", daemon=True)
            thread.start()
            thread.join(timeout=max(0.01, min(0.5, deadline - monotonic())))
            if thread.is_alive():
                raise OwnerTransportError("owner endpoint connection timed out")
            if result:
                return result[0]
            if failure:
                last_failure = failure[0]
            sleep(0.05)
        raise OwnerTransportError(
            f"cannot connect to owner endpoint: {last_failure}"
        ) from last_failure

    @staticmethod
    def _wait_for_windows_pipe(address: str, *, deadline: float) -> None:
        """Bound the Windows named-pipe availability check without a Python thread.

        ``multiprocessing.connection.Client`` may enter a blocking Win32 call
        that holds the GIL.  In that state even ``Thread.start()`` in the
        caller cannot provide a reliable timeout.  ``WaitNamedPipeW`` has a
        native timeout and ctypes releases the GIL while it waits.
        """

        if os.name != "nt":
            return
        remaining_ms = max(1, min(250, int((deadline - monotonic()) * 1000)))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        kernel32.WaitNamedPipeW.restype = ctypes.c_int
        if kernel32.WaitNamedPipeW(address, remaining_ms):
            return
        error = ctypes.get_last_error()
        raise OwnerTransportError(f"owner named-pipe endpoint is unavailable (WinError {error})")

    def _load_endpoint(self):
        try:
            metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise OwnerTransportError("owner endpoint metadata is missing") from error
        except OSError as error:
            raise OwnerTransportError(f"owner endpoint metadata cannot be read: {error}") from error
        except (json.JSONDecodeError, TypeError) as error:
            raise OwnerTransportError("owner endpoint metadata is invalid") from error
        if not isinstance(metadata, dict):
            raise OwnerTransportError("owner endpoint metadata is invalid")
        fingerprint = metadata.get("scope_fingerprint")
        if fingerprint != self.scope.scope_fingerprint:
            raise OwnerTransportError("owner endpoint metadata belongs to another data-root scope")
        protocol_version = metadata.get("protocol_version")
        if protocol_version != self.scope.protocol_version:
            raise OwnerTransportError("owner endpoint metadata uses an incompatible protocol")
        metadata_family = metadata.get("family")
        metadata_address = metadata.get("address")
        if metadata_family == "AF_INET":
            if not isinstance(metadata_address, (list, tuple)) or len(metadata_address) != 2:
                raise OwnerTransportError("owner endpoint address is invalid")
            try:
                host = str(metadata_address[0])
                port = int(metadata_address[1])
            except (TypeError, ValueError) as error:
                raise OwnerTransportError("owner endpoint address is invalid") from error
            if host != "127.0.0.1" or not 1 <= port <= 65535:
                raise OwnerTransportError("owner endpoint address is not loopback-only")
            return (host, port), "AF_INET"
        if metadata_family == self.family and isinstance(metadata_address, str):
            return metadata_address, self.family
        raise OwnerTransportError("owner endpoint metadata uses an incompatible IPC endpoint")

    @staticmethod
    def _recv_bytes(connection, timeout: float) -> bytes:
        if not wait_connections([connection], timeout):
            raise OwnerTransportError("owner response idle timeout")
        return connection.recv_bytes()
