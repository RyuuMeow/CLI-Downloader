from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, TypeAlias
from urllib.parse import urlsplit

from cli_downloader.core.errors import ContractValidationError

if TYPE_CHECKING:
    from cli_downloader.core.widgets import Page


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FrozenJsonValue: TypeAlias = (
    JsonScalar | tuple["FrozenJsonValue", ...] | Mapping[str, "FrozenJsonValue"]
)

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_MAX_JSON_DEPTH = 32


class ResultKind(StrEnum):
    GALLERY = "gallery"
    LIST_PAGE = "list_page"
    MESSAGE = "message"
    PROGRESS = "progress"
    INTERACTION_REQUIRED = "interaction_required"
    ERROR = "error"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


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


def _require_identifier(value: object, path: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise _error(
            "invalid_identifier",
            path,
            "must use lowercase letters, numbers, dots, underscores, or hyphens",
        )
    return value


def _require_url(
    value: object,
    path: str,
    *,
    allowed_schemes: frozenset[str] = frozenset({"http", "https"}),
) -> str:
    url = _require_text(value, path, maximum=8192)
    if any(character.isspace() for character in url):
        raise _error("invalid_url", path, "must not contain whitespace")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise _error("invalid_url", path, "must be a valid absolute URL") from error
    scheme = parsed.scheme.lower()
    if not scheme:
        raise _error("invalid_url", path, "must be an absolute URL")
    if scheme not in allowed_schemes:
        raise _error(
            "unsupported_url_scheme",
            path,
            f"must use one of: {', '.join(sorted(allowed_schemes))}",
        )
    if scheme in {"http", "https"} and not hostname:
        raise _error("invalid_url", path, "HTTP URLs must include a hostname")
    if not parsed.netloc and not parsed.path and not parsed.query:
        raise _error("invalid_url", path, "must include an authority, path, or query")
    if parsed.username is not None or parsed.password is not None:
        raise _error("url_credentials_forbidden", path, "must not contain embedded credentials")
    return url


def _require_non_negative_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _error("invalid_integer", path, "must be a non-negative integer")
    return value


def _require_positive_int(value: object, path: str) -> int:
    value = _require_non_negative_int(value, path)
    if value == 0:
        raise _error("invalid_integer", path, "must be greater than zero")
    return value


def _freeze_json(
    value: object,
    path: str,
    *,
    depth: int = 0,
    active_ids: set[int] | None = None,
) -> FrozenJsonValue:
    if depth > _MAX_JSON_DEPTH:
        raise _error("json_depth_exceeded", path, f"must not exceed {_MAX_JSON_DEPTH} levels")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _error("invalid_json_number", path, "must be a finite number")
        return value

    if active_ids is None:
        active_ids = set()
    if isinstance(value, Mapping):
        object_id = id(value)
        if object_id in active_ids:
            raise _error("json_cycle", path, "must not contain reference cycles")
        active_ids.add(object_id)
        frozen: dict[str, FrozenJsonValue] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise _error("invalid_json_key", path, "object keys must be strings")
                frozen[key] = _freeze_json(
                    item,
                    f"{path}.{key}",
                    depth=depth + 1,
                    active_ids=active_ids,
                )
        finally:
            active_ids.remove(object_id)
        return MappingProxyType(frozen)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        object_id = id(value)
        if object_id in active_ids:
            raise _error("json_cycle", path, "must not contain reference cycles")
        active_ids.add(object_id)
        try:
            return tuple(
                _freeze_json(
                    item,
                    f"{path}[{index}]",
                    depth=depth + 1,
                    active_ids=active_ids,
                )
                for index, item in enumerate(value)
            )
        finally:
            active_ids.remove(object_id)

    raise _error("non_json_value", path, "must contain only JSON-compatible values")


def _freeze_json_object(value: object, path: str) -> Mapping[str, FrozenJsonValue]:
    frozen = _freeze_json(value, path)
    if not isinstance(frozen, Mapping):
        raise _error("invalid_json_object", path, "must be a JSON object")
    return frozen


def _thaw_json(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_text_mapping(
    value: object,
    path: str,
    *,
    identifier_keys: bool = True,
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise _error("invalid_mapping", path, "must be an object with string values")
    frozen: dict[str, str] = {}
    for key, item in value.items():
        if identifier_keys:
            key = _require_identifier(key, f"{path}.key")
        else:
            key = _require_text(key, f"{path}.key", maximum=256)
        frozen[key] = _require_text(item, f"{path}.{key}")
    return MappingProxyType(frozen)


def _freeze_text_tuple(value: object, path: str, *, identifiers: bool = False) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("invalid_sequence", path, "must be a sequence of strings")
    validator = _require_identifier if identifiers else _require_text
    items = tuple(validator(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(set(items)) != len(items):
        raise _error("duplicate_value", path, "must not contain duplicate values")
    return items


@dataclass(frozen=True)
class RouteMatch:
    plugin_id: str
    site_id: str
    rule_name: str
    operation_id: str
    normalized_url: str
    captures: Mapping[str, str] = field(default_factory=dict)
    parameter_schema: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    required_confirmations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "plugin_id", _require_identifier(self.plugin_id, "route.plugin_id")
        )
        object.__setattr__(self, "site_id", _require_identifier(self.site_id, "route.site_id"))
        object.__setattr__(
            self, "rule_name", _require_identifier(self.rule_name, "route.rule_name")
        )
        object.__setattr__(
            self, "operation_id", _require_identifier(self.operation_id, "route.operation_id")
        )
        object.__setattr__(
            self, "normalized_url", _require_url(self.normalized_url, "route.normalized_url")
        )
        object.__setattr__(self, "captures", _freeze_text_mapping(self.captures, "route.captures"))
        object.__setattr__(
            self,
            "parameter_schema",
            _freeze_json_object(self.parameter_schema, "route.parameter_schema"),
        )
        object.__setattr__(
            self,
            "required_confirmations",
            _freeze_text_tuple(
                self.required_confirmations,
                "route.required_confirmations",
                identifiers=True,
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "plugin_id": self.plugin_id,
            "site_id": self.site_id,
            "rule_name": self.rule_name,
            "operation_id": self.operation_id,
            "normalized_url": self.normalized_url,
            "captures": dict(self.captures),
            "parameter_schema": _thaw_json(self.parameter_schema),
            "required_confirmations": list(self.required_confirmations),
        }


@dataclass(frozen=True)
class OperationRequest:
    request_id: str
    plugin_id: str
    site_id: str
    operation_id: str
    source_url: str
    route: RouteMatch
    params: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    profile: str | None = None
    settings: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    setting_refs: Mapping[str, str] = field(default_factory=dict)
    secret_refs: Mapping[str, str] = field(default_factory=dict)
    confirmations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _require_text(self.request_id, "request.request_id", maximum=128)
        )
        object.__setattr__(
            self, "plugin_id", _require_identifier(self.plugin_id, "request.plugin_id")
        )
        object.__setattr__(self, "site_id", _require_identifier(self.site_id, "request.site_id"))
        object.__setattr__(
            self, "operation_id", _require_identifier(self.operation_id, "request.operation_id")
        )
        object.__setattr__(self, "source_url", _require_url(self.source_url, "request.source_url"))
        if not isinstance(self.route, RouteMatch):
            raise _error("invalid_route", "request.route", "must be a RouteMatch")
        for field_name in ("plugin_id", "site_id", "operation_id"):
            if getattr(self, field_name) != getattr(self.route, field_name):
                raise _error(
                    "route_mismatch",
                    f"request.{field_name}",
                    f"must match route.{field_name}",
                )
        object.__setattr__(self, "params", _freeze_json_object(self.params, "request.params"))
        object.__setattr__(
            self, "profile", _optional_text(self.profile, "request.profile", maximum=128)
        )
        object.__setattr__(self, "settings", _freeze_json_object(self.settings, "request.settings"))
        object.__setattr__(
            self, "setting_refs", _freeze_text_mapping(self.setting_refs, "request.setting_refs")
        )
        object.__setattr__(
            self, "secret_refs", _freeze_text_mapping(self.secret_refs, "request.secret_refs")
        )
        object.__setattr__(
            self,
            "confirmations",
            _freeze_text_tuple(self.confirmations, "request.confirmations", identifiers=True),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id,
            "plugin_id": self.plugin_id,
            "site_id": self.site_id,
            "operation_id": self.operation_id,
            "source_url": self.source_url,
            "route": self.route.to_dict(),
            "params": _thaw_json(self.params),
            "profile": self.profile,
            "settings": _thaw_json(self.settings),
            "setting_refs": dict(self.setting_refs),
            "secret_refs": dict(self.secret_refs),
            "confirmations": list(self.confirmations),
        }


@dataclass(frozen=True)
class AssetData:
    asset_id: str
    url: str
    filename: str | None = None
    index: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    referrer: str | None = None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "asset_id", _require_text(self.asset_id, "asset.asset_id", maximum=512)
        )
        object.__setattr__(
            self,
            "url",
            _require_url(
                self.url,
                "asset.url",
                allowed_schemes=frozenset({"http", "https", "magnet"}),
            ),
        )
        filename = _optional_text(self.filename, "asset.filename", maximum=255)
        if filename in {".", ".."} or (
            filename is not None and ("/" in filename or "\\" in filename)
        ):
            raise _error("invalid_filename", "asset.filename", "must be a single filename")
        object.__setattr__(self, "filename", filename)
        if self.index is not None:
            object.__setattr__(self, "index", _require_non_negative_int(self.index, "asset.index"))
        object.__setattr__(
            self,
            "headers",
            _freeze_text_mapping(self.headers, "asset.headers", identifier_keys=False),
        )
        if self.referrer is not None:
            object.__setattr__(self, "referrer", _require_url(self.referrer, "asset.referrer"))
        object.__setattr__(self, "metadata", _freeze_json_object(self.metadata, "asset.metadata"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "asset_id": self.asset_id,
            "url": self.url,
            "filename": self.filename,
            "index": self.index,
            "headers": dict(self.headers),
            "referrer": self.referrer,
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True)
class GalleryData:
    source_url: str
    site_id: str
    gallery_id: str
    title: str
    authors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    cover: AssetData | None = None
    expected_asset_count: int | None = None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_url", _require_url(self.source_url, "gallery.source_url"))
        object.__setattr__(self, "site_id", _require_identifier(self.site_id, "gallery.site_id"))
        object.__setattr__(
            self, "gallery_id", _require_text(self.gallery_id, "gallery.gallery_id", maximum=512)
        )
        object.__setattr__(self, "title", _require_text(self.title, "gallery.title"))
        object.__setattr__(self, "authors", _freeze_text_tuple(self.authors, "gallery.authors"))
        object.__setattr__(self, "tags", _freeze_text_tuple(self.tags, "gallery.tags"))
        if self.cover is not None and not isinstance(self.cover, AssetData):
            raise _error("invalid_asset", "gallery.cover", "must be an AssetData")
        if self.expected_asset_count is not None:
            object.__setattr__(
                self,
                "expected_asset_count",
                _require_non_negative_int(
                    self.expected_asset_count, "gallery.expected_asset_count"
                ),
            )
        object.__setattr__(self, "metadata", _freeze_json_object(self.metadata, "gallery.metadata"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source_url": self.source_url,
            "site_id": self.site_id,
            "gallery_id": self.gallery_id,
            "title": self.title,
            "authors": list(self.authors),
            "tags": list(self.tags),
            "cover": self.cover.to_dict() if self.cover is not None else None,
            "expected_asset_count": self.expected_asset_count,
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True)
class ListItemData:
    item_id: str
    source_url: str
    title: str
    thumbnail: AssetData | None = None
    summary: str | None = None
    tags: tuple[str, ...] = ()
    resolved: bool = False
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "item_id", _require_text(self.item_id, "list_item.item_id", maximum=512)
        )
        object.__setattr__(
            self, "source_url", _require_url(self.source_url, "list_item.source_url")
        )
        object.__setattr__(self, "title", _require_text(self.title, "list_item.title"))
        if self.thumbnail is not None and not isinstance(self.thumbnail, AssetData):
            raise _error("invalid_asset", "list_item.thumbnail", "must be an AssetData")
        object.__setattr__(self, "summary", _optional_text(self.summary, "list_item.summary"))
        object.__setattr__(self, "tags", _freeze_text_tuple(self.tags, "list_item.tags"))
        if not isinstance(self.resolved, bool):
            raise _error("invalid_boolean", "list_item.resolved", "must be a boolean")
        object.__setattr__(
            self, "metadata", _freeze_json_object(self.metadata, "list_item.metadata")
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "item_id": self.item_id,
            "source_url": self.source_url,
            "title": self.title,
            "thumbnail": self.thumbnail.to_dict() if self.thumbnail is not None else None,
            "summary": self.summary,
            "tags": list(self.tags),
            "resolved": self.resolved,
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True)
class PageInfo:
    current_page: int
    page_size: int
    total_items: int | None = None
    total_pages: int | None = None
    next_token: str | None = None
    previous_token: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "current_page", _require_positive_int(self.current_page, "page_info.current_page")
        )
        object.__setattr__(
            self, "page_size", _require_positive_int(self.page_size, "page_info.page_size")
        )
        if self.total_items is not None:
            object.__setattr__(
                self,
                "total_items",
                _require_non_negative_int(self.total_items, "page_info.total_items"),
            )
        if self.total_pages is not None:
            object.__setattr__(
                self,
                "total_pages",
                _require_non_negative_int(self.total_pages, "page_info.total_pages"),
            )
            if self.total_pages > 0 and self.current_page > self.total_pages:
                raise _error(
                    "invalid_page_range",
                    "page_info.current_page",
                    "must not exceed total_pages",
                )
            if self.total_pages == 0 and self.total_items not in {None, 0}:
                raise _error(
                    "invalid_page_range",
                    "page_info.total_pages",
                    "zero pages require zero total items",
                )
        object.__setattr__(
            self, "next_token", _optional_text(self.next_token, "page_info.next_token")
        )
        object.__setattr__(
            self, "previous_token", _optional_text(self.previous_token, "page_info.previous_token")
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "current_page": self.current_page,
            "page_size": self.page_size,
            "total_items": self.total_items,
            "total_pages": self.total_pages,
            "next_token": self.next_token,
            "previous_token": self.previous_token,
        }


@dataclass(frozen=True)
class ListPageData:
    title: str
    items: tuple[ListItemData, ...]
    page_info: PageInfo
    active_filters: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    available_filters: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _require_text(self.title, "list_page.title"))
        if not isinstance(self.items, Sequence) or isinstance(self.items, (str, bytes, bytearray)):
            raise _error(
                "invalid_sequence", "list_page.items", "must be a sequence of ListItemData"
            )
        items = tuple(self.items)
        if any(not isinstance(item, ListItemData) for item in items):
            raise _error("invalid_list_item", "list_page.items", "must contain only ListItemData")
        item_ids = tuple(item.item_id for item in items)
        if len(set(item_ids)) != len(item_ids):
            raise _error("duplicate_item_id", "list_page.items", "item ids must be unique")
        object.__setattr__(self, "items", items)
        if not isinstance(self.page_info, PageInfo):
            raise _error("invalid_page_info", "list_page.page_info", "must be a PageInfo")
        object.__setattr__(
            self,
            "active_filters",
            _freeze_json_object(self.active_filters, "list_page.active_filters"),
        )
        object.__setattr__(
            self,
            "available_filters",
            _freeze_json_object(self.available_filters, "list_page.available_filters"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "title": self.title,
            "items": [item.to_dict() for item in self.items],
            "page_info": self.page_info.to_dict(),
            "active_filters": _thaw_json(self.active_filters),
            "available_filters": _thaw_json(self.available_filters),
        }


@dataclass(frozen=True)
class MessageData:
    message: str
    title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _require_text(self.message, "message.message"))
        object.__setattr__(self, "title", _optional_text(self.title, "message.title"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"message": self.message, "title": self.title}


@dataclass(frozen=True)
class ProgressData:
    current: int
    total: int | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "current", _require_non_negative_int(self.current, "progress.current")
        )
        if self.total is not None:
            object.__setattr__(
                self, "total", _require_non_negative_int(self.total, "progress.total")
            )
            if self.current > self.total:
                raise _error("invalid_progress", "progress.current", "must not exceed total")
        object.__setattr__(self, "message", _optional_text(self.message, "progress.message"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"current": self.current, "total": self.total, "message": self.message}


@dataclass(frozen=True)
class ErrorData:
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_identifier(self.code, "error.code"))
        object.__setattr__(self, "message", _require_text(self.message, "error.message"))
        if not isinstance(self.retryable, bool):
            raise _error("invalid_boolean", "error.retryable", "must be a boolean")
        object.__setattr__(self, "details", _freeze_json_object(self.details, "error.details"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": _thaw_json(self.details),
        }


@dataclass(frozen=True)
class InteractionRequiredData:
    message: str
    required_fields: tuple[str, ...] = ()
    required_confirmations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", _require_text(self.message, "interaction.message"))
        object.__setattr__(
            self,
            "required_fields",
            _freeze_text_tuple(
                self.required_fields,
                "interaction.required_fields",
                identifiers=True,
            ),
        )
        object.__setattr__(
            self,
            "required_confirmations",
            _freeze_text_tuple(
                self.required_confirmations,
                "interaction.required_confirmations",
                identifiers=True,
            ),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "message": self.message,
            "required_fields": list(self.required_fields),
            "required_confirmations": list(self.required_confirmations),
        }


@dataclass(frozen=True)
class ContinuationData:
    operation_id: str
    token: str
    params: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _require_identifier(self.operation_id, "continuation.operation_id"),
        )
        object.__setattr__(self, "token", _require_text(self.token, "continuation.token"))
        object.__setattr__(self, "params", _freeze_json_object(self.params, "continuation.params"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "token": self.token,
            "params": _thaw_json(self.params),
        }


@dataclass(frozen=True)
class OperationDiagnostic:
    code: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    path: str | None = None
    details: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_identifier(self.code, "diagnostic.code"))
        object.__setattr__(self, "message", _require_text(self.message, "diagnostic.message"))
        if not isinstance(self.severity, DiagnosticSeverity):
            raise _error("invalid_severity", "diagnostic.severity", "must be a DiagnosticSeverity")
        object.__setattr__(self, "path", _optional_text(self.path, "diagnostic.path"))
        object.__setattr__(self, "details", _freeze_json_object(self.details, "diagnostic.details"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "path": self.path,
            "details": _thaw_json(self.details),
        }


OperationData: TypeAlias = (
    GalleryData | ListPageData | MessageData | ProgressData | InteractionRequiredData | ErrorData
)

_RESULT_DATA_TYPES: dict[ResultKind, type[OperationData]] = {
    ResultKind.GALLERY: GalleryData,
    ResultKind.LIST_PAGE: ListPageData,
    ResultKind.MESSAGE: MessageData,
    ResultKind.PROGRESS: ProgressData,
    ResultKind.INTERACTION_REQUIRED: InteractionRequiredData,
    ResultKind.ERROR: ErrorData,
}


@dataclass(frozen=True)
class OperationResult:
    request_id: str
    result_kind: ResultKind
    data: OperationData
    page: Page | None = None
    available_actions: tuple[str, ...] = ()
    continuation: ContinuationData | None = None
    diagnostics: tuple[OperationDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _require_text(self.request_id, "result.request_id", maximum=128)
        )
        if not isinstance(self.result_kind, ResultKind):
            raise _error("invalid_result_kind", "result.result_kind", "must be a ResultKind")
        expected_type = _RESULT_DATA_TYPES[self.result_kind]
        if not isinstance(self.data, expected_type):
            raise _error(
                "result_data_mismatch",
                "result.data",
                f"{self.result_kind.value} results require {expected_type.__name__}",
            )
        if self.page is not None:
            from cli_downloader.core.widgets import Page

            if type(self.page) is not Page:
                raise _error("invalid_page", "result.page", "must be a core Page")
        object.__setattr__(
            self,
            "available_actions",
            _freeze_text_tuple(
                self.available_actions,
                "result.available_actions",
                identifiers=True,
            ),
        )
        if self.continuation is not None and not isinstance(self.continuation, ContinuationData):
            raise _error(
                "invalid_continuation",
                "result.continuation",
                "must be a ContinuationData",
            )
        if not isinstance(self.diagnostics, Sequence) or isinstance(
            self.diagnostics, (str, bytes, bytearray)
        ):
            raise _error(
                "invalid_sequence",
                "result.diagnostics",
                "must be a sequence of OperationDiagnostic",
            )
        diagnostics = tuple(self.diagnostics)
        if any(not isinstance(item, OperationDiagnostic) for item in diagnostics):
            raise _error(
                "invalid_diagnostic",
                "result.diagnostics",
                "must contain only OperationDiagnostic",
            )
        object.__setattr__(self, "diagnostics", diagnostics)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id,
            "result_kind": self.result_kind.value,
            "data": self.data.to_dict(),
            "page": self.page.to_dict() if self.page is not None else None,
            "available_actions": list(self.available_actions),
            "continuation": self.continuation.to_dict() if self.continuation is not None else None,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }
