"""Cross-process Scheduler-owner scope, lock, and discovery metadata.

This module owns the identity boundary for live download execution.  It is
deliberately independent from Shell commands and IPC transport details: a
future Named Pipe implementation must use the same :class:`OwnerScope` and
must not invent a second path or user identity scheme.
"""

from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, TypeAlias
from uuid import uuid4

from cli_downloader.core.errors import (
    ContractValidationError,
    OwnerAlreadyRunningError,
    OwnerDiscoveryError,
    OwnerLockError,
    OwnerScopeMismatchError,
)
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerLifecycleState,
    OwnerMode,
)

if TYPE_CHECKING:
    from cli_downloader.core.environment import AppPaths


OWNER_SCOPE_SCHEMA_VERSION = 1
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_INSTANCE_ID = uuid4().hex
_LOCAL_LOCK_GUARD = Lock()
_LOCAL_HELD_LOCKS: set[str] = set()

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def _error(code: str, path: str, message: str) -> ContractValidationError:
    return ContractValidationError(code=code, path=path, message=message)


def _require_text(value: object, path: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("invalid_text", path, "must be a non-empty string")
    if len(value) > maximum:
        raise _error("text_too_long", path, f"must contain at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _error("invalid_text", path, "must not contain control characters")
    return value


def _require_token(value: object, path: str, *, maximum: int = 256) -> str:
    result = _require_text(value, path, maximum=maximum)
    if any(character.isspace() for character in result):
        raise _error("invalid_token", path, "must not contain whitespace")
    return result


def _require_identifier(value: object, path: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise _error(
            "invalid_identifier",
            path,
            "must use lowercase letters, numbers, dots, underscores, or hyphens",
        )
    return value


def _require_positive_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _error("invalid_integer", path, "must be a positive integer")
    return value


def _require_protocol_version(value: object, path: str) -> int:
    version = _require_positive_int(value, path)
    if version != OWNER_PROTOCOL_VERSION:
        raise _error(
            "unsupported_protocol",
            path,
            f"supported protocol version is {OWNER_PROTOCOL_VERSION}",
        )
    return version


def _require_fingerprint(value: object, path: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_PATTERN.fullmatch(value) is None:
        raise _error("invalid_fingerprint", path, "must be a lowercase SHA-256 fingerprint")
    return value


def _enum(value: object, enum_type: type[StrEnum], path: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        choices = ", ".join(item.value for item in enum_type)
        raise _error("invalid_enum", path, f"must be one of: {choices}") from error


def _utc_datetime(value: object, path: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _error("invalid_datetime", path, "must be timezone-aware")
    return value.astimezone(UTC)


def _parse_datetime(value: object, path: str) -> datetime:
    if not isinstance(value, str):
        raise _error("invalid_datetime", path, "must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _error("invalid_datetime", path, "must be an ISO-8601 timestamp") from error
    return _utc_datetime(parsed, path)


def _identifiers(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("invalid_sequence", path, "must be a sequence of identifiers")
    result = tuple(
        _require_identifier(item, f"{path}[{index}]") for index, item in enumerate(value)
    )
    if len(result) != len(set(result)):
        raise _error("duplicate_value", path, "must not contain duplicate identifiers")
    return result


def canonicalize_data_root(data_root: str | os.PathLike[str]) -> Path:
    """Return the stable absolute path used as the ownership boundary.

    ``realpath``/``resolve`` collapse existing symlinks and Windows junctions;
    ``normcase`` and ``normpath`` then make case, separators, and trailing
    separators equivalent.  The function never creates the path.
    """

    if not isinstance(data_root, (str, os.PathLike)):
        raise _error("invalid_path", "scope.data_root", "must be a path")
    candidate = Path(data_root).expanduser()
    try:
        absolute = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
        resolved = absolute.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
    normalized = os.path.normpath(os.path.abspath(os.fspath(resolved)))
    if os.name == "nt":
        normalized = os.path.normcase(normalized).replace("/", "\\")
    return Path(normalized)


def _windows_current_user_sid() -> str | None:
    """Resolve the current process token SID without exposing account data."""

    if os.name != "nt":
        return None

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("User", _SidAndAttributes)]

    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        advapi.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi.GetTokenInformation.restype = wintypes.BOOL
        advapi.ConvertSidToStringSidW.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel.LocalFree.restype = wintypes.HLOCAL

        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return None
        required = wintypes.DWORD()
        advapi.GetTokenInformation(token, token_user, None, 0, ctypes.byref(required))
        if required.value <= 0:
            return None
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi.GetTokenInformation(
            token,
            token_user,
            buffer,
            required,
            ctypes.byref(required),
        ):
            return None
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_string = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(sid_string)):
            return None
        try:
            return sid_string.value
        finally:
            kernel.LocalFree(sid_string)
    except (AttributeError, OSError, TypeError):
        return None
    finally:
        if token:
            kernel.CloseHandle(token)


def current_user_identity() -> str:
    """Return a stable current-user identity for the owner namespace."""

    sid = _windows_current_user_sid()
    if sid:
        return sid
    username = getpass.getuser().strip().casefold()
    if not username:
        username = "unknown"
    return f"user:{username}"


def current_process_start_identity() -> str:
    """Return a process-instance identity suitable for stale metadata checks."""

    return f"{os.getpid()}-{_PROCESS_INSTANCE_ID}"


def new_owner_session_id() -> str:
    return uuid4().hex


def _local_lock_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _release_local_lock(key: str | None) -> None:
    if key is None:
        return
    with _LOCAL_LOCK_GUARD:
        _LOCAL_HELD_LOCKS.discard(key)


def local_owner_lock_held(path: Path) -> bool:
    """Return whether this process already owns the scoped lock path."""

    key = _local_lock_key(path)
    with _LOCAL_LOCK_GUARD:
        return key in _LOCAL_HELD_LOCKS


@dataclass(frozen=True)
class OwnerScope:
    """The single identity shared by lock, discovery, and future IPC endpoint."""

    product_namespace: str
    user_identity: str
    protocol_version: int
    canonical_data_root: Path
    scope_fingerprint: str
    endpoint_identity: str
    lock_path: Path
    discovery_path: Path

    @classmethod
    def from_data_root(
        cls,
        data_root: str | os.PathLike[str],
        *,
        product_namespace: str = "cli-downloader",
        protocol_version: int = OWNER_PROTOCOL_VERSION,
        user_identity: str | None = None,
    ) -> "OwnerScope":
        namespace = _require_identifier(product_namespace, "scope.product_namespace")
        version = _require_protocol_version(protocol_version, "scope.protocol_version")
        canonical_root = canonicalize_data_root(data_root)
        identity = _require_token(
            user_identity if user_identity is not None else current_user_identity(),
            "scope.user_identity",
            maximum=256,
        )
        scope_input = "\0".join(
            (namespace, identity, os.fspath(canonical_root), str(version))
        ).encode("utf-8")
        fingerprint = hashlib.sha256(scope_input).hexdigest()
        state_root = canonical_root / "state"
        endpoint_identity = f"{namespace}-{fingerprint[:32]}-v{version}"
        return cls(
            product_namespace=namespace,
            user_identity=identity,
            protocol_version=version,
            canonical_data_root=canonical_root,
            scope_fingerprint=fingerprint,
            endpoint_identity=endpoint_identity,
            lock_path=state_root / f"owner-{fingerprint}.lock",
            discovery_path=state_root / f"owner-{fingerprint}.json",
        )

    @classmethod
    def from_app_paths(
        cls,
        paths: "AppPaths",
        **kwargs: object,
    ) -> "OwnerScope":
        return cls.from_data_root(paths.data_root, **kwargs)

    @property
    def data_root(self) -> Path:
        return self.canonical_data_root

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "product_namespace": self.product_namespace,
            "user_identity": self.user_identity,
            "protocol_version": self.protocol_version,
            "canonical_data_root": os.fspath(self.canonical_data_root),
            "scope_fingerprint": self.scope_fingerprint,
            "endpoint_identity": self.endpoint_identity,
            "lock_path": os.fspath(self.lock_path),
            "discovery_path": os.fspath(self.discovery_path),
        }

    def lock(self) -> "OwnerLock":
        return OwnerLock(self)

    def discovery(self) -> "OwnerDiscoveryStore":
        return OwnerDiscoveryStore(self)


@dataclass(frozen=True)
class OwnerDiscoveryMetadata:
    """Non-secret owner advertisement written only while a lease is held."""

    scope_fingerprint: str
    protocol_version: int
    application_version: str
    owner_mode: OwnerMode
    lifecycle_state: OwnerLifecycleState
    pid: int
    process_start_identity: str
    session_id: str
    endpoint_identity: str
    capabilities: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = OWNER_SCOPE_SCHEMA_VERSION

    @classmethod
    def for_current_process(
        cls,
        scope: OwnerScope,
        *,
        application_version: str,
        owner_mode: OwnerMode,
        lifecycle_state: OwnerLifecycleState,
        session_id: str,
        capabilities: Sequence[str] = (),
        now: datetime | None = None,
    ) -> "OwnerDiscoveryMetadata":
        timestamp = _utc_datetime(now or datetime.now(UTC), "discovery.now")
        return cls(
            scope_fingerprint=scope.scope_fingerprint,
            protocol_version=scope.protocol_version,
            application_version=application_version,
            owner_mode=owner_mode,
            lifecycle_state=lifecycle_state,
            pid=os.getpid(),
            process_start_identity=current_process_start_identity(),
            session_id=session_id,
            endpoint_identity=scope.endpoint_identity,
            capabilities=tuple(capabilities),
            created_at=timestamp,
            updated_at=timestamp,
        )

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise _error("invalid_integer", "discovery.schema_version", "must be an integer")
        if self.schema_version != OWNER_SCOPE_SCHEMA_VERSION:
            raise _error(
                "unsupported_schema",
                "discovery.schema_version",
                f"supported schema version is {OWNER_SCOPE_SCHEMA_VERSION}",
            )
        object.__setattr__(
            self,
            "scope_fingerprint",
            _require_fingerprint(self.scope_fingerprint, "discovery.scope_fingerprint"),
        )
        object.__setattr__(
            self,
            "protocol_version",
            _require_protocol_version(self.protocol_version, "discovery.protocol_version"),
        )
        object.__setattr__(
            self,
            "application_version",
            _require_text(self.application_version, "discovery.application_version", maximum=64),
        )
        object.__setattr__(
            self, "owner_mode", _enum(self.owner_mode, OwnerMode, "discovery.owner_mode")
        )
        object.__setattr__(
            self,
            "lifecycle_state",
            _enum(self.lifecycle_state, OwnerLifecycleState, "discovery.lifecycle_state"),
        )
        object.__setattr__(self, "pid", _require_positive_int(self.pid, "discovery.pid"))
        object.__setattr__(
            self,
            "process_start_identity",
            _require_token(self.process_start_identity, "discovery.process_start_identity"),
        )
        object.__setattr__(
            self, "session_id", _require_token(self.session_id, "discovery.session_id")
        )
        object.__setattr__(
            self,
            "endpoint_identity",
            _require_token(self.endpoint_identity, "discovery.endpoint_identity", maximum=256),
        )
        object.__setattr__(
            self, "capabilities", _identifiers(self.capabilities, "discovery.capabilities")
        )
        object.__setattr__(
            self, "created_at", _utc_datetime(self.created_at, "discovery.created_at")
        )
        object.__setattr__(
            self, "updated_at", _utc_datetime(self.updated_at, "discovery.updated_at")
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "scope_fingerprint": self.scope_fingerprint,
            "protocol_version": self.protocol_version,
            "application_version": self.application_version,
            "owner_mode": self.owner_mode.value,
            "lifecycle_state": self.lifecycle_state.value,
            "pid": self.pid,
            "process_start_identity": self.process_start_identity,
            "session_id": self.session_id,
            "endpoint_identity": self.endpoint_identity,
            "capabilities": list(self.capabilities),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "OwnerDiscoveryMetadata":
        if not isinstance(value, Mapping):
            raise _error("invalid_metadata", "discovery", "must be a JSON object")
        allowed = frozenset(
            {
                "schema_version",
                "scope_fingerprint",
                "protocol_version",
                "application_version",
                "owner_mode",
                "lifecycle_state",
                "pid",
                "process_start_identity",
                "session_id",
                "endpoint_identity",
                "capabilities",
                "created_at",
                "updated_at",
            }
        )
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise _error(
                "unknown_field", f"discovery.{unknown[0]}", "is not part of owner metadata"
            )
        required = (
            "scope_fingerprint",
            "protocol_version",
            "application_version",
            "owner_mode",
            "lifecycle_state",
            "pid",
            "process_start_identity",
            "session_id",
            "endpoint_identity",
            "created_at",
            "updated_at",
        )
        for key in required:
            if key not in value:
                raise _error("missing_field", f"discovery.{key}", "is required")
        return cls(
            scope_fingerprint=value["scope_fingerprint"],
            protocol_version=value["protocol_version"],
            application_version=value["application_version"],
            owner_mode=_enum(value["owner_mode"], OwnerMode, "discovery.owner_mode"),
            lifecycle_state=_enum(
                value["lifecycle_state"], OwnerLifecycleState, "discovery.lifecycle_state"
            ),
            pid=value["pid"],
            process_start_identity=value["process_start_identity"],
            session_id=value["session_id"],
            endpoint_identity=value["endpoint_identity"],
            capabilities=value.get("capabilities", ()),
            created_at=_parse_datetime(value["created_at"], "discovery.created_at"),
            updated_at=_parse_datetime(value["updated_at"], "discovery.updated_at"),
            schema_version=value.get("schema_version", OWNER_SCOPE_SCHEMA_VERSION),
        )


class OwnerDiscoveryStore:
    """Atomic JSON store whose contents are advisory, never proof of liveness."""

    def __init__(self, scope: OwnerScope) -> None:
        self.scope = scope
        self.path = scope.discovery_path

    def read(self) -> OwnerDiscoveryMetadata | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return OwnerDiscoveryMetadata.from_dict(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ContractValidationError) as error:
            raise OwnerDiscoveryError(f"cannot read owner metadata: {error}") from error

    def read_for_scope(self) -> OwnerDiscoveryMetadata | None:
        """Read metadata and reject records from another owner scope."""

        metadata = self.read()
        if metadata is not None:
            self._validate_scope(metadata)
        return metadata

    def validate(self, metadata: OwnerDiscoveryMetadata) -> None:
        """Validate that metadata belongs to this store's scope."""

        self._validate_scope(metadata)

    def write(self, metadata: OwnerDiscoveryMetadata) -> None:
        self._validate_scope(metadata)
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
                    metadata.to_dict(),
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
        except (OSError, TypeError, ValueError) as error:
            raise OwnerDiscoveryError(f"cannot write owner metadata: {error}") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self, *, expected_session_id: str | None = None) -> bool:
        if not self.path.exists():
            return False
        if expected_session_id is not None:
            current = self.read()
            if current is None or current.session_id != expected_session_id:
                return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise OwnerDiscoveryError(f"cannot remove owner metadata: {error}") from error
        return True

    def _validate_scope(self, metadata: OwnerDiscoveryMetadata) -> None:
        if metadata.scope_fingerprint != self.scope.scope_fingerprint:
            raise OwnerScopeMismatchError("owner metadata belongs to a different data-root scope")
        if metadata.protocol_version != self.scope.protocol_version:
            raise OwnerScopeMismatchError("owner metadata uses a different protocol version")
        if metadata.endpoint_identity != self.scope.endpoint_identity:
            raise OwnerScopeMismatchError("owner metadata uses a different endpoint identity")


class OwnerLease(AbstractContextManager["OwnerLease"]):
    """A held OS lock; releasing it also removes metadata published by this lease."""

    def __init__(
        self,
        scope: OwnerScope,
        stream: Any,
        *,
        local_lock_key: str | None = None,
    ) -> None:
        self.scope = scope
        self._stream = stream
        self._local_lock_key = local_lock_key
        self._released = False
        self._published_session_id: str | None = None
        self._discovery = OwnerDiscoveryStore(scope)

    @property
    def active(self) -> bool:
        return not self._released

    def publish(self, metadata: OwnerDiscoveryMetadata) -> None:
        if self._released:
            raise OwnerLockError("cannot publish metadata after owner lease release")
        self._discovery.write(metadata)
        self._published_session_id = metadata.session_id

    def clear_discovery(self) -> bool:
        if self._released:
            return False
        if self._published_session_id is None:
            return False
        removed = self._discovery.clear(expected_session_id=self._published_session_id)
        self._published_session_id = None
        return removed

    def release(self) -> None:
        if self._released:
            return
        cleanup_error: Exception | None = None
        try:
            if self._published_session_id is not None:
                try:
                    self._discovery.clear(expected_session_id=self._published_session_id)
                except Exception as error:  # noqa: BLE001 - lock must always be released
                    cleanup_error = error
        finally:
            try:
                _unlock_stream(self._stream)
            finally:
                try:
                    self._stream.close()
                finally:
                    _release_local_lock(self._local_lock_key)
                    self._released = True
        if cleanup_error is not None:
            raise cleanup_error

    def __enter__(self) -> "OwnerLease":
        if self._released:
            raise OwnerLockError("owner lease has already been released")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:
            pass


class OwnerLock:
    """OS-released, non-blocking lock for one :class:`OwnerScope`."""

    def __init__(self, scope: OwnerScope) -> None:
        self.scope = scope

    def acquire(self) -> OwnerLease:
        lock_key = _local_lock_key(self.scope.lock_path)
        with _LOCAL_LOCK_GUARD:
            if lock_key in _LOCAL_HELD_LOCKS:
                raise OwnerAlreadyRunningError(
                    f"another process owns scope {self.scope.scope_fingerprint}"
                )
            _LOCAL_HELD_LOCKS.add(lock_key)
        stream: Any | None = None
        try:
            self.scope.lock_path.parent.mkdir(parents=True, exist_ok=True)
            stream = _open_lock_stream(self.scope.lock_path)
            _lock_stream(stream)
        except OwnerAlreadyRunningError:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            _release_local_lock(lock_key)
            raise
        except (BlockingIOError, PermissionError) as error:
            try:
                stream.close()  # type: ignore[union-attr]
            except (NameError, AttributeError, OSError):
                pass
            _release_local_lock(lock_key)
            raise OwnerAlreadyRunningError(
                f"another process owns scope {self.scope.scope_fingerprint}"
            ) from error
        except OSError as error:
            try:
                stream.close()  # type: ignore[union-attr]
            except (NameError, AttributeError, OSError):
                pass
            _release_local_lock(lock_key)
            raise OwnerLockError(f"cannot acquire owner lock: {error}") from error
        except BaseException:
            try:
                stream.close()  # type: ignore[union-attr]
            except (NameError, AttributeError, OSError):
                pass
            _release_local_lock(lock_key)
            raise
        return OwnerLease(self.scope, stream, local_lock_key=lock_key)


class OwnerManager:
    """Acquire a scope and publish its non-secret discovery record atomically."""

    def __init__(self, scope: OwnerScope) -> None:
        self.scope = scope

    def acquire(self, metadata: OwnerDiscoveryMetadata) -> OwnerLease:
        lease = self.scope.lock().acquire()
        try:
            lease.publish(metadata)
        except Exception:
            lease.release()
            raise
        return lease


def _open_lock_stream(path: Path) -> Any:
    try:
        return path.open("r+b", buffering=0)
    except FileNotFoundError:
        flags = os.O_CREAT | os.O_RDWR
        mode = 0o600 if os.name != "nt" else 0o666
        try:
            descriptor = os.open(path, flags, mode)
        except OSError as error:
            raise OwnerLockError(f"cannot create owner lock file: {error}") from error
        return os.fdopen(descriptor, "r+b", buffering=0)
    except PermissionError as error:
        if path.exists():
            raise OwnerAlreadyRunningError("owner lock file is already held") from error
        raise OwnerLockError(f"cannot open owner lock file: {error}") from error


def _lock_stream(stream: Any) -> None:
    stream.seek(0)
    stream.write(b"\0")
    stream.flush()
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(stream: Any) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
