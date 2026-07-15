"""Platform-neutral Scheduler-owner and local IPC contracts.

The models in this module deliberately stop at the protocol boundary.  They do
not open a socket, acquire an operating-system lock, or dispatch a download.
That keeps the contract testable in-process and lets the later Windows
transport use the same validation rules as a foreground or Shell owner.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias
from urllib.parse import urlsplit

from cli_downloader.core.errors import ContractValidationError


OWNER_PROTOCOL_VERSION = 1
DEFAULT_MAX_FRAME_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 16
_MAX_MAP_ITEMS = 256
_MAX_SEQUENCE_ITEMS = 1024
_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]


class OwnerMode(StrEnum):
    SHELL = "shell"
    FOREGROUND = "foreground"


class OwnerLifecycleState(StrEnum):
    STARTING = "starting"
    RECOVERING = "recovering"
    READY = "ready"
    STOPPING = "stopping"
    BUSY = "busy"
    INCOMPATIBLE = "incompatible"


class OwnerSource(StrEnum):
    CLI = "cli"
    SHELL = "shell"
    AGENT = "agent"
    INTERNAL = "internal"


class OwnerResponseMode(StrEnum):
    WAIT = "wait"
    DETACH = "detach"


class OwnerCommand(StrEnum):
    DOWNLOAD = "download"
    TASK_CONTROL = "task_control"
    TASK_QUERY = "task_query"
    HISTORY_QUERY = "history_query"
    SHELL_STATUS = "shell_status"
    PING = "ping"


class OwnerEventKind(StrEnum):
    ACCEPTED = "accepted"
    PROGRESS = "progress"
    CONTROL = "control"
    HEARTBEAT = "heartbeat"
    TERMINAL = "terminal"
    OWNER_DISCONNECTED = "owner_disconnected"


class OwnerTerminalStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OwnerDisconnectOutcome(StrEnum):
    NOT_ACCEPTED = "not_accepted"
    ACCEPTED_COMPLETION_UNKNOWN = "accepted_completion_unknown"
    TERMINAL_RESULT_LOST = "terminal_result_lost"


class OwnerWaitPhase(StrEnum):
    CONNECT = "connect"
    HANDSHAKE = "handshake"
    ACCEPTANCE = "acceptance"
    RESPONSE_IDLE = "response_idle"


class RequestDisposition(StrEnum):
    NEW = "new"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class RequestState(StrEnum):
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    TERMINAL = "terminal"


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


def _optional_text(value: object, path: str, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return _require_text(value, path, maximum=maximum)


def _require_token(value: object, path: str, *, maximum: int = 128) -> str:
    token = _require_text(value, path, maximum=maximum)
    if any(character.isspace() for character in token):
        raise _error("invalid_token", path, "must not contain whitespace")
    return token


def _require_identifier(value: object, path: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise _error("invalid_identifier", path, "must use lowercase letters, numbers, dots, underscores, or hyphens")
    return value


def _optional_fingerprint(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.fullmatch(value):
        raise _error("invalid_fingerprint", path, "must be a lowercase SHA-256 fingerprint")
    return value


def _require_url(value: object, path: str) -> str:
    url = _require_text(value, path, maximum=8192)
    if any(character.isspace() for character in url):
        raise _error("invalid_url", path, "must not contain whitespace")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise _error("invalid_url", path, "must be a valid absolute URL") from error
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise _error("invalid_url", path, "must be an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise _error("url_credentials_forbidden", path, "must not contain embedded credentials")
    return url


def _require_non_negative_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error("invalid_integer", path, "must be a non-negative integer")
    return value


def _require_positive_int(value: object, path: str) -> int:
    result = _require_non_negative_int(value, path)
    if result == 0:
        raise _error("invalid_integer", path, "must be greater than zero")
    return result


def _protocol_version(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error("invalid_integer", path, "must be an integer protocol version")
    if value != OWNER_PROTOCOL_VERSION:
        raise _error("unsupported_protocol", path, f"supported version is {OWNER_PROTOCOL_VERSION}")
    return value


def _require_finite_non_negative_float(value: object, path: str, *, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error("invalid_number", path, "must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise _error("invalid_number", path, "must be a finite non-negative number")
    if maximum is not None and result > maximum:
        raise _error("invalid_number", path, f"must not exceed {maximum}")
    return result


def _freeze_json(value: object, path: str, *, depth: int = 0, active: set[int] | None = None) -> FrozenJsonValue:
    if depth > _MAX_JSON_DEPTH:
        raise _error("json_depth_exceeded", path, f"must not exceed {_MAX_JSON_DEPTH} levels")
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            if len(value) > 16384:
                raise _error("text_too_long", path, "must contain at most 16384 characters")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise _error("invalid_text", path, "must not contain control characters")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _error("invalid_json_number", path, "must be a finite number")
        return value
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        if len(value) > _MAX_MAP_ITEMS:
            raise _error("json_object_too_large", path, f"must contain at most {_MAX_MAP_ITEMS} keys")
        object_id = id(value)
        if object_id in active:
            raise _error("json_cycle", path, "must not contain reference cycles")
        active.add(object_id)
        frozen: dict[str, FrozenJsonValue] = {}
        try:
            for key, item in value.items():
                key = _require_text(key, f"{path}.key", maximum=256)
                frozen[key] = _freeze_json(item, f"{path}.{key}", depth=depth + 1, active=active)
        finally:
            active.remove(object_id)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > _MAX_SEQUENCE_ITEMS:
            raise _error("json_array_too_large", path, f"must contain at most {_MAX_SEQUENCE_ITEMS} items")
        object_id = id(value)
        if object_id in active:
            raise _error("json_cycle", path, "must not contain reference cycles")
        active.add(object_id)
        try:
            return tuple(_freeze_json(item, f"{path}[{index}]", depth=depth + 1, active=active) for index, item in enumerate(value))
        finally:
            active.remove(object_id)
    raise _error("non_json_value", path, "must contain only JSON-compatible values")


def _freeze_json_object(value: object, path: str) -> Mapping[str, FrozenJsonValue]:
    frozen = _freeze_json(value, path)
    if not isinstance(frozen, Mapping):
        raise _error("invalid_json_object", path, "must be an object")
    return frozen


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_identifiers(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("invalid_sequence", path, "must be a sequence of identifiers")
    result = tuple(_require_identifier(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(result) != len(set(result)):
        raise _error("duplicate_value", path, "must not contain duplicate values")
    return result


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


def _check_keys(value: Mapping[str, object], path: str, allowed: frozenset[str]) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _error("unknown_field", f"{path}.{unknown[0]}", "is not part of this protocol contract")


_COMMAND_FIELDS: dict[OwnerCommand, frozenset[str]] = {
    OwnerCommand.DOWNLOAD: frozenset({"url", "params", "settings", "setting_refs", "secret_refs", "strategy", "select", "selection_policy", "confirmations", "profile", "force"}),
    OwnerCommand.TASK_CONTROL: frozenset({"task_id", "action", "expected_revision"}),
    OwnerCommand.TASK_QUERY: frozenset({"task_id", "include_children"}),
    OwnerCommand.HISTORY_QUERY: frozenset({"task_id", "child"}),
    OwnerCommand.SHELL_STATUS: frozenset(),
    OwnerCommand.PING: frozenset(),
}
_FORBIDDEN_PAYLOAD_FIELDS = frozenset({"command_line", "shell_command", "python", "callable", "executable", "cookies", "token", "password", "secret"})


def _validate_command_payload(command: OwnerCommand, payload: Mapping[str, FrozenJsonValue]) -> None:
    forbidden = sorted(set(payload) & _FORBIDDEN_PAYLOAD_FIELDS)
    if forbidden:
        raise _error("forbidden_field", f"request.payload.{forbidden[0]}", "raw process commands and secret values are not allowed")
    _check_keys(payload, "request.payload", _COMMAND_FIELDS[command])
    if command is OwnerCommand.DOWNLOAD:
        if "url" not in payload:
            raise _error("missing_field", "request.payload.url", "is required for download")
        _require_url(payload["url"], "request.payload.url")
        for name in ("params", "settings"):
            if name in payload:
                _freeze_json_object(payload[name], f"request.payload.{name}")
        for name in ("setting_refs", "secret_refs"):
            if name in payload:
                refs = _freeze_json_object(payload[name], f"request.payload.{name}")
                for key, value in refs.items():
                    _require_identifier(key, f"request.payload.{name}.{key}")
                    _require_token(value, f"request.payload.{name}.{key}")
        if "strategy" in payload:
            _require_identifier(payload["strategy"], "request.payload.strategy")
        if "profile" in payload:
            _require_token(payload["profile"], "request.payload.profile", maximum=128)
        if "selection_policy" in payload:
            policy = _require_identifier(
                payload["selection_policy"],
                "request.payload.selection_policy",
            )
            if policy not in {"all", "explicit", "interactive"}:
                raise _error(
                    "invalid_value",
                    "request.payload.selection_policy",
                    "must be all, explicit, or interactive",
                )
        if "force" in payload and not isinstance(payload["force"], bool):
            raise _error("invalid_boolean", "request.payload.force", "must be a boolean")
        if "select" in payload:
            selections = payload["select"]
            if not isinstance(selections, tuple):
                raise _error("invalid_sequence", "request.payload.select", "must be a sequence")
            for index, item in enumerate(selections):
                _require_token(item, f"request.payload.select[{index}]")
    elif command is OwnerCommand.TASK_CONTROL:
        if "task_id" not in payload:
            raise _error("missing_field", "request.payload.task_id", "is required for task control")
        if "action" not in payload:
            raise _error("missing_field", "request.payload.action", "is required for task control")
        _require_token(payload["task_id"], "request.payload.task_id")
        _require_identifier(payload["action"], "request.payload.action")
        if "expected_revision" in payload:
            _require_positive_int(payload["expected_revision"], "request.payload.expected_revision")
    elif command is OwnerCommand.TASK_QUERY:
        if "task_id" in payload:
            _require_token(payload["task_id"], "request.payload.task_id")
        if "include_children" in payload and not isinstance(payload["include_children"], bool):
            raise _error("invalid_boolean", "request.payload.include_children", "must be a boolean")
    elif command is OwnerCommand.HISTORY_QUERY and "task_id" in payload:
        _require_token(payload["task_id"], "request.payload.task_id")


@dataclass(frozen=True)
class OwnerHandshake:
    """Version and capability declaration exchanged before commands."""

    protocol_version: int
    session_id: str
    mode: OwnerMode
    application_version: str
    capabilities: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    scope_fingerprint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_version", _protocol_version(self.protocol_version, "handshake.protocol_version"))
        object.__setattr__(self, "session_id", _require_token(self.session_id, "handshake.session_id"))
        object.__setattr__(self, "mode", _enum(self.mode, OwnerMode, "handshake.mode"))
        object.__setattr__(self, "application_version", _require_text(self.application_version, "handshake.application_version", maximum=64))
        object.__setattr__(self, "capabilities", _freeze_identifiers(self.capabilities, "handshake.capabilities"))
        object.__setattr__(self, "required_capabilities", _freeze_identifiers(self.required_capabilities, "handshake.required_capabilities"))
        object.__setattr__(self, "scope_fingerprint", _optional_fingerprint(self.scope_fingerprint, "handshake.scope_fingerprint"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "mode": self.mode.value,
            "application_version": self.application_version,
            "capabilities": list(self.capabilities),
            "required_capabilities": list(self.required_capabilities),
            "scope_fingerprint": self.scope_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: object) -> "OwnerHandshake":
        if not isinstance(value, Mapping):
            raise _error("invalid_envelope", "handshake", "must be an object")
        _check_keys(
            value,
            "handshake",
            frozenset(
                {
                    "protocol_version",
                    "session_id",
                    "mode",
                    "application_version",
                    "capabilities",
                    "required_capabilities",
                    "scope_fingerprint",
                }
            ),
        )
        required = ("protocol_version", "session_id", "mode", "application_version")
        for key in required:
            if key not in value:
                raise _error("missing_field", f"handshake.{key}", "is required")
        return cls(
            protocol_version=value["protocol_version"],
            session_id=value["session_id"],
            mode=_enum(value["mode"], OwnerMode, "handshake.mode"),
            application_version=value["application_version"],
            capabilities=value.get("capabilities", ()),
            required_capabilities=value.get("required_capabilities", ()),
            scope_fingerprint=value.get("scope_fingerprint"),
        )


@dataclass(frozen=True)
class OwnerHandshakeResult:
    protocol_version: int
    owner_session_id: str
    owner_mode: OwnerMode
    negotiated_capabilities: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_version", _protocol_version(self.protocol_version, "handshake_result.protocol_version"))
        object.__setattr__(self, "owner_session_id", _require_token(self.owner_session_id, "handshake_result.owner_session_id"))
        object.__setattr__(self, "owner_mode", _enum(self.owner_mode, OwnerMode, "handshake_result.owner_mode"))
        object.__setattr__(self, "negotiated_capabilities", _freeze_identifiers(self.negotiated_capabilities, "handshake_result.negotiated_capabilities"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "owner_session_id": self.owner_session_id,
            "owner_mode": self.owner_mode.value,
            "negotiated_capabilities": list(self.negotiated_capabilities),
        }


def negotiate_handshake(client: OwnerHandshake, owner: OwnerHandshake) -> OwnerHandshakeResult:
    if client.protocol_version != owner.protocol_version:
        raise _error("protocol_mismatch", "handshake.protocol_version", "client and owner protocol versions differ")
    if (client.scope_fingerprint is None) != (owner.scope_fingerprint is None):
        raise _error("scope_mismatch", "handshake.scope_fingerprint", "client and owner must identify the same data-root scope")
    if client.scope_fingerprint is not None and client.scope_fingerprint != owner.scope_fingerprint:
        raise _error("scope_mismatch", "handshake.scope_fingerprint", "client and owner data roots differ")
    client_capabilities = set(client.capabilities)
    owner_capabilities = set(owner.capabilities)
    missing = sorted((set(client.required_capabilities) - owner_capabilities) | (set(owner.required_capabilities) - client_capabilities))
    if missing:
        raise _error("unsupported_capability", "handshake.capabilities", f"required capabilities are unavailable: {', '.join(missing)}")
    return OwnerHandshakeResult(
        protocol_version=owner.protocol_version,
        owner_session_id=owner.session_id,
        owner_mode=owner.mode,
        negotiated_capabilities=tuple(sorted(client_capabilities & owner_capabilities)),
    )


@dataclass(frozen=True)
class OwnerRequest:
    protocol_version: int
    request_id: str
    source: OwnerSource
    command: OwnerCommand
    response_mode: OwnerResponseMode
    payload: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    client_session_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_version", _protocol_version(self.protocol_version, "request.protocol_version"))
        object.__setattr__(self, "request_id", _require_token(self.request_id, "request.request_id"))
        object.__setattr__(self, "source", _enum(self.source, OwnerSource, "request.source"))
        object.__setattr__(self, "command", _enum(self.command, OwnerCommand, "request.command"))
        object.__setattr__(self, "response_mode", _enum(self.response_mode, OwnerResponseMode, "request.response_mode"))
        object.__setattr__(self, "payload", _freeze_json_object(self.payload, "request.payload"))
        _validate_command_payload(self.command, self.payload)
        object.__setattr__(self, "client_session_id", _optional_text(self.client_session_id, "request.client_session_id", maximum=128))
        object.__setattr__(self, "created_at", _utc_datetime(self.created_at, "request.created_at"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "source": self.source.value,
            "command": self.command.value,
            "response_mode": self.response_mode.value,
            "payload": _thaw_json(self.payload),
            "client_session_id": self.client_session_id,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "OwnerRequest":
        if not isinstance(value, Mapping):
            raise _error("invalid_envelope", "request", "must be an object")
        _check_keys(value, "request", frozenset({"protocol_version", "request_id", "source", "command", "response_mode", "payload", "client_session_id", "created_at"}))
        required = ("protocol_version", "request_id", "source", "command", "response_mode", "payload", "created_at")
        for key in required:
            if key not in value:
                raise _error("missing_field", f"request.{key}", "is required")
        return cls(
            protocol_version=value["protocol_version"],
            request_id=value["request_id"],
            source=_enum(value["source"], OwnerSource, "request.source"),
            command=_enum(value["command"], OwnerCommand, "request.command"),
            response_mode=_enum(value["response_mode"], OwnerResponseMode, "request.response_mode"),
            payload=value["payload"],
            client_session_id=value.get("client_session_id"),
            created_at=_parse_datetime(value["created_at"], "request.created_at"),
        )

    def to_bytes(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> bytes:
        if not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise _error("invalid_integer", "request.max_frame_bytes", "must be greater than zero")
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > max_frame_bytes:
            raise _error("frame_too_large", "request", f"must not exceed {max_frame_bytes} bytes")
        return encoded

    @classmethod
    def from_bytes(cls, value: bytes, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> "OwnerRequest":
        if not isinstance(value, bytes):
            raise _error("invalid_frame", "request", "must be encoded as bytes")
        if len(value) > max_frame_bytes:
            raise _error("frame_too_large", "request", f"must not exceed {max_frame_bytes} bytes")
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _error("invalid_frame", "request", "must contain a UTF-8 JSON object") from error
        return cls.from_dict(decoded)


def _event_payload(kind: OwnerEventKind, payload: Mapping[str, FrozenJsonValue]) -> None:
    if kind is OwnerEventKind.ACCEPTED:
        _check_keys(payload, "event.payload", frozenset({"owner_session_id", "owner_pid", "accepted_at", "task_id", "task_kind", "queued"}))
        for name in ("owner_session_id", "task_id", "task_kind"):
            if name in payload:
                _require_token(payload[name], f"event.payload.{name}")
        if "accepted_at" not in payload:
            raise _error("missing_field", "event.payload.accepted_at", "is required")
        _parse_datetime(payload["accepted_at"], "event.payload.accepted_at")
        if "queued" in payload and not isinstance(payload["queued"], bool):
            raise _error("invalid_boolean", "event.payload.queued", "must be a boolean")
        if "owner_pid" in payload:
            _require_positive_int(payload["owner_pid"], "event.payload.owner_pid")
        if "owner_session_id" not in payload:
            raise _error("missing_field", "event.payload.owner_session_id", "is required")
    elif kind is OwnerEventKind.PROGRESS:
        _check_keys(payload, "event.payload", frozenset({"task_id", "status", "progress", "downloaded_bytes", "total_bytes", "file_count", "message"}))
        for name in ("task_id", "status"):
            if name not in payload:
                raise _error("missing_field", f"event.payload.{name}", "is required")
            _require_token(payload[name], f"event.payload.{name}")
        if "progress" in payload:
            _require_finite_non_negative_float(payload["progress"], "event.payload.progress", maximum=1.0)
        for name in ("downloaded_bytes", "total_bytes", "file_count"):
            if name in payload:
                _require_non_negative_int(payload[name], f"event.payload.{name}")
        if "message" in payload:
            _require_text(payload["message"], "event.payload.message")
    elif kind is OwnerEventKind.CONTROL:
        _check_keys(payload, "event.payload", frozenset({"task_id", "action", "acknowledged", "revision", "desired_control"}))
        for name in ("task_id", "action"):
            if name not in payload:
                raise _error("missing_field", f"event.payload.{name}", "is required")
            _require_token(payload[name], f"event.payload.{name}")
        if "acknowledged" not in payload or not isinstance(payload["acknowledged"], bool):
            raise _error("invalid_boolean", "event.payload.acknowledged", "must be a boolean")
        if "revision" in payload:
            _require_positive_int(payload["revision"], "event.payload.revision")
        if "desired_control" in payload:
            _require_identifier(payload["desired_control"], "event.payload.desired_control")
    elif kind is OwnerEventKind.HEARTBEAT:
        _check_keys(payload, "event.payload", frozenset({"owner_session_id", "emitted_at"}))
        if "owner_session_id" not in payload or "emitted_at" not in payload:
            raise _error("missing_field", "event.payload", "owner_session_id and emitted_at are required")
        _require_token(payload["owner_session_id"], "event.payload.owner_session_id")
        _parse_datetime(payload["emitted_at"], "event.payload.emitted_at")
    elif kind is OwnerEventKind.TERMINAL:
        _check_keys(payload, "event.payload", frozenset({"status", "task_id", "exit_code", "result", "error"}))
        if "status" not in payload:
            raise _error("missing_field", "event.payload.status", "is required")
        _enum(payload["status"], OwnerTerminalStatus, "event.payload.status")
        if "task_id" in payload:
            _require_token(payload["task_id"], "event.payload.task_id")
        if "exit_code" in payload and (isinstance(payload["exit_code"], bool) or not isinstance(payload["exit_code"], int)):
            raise _error("invalid_integer", "event.payload.exit_code", "must be an integer")
        for name in ("result", "error"):
            if name in payload:
                _freeze_json_object(payload[name], f"event.payload.{name}")
    elif kind is OwnerEventKind.OWNER_DISCONNECTED:
        _check_keys(payload, "event.payload", frozenset({"phase", "outcome", "known_task_id", "owner_session_id", "last_sequence", "message", "terminal_persisted"}))
        if "phase" not in payload or "outcome" not in payload:
            raise _error("missing_field", "event.payload", "phase and outcome are required")
        phase = _enum(payload["phase"], OwnerWaitPhase, "event.payload.phase")
        outcome = _enum(payload["outcome"], OwnerDisconnectOutcome, "event.payload.outcome")
        if outcome is OwnerDisconnectOutcome.NOT_ACCEPTED and phase is OwnerWaitPhase.RESPONSE_IDLE:
            raise _error("invalid_disconnect_outcome", "event.payload.outcome", "not_accepted cannot follow response idle")
        if outcome is not OwnerDisconnectOutcome.NOT_ACCEPTED and phase is not OwnerWaitPhase.RESPONSE_IDLE:
            raise _error("invalid_disconnect_outcome", "event.payload.outcome", "accepted outcomes require response idle")
        for name in ("known_task_id", "owner_session_id"):
            if name in payload:
                _require_token(payload[name], f"event.payload.{name}")
        if "message" in payload:
            _require_text(payload["message"], "event.payload.message")
        if "last_sequence" in payload:
            _require_non_negative_int(payload["last_sequence"], "event.payload.last_sequence")
        if outcome in {OwnerDisconnectOutcome.ACCEPTED_COMPLETION_UNKNOWN, OwnerDisconnectOutcome.TERMINAL_RESULT_LOST} and "known_task_id" not in payload:
            raise _error("missing_task_id", "event.payload.known_task_id", "accepted disconnect outcomes require the known Task ID")
        if outcome is OwnerDisconnectOutcome.TERMINAL_RESULT_LOST and payload.get("terminal_persisted") is not True:
            raise _error("missing_terminal_evidence", "event.payload.terminal_persisted", "must be true when terminal result was persisted")


@dataclass(frozen=True)
class OwnerResponseEvent:
    """One ordered response frame; constructors keep payloads typed by event kind."""

    request_id: str
    sequence: int
    kind: OwnerEventKind
    payload: Mapping[str, FrozenJsonValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _require_token(self.request_id, "event.request_id"))
        object.__setattr__(self, "sequence", _require_positive_int(self.sequence, "event.sequence"))
        object.__setattr__(self, "kind", _enum(self.kind, OwnerEventKind, "event.kind"))
        object.__setattr__(self, "payload", _freeze_json_object(self.payload, "event.payload"))
        _event_payload(self.kind, self.payload)

    @classmethod
    def accepted(cls, request_id: str, sequence: int, *, owner_session_id: str, accepted_at: datetime, task_id: str | None = None, task_kind: str | None = None, queued: bool = True, owner_pid: int | None = None) -> "OwnerResponseEvent":
        payload: dict[str, object] = {"owner_session_id": owner_session_id, "accepted_at": _utc_datetime(accepted_at, "event.payload.accepted_at").isoformat(), "queued": queued}
        if task_id is not None:
            payload["task_id"] = task_id
        if task_kind is not None:
            payload["task_kind"] = task_kind
        if owner_pid is not None:
            payload["owner_pid"] = owner_pid
        return cls(request_id, sequence, OwnerEventKind.ACCEPTED, payload)

    @classmethod
    def progress(cls, request_id: str, sequence: int, *, task_id: str, status: str, progress: float | None = None, downloaded_bytes: int | None = None, total_bytes: int | None = None, file_count: int | None = None, message: str | None = None) -> "OwnerResponseEvent":
        payload: dict[str, object] = {"task_id": task_id, "status": status}
        for name, value in (("progress", progress), ("downloaded_bytes", downloaded_bytes), ("total_bytes", total_bytes), ("file_count", file_count), ("message", message)):
            if value is not None:
                payload[name] = value
        return cls(request_id, sequence, OwnerEventKind.PROGRESS, payload)

    @classmethod
    def control(cls, request_id: str, sequence: int, *, task_id: str, action: str, acknowledged: bool, revision: int | None = None, desired_control: str | None = None) -> "OwnerResponseEvent":
        payload: dict[str, object] = {"task_id": task_id, "action": action, "acknowledged": acknowledged}
        if revision is not None:
            payload["revision"] = revision
        if desired_control is not None:
            payload["desired_control"] = desired_control
        return cls(request_id, sequence, OwnerEventKind.CONTROL, payload)

    @classmethod
    def heartbeat(cls, request_id: str, sequence: int, *, owner_session_id: str, emitted_at: datetime) -> "OwnerResponseEvent":
        return cls(request_id, sequence, OwnerEventKind.HEARTBEAT, {"owner_session_id": owner_session_id, "emitted_at": _utc_datetime(emitted_at, "event.payload.emitted_at").isoformat()})

    @classmethod
    def terminal(cls, request_id: str, sequence: int, *, status: OwnerTerminalStatus, task_id: str | None = None, exit_code: int | None = None, result: Mapping[str, object] | None = None, error: Mapping[str, object] | None = None) -> "OwnerResponseEvent":
        payload: dict[str, object] = {"status": status.value if isinstance(status, OwnerTerminalStatus) else status}
        for name, value in (("task_id", task_id), ("exit_code", exit_code), ("result", result), ("error", error)):
            if value is not None:
                payload[name] = value
        return cls(request_id, sequence, OwnerEventKind.TERMINAL, payload)

    @classmethod
    def owner_disconnected(cls, request_id: str, sequence: int, *, phase: OwnerWaitPhase, outcome: OwnerDisconnectOutcome, known_task_id: str | None = None, owner_session_id: str | None = None, last_sequence: int = 0, message: str = "owner disconnected", terminal_persisted: bool = False) -> "OwnerResponseEvent":
        payload: dict[str, object] = {"phase": phase.value if isinstance(phase, OwnerWaitPhase) else phase, "outcome": outcome.value if isinstance(outcome, OwnerDisconnectOutcome) else outcome, "last_sequence": last_sequence, "message": message}
        for name, value in (("known_task_id", known_task_id), ("owner_session_id", owner_session_id)):
            if value is not None:
                payload[name] = value
        if terminal_persisted:
            payload["terminal_persisted"] = True
        return cls(request_id, sequence, OwnerEventKind.OWNER_DISCONNECTED, payload)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"protocol_version": OWNER_PROTOCOL_VERSION, "request_id": self.request_id, "sequence": self.sequence, "kind": self.kind.value, "payload": _thaw_json(self.payload)}

    @classmethod
    def from_dict(cls, value: object) -> "OwnerResponseEvent":
        if not isinstance(value, Mapping):
            raise _error("invalid_envelope", "event", "must be an object")
        _check_keys(value, "event", frozenset({"protocol_version", "request_id", "sequence", "kind", "payload"}))
        for key in ("protocol_version", "request_id", "sequence", "kind", "payload"):
            if key not in value:
                raise _error("missing_field", f"event.{key}", "is required")
        _protocol_version(value["protocol_version"], "event.protocol_version")
        return cls(value["request_id"], value["sequence"], _enum(value["kind"], OwnerEventKind, "event.kind"), value["payload"])

    def to_bytes(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> bytes:
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > max_frame_bytes:
            raise _error("frame_too_large", "event", f"must not exceed {max_frame_bytes} bytes")
        return encoded

    @classmethod
    def from_bytes(cls, value: bytes, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> "OwnerResponseEvent":
        if not isinstance(value, bytes) or len(value) > max_frame_bytes:
            raise _error("frame_too_large", "event", f"must not exceed {max_frame_bytes} bytes")
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _error("invalid_frame", "event", "must contain a UTF-8 JSON object") from error
        return cls.from_dict(decoded)


class OwnerResponseStream:
    """Validate ordering and terminal-event rules for one request."""

    def __init__(self, request_id: str) -> None:
        self.request_id = _require_token(request_id, "stream.request_id")
        self.last_sequence = 0
        self.accepted = False
        self.terminal = False

    def push(self, event: OwnerResponseEvent) -> None:
        if event.request_id != self.request_id:
            raise _error("request_id_mismatch", "event.request_id", "must match the response stream")
        if self.terminal:
            raise _error("event_after_terminal", "event", "no event may follow a terminal result")
        if event.sequence != self.last_sequence + 1:
            raise _error("invalid_sequence", "event.sequence", "must increase by exactly one")
        if event.kind is OwnerEventKind.ACCEPTED:
            if self.accepted:
                raise _error("duplicate_acceptance", "event.kind", "a request may be accepted only once")
            self.accepted = True
        elif event.kind is not OwnerEventKind.OWNER_DISCONNECTED and not self.accepted:
            raise _error("invalid_lifecycle", "event.kind", "accepted must be the first response event")
        self.last_sequence = event.sequence
        if event.kind in {OwnerEventKind.TERMINAL, OwnerEventKind.OWNER_DISCONNECTED}:
            self.terminal = True


@dataclass(frozen=True)
class OwnerRequestRecord:
    request_id: str
    fingerprint: str
    state: RequestState
    task_id: str | None = None
    terminal_status: OwnerTerminalStatus | None = None


@dataclass(frozen=True)
class RequestRegistration:
    disposition: RequestDisposition
    record: OwnerRequestRecord


class OwnerRequestLedger:
    """In-memory request de-duplication used by contract and transport tests."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[OwnerRequest, OwnerRequestRecord]] = {}

    def register(self, request: OwnerRequest) -> RequestRegistration:
        fingerprint = hashlib.sha256(request.to_bytes(max_frame_bytes=10 * 1024 * 1024)).hexdigest()
        existing = self._records.get(request.request_id)
        if existing is None:
            record = OwnerRequestRecord(request.request_id, fingerprint, RequestState.SUBMITTED)
            self._records[request.request_id] = (request, record)
            return RequestRegistration(RequestDisposition.NEW, record)
        if existing[1].fingerprint == fingerprint:
            return RequestRegistration(RequestDisposition.DUPLICATE, existing[1])
        return RequestRegistration(RequestDisposition.CONFLICT, existing[1])

    def mark_accepted(self, request_id: str, *, task_id: str | None = None) -> OwnerRequestRecord:
        request, record = self._get(request_id)
        if record.state is RequestState.TERMINAL:
            raise _error("request_already_terminal", "request.request_id", "terminal requests cannot be accepted again")
        if record.state is RequestState.ACCEPTED:
            if record.task_id != task_id:
                raise _error("request_acceptance_conflict", "request.task_id", "accepted request already has a different Task ID")
            return record
        updated = OwnerRequestRecord(request_id, record.fingerprint, RequestState.ACCEPTED, task_id=task_id)
        self._records[request_id] = (request, updated)
        return updated

    def mark_terminal(self, request_id: str, *, status: OwnerTerminalStatus) -> OwnerRequestRecord:
        request, record = self._get(request_id)
        if record.state is RequestState.TERMINAL:
            if record.terminal_status is status:
                return record
            raise _error("request_terminal_conflict", "request.terminal_status", "terminal request already has a different status")
        updated = OwnerRequestRecord(request_id, record.fingerprint, RequestState.TERMINAL, task_id=record.task_id, terminal_status=_enum(status, OwnerTerminalStatus, "request.terminal_status"))
        self._records[request_id] = (request, updated)
        return updated

    def get(self, request_id: str) -> OwnerRequestRecord | None:
        entry = self._records.get(request_id)
        return entry[1] if entry else None

    def _get(self, request_id: str) -> tuple[OwnerRequest, OwnerRequestRecord]:
        entry = self._records.get(request_id)
        if entry is None:
            raise _error("unknown_request", "request.request_id", "has not been registered")
        return entry


@dataclass(frozen=True)
class OwnerWaitPolicy:
    connect_timeout: float = 5.0
    handshake_timeout: float = 5.0
    acceptance_timeout: float = 10.0
    response_idle_timeout: float = 10.0
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES

    def __post_init__(self) -> None:
        for name in ("connect_timeout", "handshake_timeout", "acceptance_timeout", "response_idle_timeout"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
                raise _error("invalid_timeout", f"wait.{name}", "must be a finite number greater than zero")
        if not isinstance(self.max_frame_bytes, int) or self.max_frame_bytes < 1024:
            raise _error("invalid_integer", "wait.max_frame_bytes", "must be at least 1024 bytes")

    def timeout_for(self, phase: OwnerWaitPhase) -> float:
        phase = _enum(phase, OwnerWaitPhase, "wait.phase")
        return {OwnerWaitPhase.CONNECT: self.connect_timeout, OwnerWaitPhase.HANDSHAKE: self.handshake_timeout, OwnerWaitPhase.ACCEPTANCE: self.acceptance_timeout, OwnerWaitPhase.RESPONSE_IDLE: self.response_idle_timeout}[phase]


class OwnerLivenessTracker:
    """Track response-idle activity without turning it into a total task timeout."""

    def __init__(self, policy: OwnerWaitPolicy, *, now: float = 0.0) -> None:
        if not math.isfinite(now):
            raise _error("invalid_timestamp", "liveness.now", "must be finite")
        self._timeout = policy.response_idle_timeout
        self._last_activity = now

    @property
    def last_activity(self) -> float:
        return self._last_activity

    def observe(self, event: OwnerResponseEvent, *, now: float) -> None:
        if not math.isfinite(now):
            raise _error("invalid_timestamp", "liveness.now", "must be finite")
        if event.kind in {OwnerEventKind.ACCEPTED, OwnerEventKind.PROGRESS, OwnerEventKind.CONTROL, OwnerEventKind.HEARTBEAT}:
            self._last_activity = now

    def expires_at(self) -> float:
        return self._last_activity + self._timeout

    def is_expired(self, *, now: float) -> bool:
        if not math.isfinite(now):
            raise _error("invalid_timestamp", "liveness.now", "must be finite")
        return now >= self.expires_at()


@dataclass
class _InMemoryOwnerSession:
    request: OwnerRequest
    stream: OwnerResponseStream
    liveness: OwnerLivenessTracker
    submitted_at: float
    pending: deque[OwnerResponseEvent] = field(default_factory=deque)
    connected: bool = True


class InMemoryOwnerTransport:
    """Deterministic transport harness for owner/client contract tests.

    ``poll`` is intentionally non-blocking.  A caller can drive virtual time
    through it and prove that acceptance and response-idle deadlines produce a
    structured ``owner_disconnected`` event without sleeping or starting a
    process.  The Windows transport in later P5 tasks will preserve this API's
    request/event semantics while replacing the queue with a local pipe.
    """

    def __init__(self, *, policy: OwnerWaitPolicy | None = None) -> None:
        self.policy = policy or OwnerWaitPolicy()
        self.ledger = OwnerRequestLedger()
        self._sessions: dict[str, _InMemoryOwnerSession] = {}

    def submit(self, request: OwnerRequest, *, now: float = 0.0) -> RequestRegistration:
        if not math.isfinite(now):
            raise _error("invalid_timestamp", "transport.now", "must be finite")
        registration = self.ledger.register(request)
        if registration.disposition is RequestDisposition.CONFLICT:
            return registration
        if registration.disposition is RequestDisposition.DUPLICATE:
            return registration
        self._sessions[request.request_id] = _InMemoryOwnerSession(
            request=request,
            stream=OwnerResponseStream(request.request_id),
            liveness=OwnerLivenessTracker(self.policy, now=now),
            submitted_at=now,
        )
        return registration

    def deliver(self, event: OwnerResponseEvent, *, now: float = 0.0) -> None:
        session = self._session(event.request_id)
        if not session.connected:
            raise _error("transport_closed", "event.request_id", "owner connection is closed")
        session.stream.push(event)
        session.liveness.observe(event, now=now)
        session.pending.append(event)
        if event.kind is OwnerEventKind.ACCEPTED:
            task_id = event.payload.get("task_id")
            self.ledger.mark_accepted(event.request_id, task_id=task_id if isinstance(task_id, str) else None)
        elif event.kind is OwnerEventKind.TERMINAL:
            self.ledger.mark_terminal(event.request_id, status=_enum(event.payload["status"], OwnerTerminalStatus, "event.payload.status"))
        elif event.kind is OwnerEventKind.OWNER_DISCONNECTED:
            session.connected = False

    def poll(self, request_id: str, *, now: float = 0.0) -> OwnerResponseEvent | None:
        session = self._session(request_id)
        if not math.isfinite(now):
            raise _error("invalid_timestamp", "transport.now", "must be finite")
        if session.pending:
            return session.pending.popleft()
        if not session.connected:
            return None
        if not session.stream.accepted and now >= session.submitted_at + self.policy.acceptance_timeout:
            event = OwnerResponseEvent.owner_disconnected(
                request_id,
                session.stream.last_sequence + 1,
                phase=OwnerWaitPhase.ACCEPTANCE,
                outcome=OwnerDisconnectOutcome.NOT_ACCEPTED,
                last_sequence=session.stream.last_sequence,
                message="owner did not acknowledge the request",
            )
            self.deliver(event, now=now)
            return session.pending.popleft()
        if session.stream.accepted and session.liveness.is_expired(now=now):
            record = self.ledger.get(request_id)
            task_id = record.task_id if record is not None else None
            event = OwnerResponseEvent.owner_disconnected(
                request_id,
                session.stream.last_sequence + 1,
                phase=OwnerWaitPhase.RESPONSE_IDLE,
                outcome=OwnerDisconnectOutcome.ACCEPTED_COMPLETION_UNKNOWN,
                known_task_id=task_id,
                last_sequence=session.stream.last_sequence,
                message="owner stopped sending response events",
            )
            self.deliver(event, now=now)
            return session.pending.popleft()
        return None

    def disconnect(self, request_id: str, *, phase: OwnerWaitPhase, outcome: OwnerDisconnectOutcome, now: float = 0.0, known_task_id: str | None = None, owner_session_id: str | None = None, terminal_persisted: bool = False) -> OwnerResponseEvent:
        session = self._session(request_id)
        event = OwnerResponseEvent.owner_disconnected(
            request_id,
            session.stream.last_sequence + 1,
            phase=phase,
            outcome=outcome,
            known_task_id=known_task_id,
            owner_session_id=owner_session_id,
            last_sequence=session.stream.last_sequence,
            terminal_persisted=terminal_persisted,
        )
        self.deliver(event, now=now)
        return event

    def _session(self, request_id: str) -> _InMemoryOwnerSession:
        session = self._sessions.get(request_id)
        if session is None:
            raise _error("unknown_request", "request.request_id", "has not been submitted to this transport")
        return session
