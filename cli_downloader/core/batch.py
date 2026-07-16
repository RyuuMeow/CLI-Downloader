from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Mapping, TypeVar

from cli_downloader.core.errors import ContractValidationError
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.operations import (
    FrozenJsonValue,
    JsonValue,
    _freeze_json_object,
    _freeze_text_mapping,
    _optional_text,
    _require_identifier,
    _require_non_negative_int,
    _require_positive_int,
    _require_text,
    _require_url,
    _thaw_json,
)


class FailureOrigin(StrEnum):
    PLUGIN = "plugin"
    SYSTEM = "system"


class FailureStage(StrEnum):
    CLASSIFY = "classify"
    RESOLVE = "resolve"
    REQUEST = "request"
    ENUMERATE = "enumerate"
    PARSE = "parse"
    BUILD_PACKAGE = "build_package"
    VALIDATE = "validate"
    DEDUPLICATE = "deduplicate"
    POLICY = "policy"
    CREATE_CHILD = "create_child"
    SCHEDULE = "schedule"
    FETCH = "fetch"
    VERIFY = "verify"
    WRITE = "write"


class BatchExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BatchEntryStatus(StrEnum):
    PENDING_RESOLUTION = "pending_resolution"
    RESOLVING = "resolving"
    PACKAGE_READY = "package_ready"
    RESOLUTION_FAILED = "resolution_failed"
    PACKAGE_REJECTED = "package_rejected"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    CANCELLED = "cancelled"


class DownloadChildStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssetStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Checkpoint(StrEnum):
    SELECTED = "selected"
    PACKAGE_RESOLVED = "package_resolved"
    PACKAGE_VALIDATED = "package_validated"
    CHILD_CREATED = "child_created"
    ASSET_FETCHED = "asset_fetched"
    ASSET_VERIFIED = "asset_verified"
    ASSET_WRITTEN = "asset_written"


class RetryScope(StrEnum):
    ENTRY = "entry"
    CHILD = "child"
    ASSET = "asset"


class DuplicateKind(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"


_PLUGIN_STAGES = frozenset(
    {
        FailureStage.CLASSIFY,
        FailureStage.RESOLVE,
        FailureStage.REQUEST,
        FailureStage.ENUMERATE,
        FailureStage.PARSE,
        FailureStage.BUILD_PACKAGE,
    }
)
_SYSTEM_STAGES = frozenset(FailureStage) - _PLUGIN_STAGES
_PRE_CHILD_STAGES = frozenset(
    {
        FailureStage.VALIDATE,
        FailureStage.DEDUPLICATE,
        FailureStage.POLICY,
        FailureStage.CREATE_CHILD,
    }
)
_CHILD_STAGES = frozenset(
    {
        FailureStage.SCHEDULE,
        FailureStage.FETCH,
        FailureStage.VERIFY,
        FailureStage.WRITE,
    }
)
_ASSET_STAGES = frozenset({FailureStage.FETCH, FailureStage.VERIFY, FailureStage.WRITE})
_ENTRY_TRANSITIONS = {
    BatchEntryStatus.PENDING_RESOLUTION: frozenset(
        {
            BatchEntryStatus.RESOLVING,
            BatchEntryStatus.DUPLICATE_SKIPPED,
            BatchEntryStatus.CANCELLED,
        }
    ),
    BatchEntryStatus.RESOLVING: frozenset(
        {
            BatchEntryStatus.PACKAGE_READY,
            BatchEntryStatus.RESOLUTION_FAILED,
            BatchEntryStatus.PACKAGE_REJECTED,
            BatchEntryStatus.CANCELLED,
        }
    ),
    BatchEntryStatus.RESOLUTION_FAILED: frozenset(
        {BatchEntryStatus.RESOLVING, BatchEntryStatus.CANCELLED}
    ),
    BatchEntryStatus.PACKAGE_REJECTED: frozenset(
        {BatchEntryStatus.RESOLVING, BatchEntryStatus.CANCELLED}
    ),
    BatchEntryStatus.CANCELLED: frozenset({BatchEntryStatus.RESOLVING}),
    BatchEntryStatus.PACKAGE_READY: frozenset(),
    BatchEntryStatus.DUPLICATE_SKIPPED: frozenset(),
}
_CHILD_TRANSITIONS = {
    DownloadChildStatus.QUEUED: frozenset(
        {
            DownloadChildStatus.DOWNLOADING,
            DownloadChildStatus.FAILED,
            DownloadChildStatus.CANCELLED,
        }
    ),
    DownloadChildStatus.DOWNLOADING: frozenset(
        {
            DownloadChildStatus.QUEUED,
            DownloadChildStatus.COMPLETED,
            DownloadChildStatus.PARTIALLY_COMPLETED,
            DownloadChildStatus.FAILED,
            DownloadChildStatus.CANCELLED,
        }
    ),
    DownloadChildStatus.PARTIALLY_COMPLETED: frozenset({DownloadChildStatus.QUEUED}),
    DownloadChildStatus.FAILED: frozenset({DownloadChildStatus.QUEUED}),
    DownloadChildStatus.CANCELLED: frozenset({DownloadChildStatus.QUEUED}),
    DownloadChildStatus.COMPLETED: frozenset(),
}
_ASSET_TRANSITIONS = {
    AssetStatus.QUEUED: frozenset(
        {AssetStatus.DOWNLOADING, AssetStatus.FAILED, AssetStatus.CANCELLED}
    ),
    AssetStatus.DOWNLOADING: frozenset(
        {AssetStatus.COMPLETED, AssetStatus.FAILED, AssetStatus.CANCELLED, AssetStatus.QUEUED}
    ),
    AssetStatus.FAILED: frozenset({AssetStatus.QUEUED}),
    AssetStatus.CANCELLED: frozenset({AssetStatus.QUEUED}),
    AssetStatus.COMPLETED: frozenset(),
}

EnumT = TypeVar("EnumT", bound=StrEnum)


def _error(code: str, path: str, message: str) -> ContractValidationError:
    return ContractValidationError(code=code, path=path, message=message)


def _require_enum(value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if type(value) is not enum_type:
        raise _error("invalid_enum", path, f"must be a {enum_type.__name__}")
    return value


def _require_bool(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error("invalid_boolean", path, "must be a boolean")
    return value


def _require_hex_digest(value: object, path: str) -> str:
    digest = _require_text(value, path, maximum=64)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise _error("invalid_sha256", path, "must be a lowercase SHA-256 hex digest")
    return digest


def _require_datetime(value: object, path: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _error("invalid_datetime", path, "must be a timezone-aware datetime")
    return value


def _require_unique_ids(values: object, path: str) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise _error("invalid_sequence", path, "must be a sequence of ids")
    result = tuple(
        _require_text(value, f"{path}[{index}]", maximum=128) for index, value in enumerate(values)
    )
    if not result:
        raise _error("empty_sequence", path, "must contain at least one id")
    if len(set(result)) != len(result):
        raise _error("duplicate_value", path, "must not contain duplicate ids")
    return result


def _validate_transition(
    current: EnumT,
    target: EnumT,
    transitions: Mapping[EnumT, frozenset[EnumT]],
    path: str,
) -> None:
    if target not in transitions[current]:
        raise _error(
            "invalid_status_transition",
            path,
            f"cannot transition from {current.value} to {target.value}",
        )


def validate_entry_transition(current: BatchEntryStatus, target: BatchEntryStatus) -> None:
    _require_enum(current, BatchEntryStatus, "entry.current_status")
    _require_enum(target, BatchEntryStatus, "entry.target_status")
    _validate_transition(current, target, _ENTRY_TRANSITIONS, "entry.status")


def validate_child_transition(current: DownloadChildStatus, target: DownloadChildStatus) -> None:
    _require_enum(current, DownloadChildStatus, "child.current_status")
    _require_enum(target, DownloadChildStatus, "child.target_status")
    _validate_transition(current, target, _CHILD_TRANSITIONS, "child.status")


def validate_asset_transition(current: AssetStatus, target: AssetStatus) -> None:
    _require_enum(current, AssetStatus, "asset.current_status")
    _require_enum(target, AssetStatus, "asset.target_status")
    _validate_transition(current, target, _ASSET_TRANSITIONS, "asset.status")


def derive_asset_id(site_id: str, gallery_id: str, plugin_asset_key: str) -> str:
    site_id = _require_identifier(site_id, "asset.site_id")
    gallery_id = _require_text(gallery_id, "asset.gallery_id", maximum=512)
    plugin_asset_key = _require_text(plugin_asset_key, "asset.plugin_asset_key", maximum=1024)
    identity = "\0".join((site_id, gallery_id, plugin_asset_key)).encode("utf-8")
    return sha256(identity).hexdigest()


@dataclass(frozen=True)
class FailureData:
    origin: FailureOrigin
    stage: FailureStage
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        origin = _require_enum(self.origin, FailureOrigin, "failure.origin")
        stage = _require_enum(self.stage, FailureStage, "failure.stage")
        expected_origin = FailureOrigin.PLUGIN if stage in _PLUGIN_STAGES else FailureOrigin.SYSTEM
        if origin is not expected_origin:
            raise _error(
                "failure_origin_mismatch",
                "failure.origin",
                f"{stage.value} failures must originate from {expected_origin.value}",
            )
        object.__setattr__(self, "code", _require_identifier(self.code, "failure.code"))
        object.__setattr__(self, "message", _require_text(self.message, "failure.message"))
        object.__setattr__(self, "retryable", _require_bool(self.retryable, "failure.retryable"))
        object.__setattr__(self, "details", _freeze_json_object(self.details, "failure.details"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "origin": self.origin.value,
            "stage": self.stage.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": _thaw_json(self.details),
        }


@dataclass(frozen=True)
class DownloadAsset:
    site_id: str
    gallery_id: str
    plugin_asset_key: str
    url: str
    filename: str | None = None
    index: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    referrer: str | None = None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    asset_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "site_id", _require_identifier(self.site_id, "asset.site_id"))
        object.__setattr__(
            self, "gallery_id", _require_text(self.gallery_id, "asset.gallery_id", maximum=512)
        )
        object.__setattr__(
            self,
            "plugin_asset_key",
            _require_text(self.plugin_asset_key, "asset.plugin_asset_key", maximum=1024),
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
        object.__setattr__(
            self,
            "asset_id",
            derive_asset_id(self.site_id, self.gallery_id, self.plugin_asset_key),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "asset_id": self.asset_id,
            "site_id": self.site_id,
            "gallery_id": self.gallery_id,
            "plugin_asset_key": self.plugin_asset_key,
            "url": self.url,
            "filename": self.filename,
            "index": self.index,
            "headers": dict(self.headers),
            "referrer": self.referrer,
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True)
class DownloadPackage:
    package_id: str
    version: int
    plugin_id: str
    site_id: str
    gallery_id: str
    source_operation_id: str
    source_url: str
    strategy: DownloadStrategy
    assets: tuple[DownloadAsset, ...]
    title: str | None = None
    expires_at: datetime | None = None
    refresh_on_status: tuple[int, ...] = ()
    secret_refs: Mapping[str, str] = field(default_factory=dict)
    folder_template: str | None = None
    folder_fields: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "package_id", _require_text(self.package_id, "package.package_id", maximum=128)
        )
        object.__setattr__(self, "version", _require_positive_int(self.version, "package.version"))
        object.__setattr__(
            self, "plugin_id", _require_identifier(self.plugin_id, "package.plugin_id")
        )
        object.__setattr__(self, "site_id", _require_identifier(self.site_id, "package.site_id"))
        object.__setattr__(
            self, "gallery_id", _require_text(self.gallery_id, "package.gallery_id", maximum=512)
        )
        object.__setattr__(
            self,
            "source_operation_id",
            _require_identifier(self.source_operation_id, "package.source_operation_id"),
        )
        object.__setattr__(self, "source_url", _require_url(self.source_url, "package.source_url"))
        if self.title is not None:
            object.__setattr__(
                self,
                "title",
                _require_text(self.title, "package.title", maximum=1024),
            )
        strategy = _require_enum(self.strategy, DownloadStrategy, "package.strategy")
        if strategy is DownloadStrategy.AUTO:
            raise _error(
                "incomplete_package",
                "package.strategy",
                "must select an executable download strategy",
            )
        if not isinstance(self.assets, (tuple, list)) or not self.assets:
            raise _error("incomplete_package", "package.assets", "must contain at least one asset")
        assets = tuple(self.assets)
        seen_ids: set[str] = set()
        seen_indexes: set[int] = set()
        for index, asset in enumerate(assets):
            if type(asset) is not DownloadAsset:
                raise _error("invalid_asset", f"package.assets[{index}]", "must be a DownloadAsset")
            if (asset.site_id, asset.gallery_id) != (self.site_id, self.gallery_id):
                raise _error(
                    "asset_identity_mismatch",
                    f"package.assets[{index}]",
                    "site_id and gallery_id must match the package",
                )
            if asset.asset_id in seen_ids:
                raise _error(
                    "duplicate_asset_id", "package.assets", "must not contain duplicate assets"
                )
            seen_ids.add(asset.asset_id)
            if asset.index is not None:
                if asset.index in seen_indexes:
                    raise _error(
                        "duplicate_asset_index",
                        "package.assets",
                        "must not contain duplicate indexes",
                    )
                seen_indexes.add(asset.index)
        object.__setattr__(self, "assets", assets)
        if self.expires_at is not None:
            object.__setattr__(
                self, "expires_at", _require_datetime(self.expires_at, "package.expires_at")
            )
        if not isinstance(self.refresh_on_status, (tuple, list)):
            raise _error("invalid_sequence", "package.refresh_on_status", "must be a sequence")
        statuses = tuple(
            _require_non_negative_int(value, f"package.refresh_on_status[{index}]")
            for index, value in enumerate(self.refresh_on_status)
        )
        if any(value < 100 or value > 599 for value in statuses):
            raise _error(
                "invalid_http_status",
                "package.refresh_on_status",
                "statuses must be 100 through 599",
            )
        if len(set(statuses)) != len(statuses):
            raise _error(
                "duplicate_value", "package.refresh_on_status", "must not contain duplicates"
            )
        object.__setattr__(self, "refresh_on_status", statuses)
        object.__setattr__(
            self, "secret_refs", _freeze_text_mapping(self.secret_refs, "package.secret_refs")
        )
        if self.folder_template is not None:
            object.__setattr__(
                self,
                "folder_template",
                _require_text(self.folder_template, "package.folder_template", maximum=2048),
            )
        object.__setattr__(
            self,
            "folder_fields",
            _freeze_json_object(self.folder_fields, "package.folder_fields"),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "package_id": self.package_id,
            "version": self.version,
            "plugin_id": self.plugin_id,
            "site_id": self.site_id,
            "gallery_id": self.gallery_id,
            "source_operation_id": self.source_operation_id,
            "source_url": self.source_url,
            "strategy": self.strategy.value,
            "assets": [asset.to_dict() for asset in self.assets],
            "title": self.title,
            "expires_at": self.expires_at.isoformat() if self.expires_at is not None else None,
            "refresh_on_status": list(self.refresh_on_status),
            "secret_refs": dict(self.secret_refs),
            "folder_template": self.folder_template,
            "folder_fields": _thaw_json(self.folder_fields),
        }


@dataclass(frozen=True)
class BatchParent:
    parent_id: str
    entry_ids: tuple[str, ...]
    title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "parent_id", _require_text(self.parent_id, "parent.parent_id", maximum=128)
        )
        object.__setattr__(
            self, "entry_ids", _require_unique_ids(self.entry_ids, "parent.entry_ids")
        )
        if self.title is not None:
            object.__setattr__(
                self,
                "title",
                _require_text(self.title, "parent.title", maximum=1024),
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "parent_id": self.parent_id,
            "entry_ids": list(self.entry_ids),
            "title": self.title,
        }


@dataclass(frozen=True)
class BatchEntry:
    entry_id: str
    parent_id: str
    plugin_id: str
    site_id: str
    candidate_id: str
    source_operation_id: str
    source_url: str
    title: str | None = None
    strategy: DownloadStrategy = DownloadStrategy.AUTO
    status: BatchEntryStatus = BatchEntryStatus.PENDING_RESOLUTION
    child_id: str | None = None
    failure: FailureData | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "entry_id", _require_text(self.entry_id, "entry.entry_id", maximum=128)
        )
        object.__setattr__(
            self, "parent_id", _require_text(self.parent_id, "entry.parent_id", maximum=128)
        )
        object.__setattr__(
            self, "plugin_id", _require_identifier(self.plugin_id, "entry.plugin_id")
        )
        object.__setattr__(self, "site_id", _require_identifier(self.site_id, "entry.site_id"))
        object.__setattr__(
            self,
            "candidate_id",
            _require_text(self.candidate_id, "entry.candidate_id", maximum=512),
        )
        object.__setattr__(
            self,
            "source_operation_id",
            _require_identifier(self.source_operation_id, "entry.source_operation_id"),
        )
        object.__setattr__(self, "source_url", _require_url(self.source_url, "entry.source_url"))
        object.__setattr__(self, "title", _optional_text(self.title, "entry.title"))
        strategy = _require_enum(self.strategy, DownloadStrategy, "entry.strategy")
        object.__setattr__(self, "strategy", strategy)
        status = _require_enum(self.status, BatchEntryStatus, "entry.status")
        object.__setattr__(
            self, "child_id", _optional_text(self.child_id, "entry.child_id", maximum=128)
        )
        if self.failure is not None and type(self.failure) is not FailureData:
            raise _error("invalid_failure", "entry.failure", "must be FailureData")
        if status is BatchEntryStatus.RESOLUTION_FAILED:
            if self.failure is None or self.failure.origin is not FailureOrigin.PLUGIN:
                raise _error(
                    "missing_failure",
                    "entry.failure",
                    "resolution_failed requires a plugin failure",
                )
        elif status is BatchEntryStatus.PACKAGE_REJECTED:
            if self.failure is None or self.failure.stage not in _PRE_CHILD_STAGES:
                raise _error(
                    "invalid_failure",
                    "entry.failure",
                    "package_rejected requires a pre-child system failure",
                )
        elif self.failure is not None:
            raise _error(
                "unexpected_failure", "entry.failure", f"{status.value} cannot contain a failure"
            )
        if status is BatchEntryStatus.PACKAGE_READY and self.child_id is None:
            raise _error("missing_child", "entry.child_id", "package_ready requires a child id")
        if status is not BatchEntryStatus.PACKAGE_READY and self.child_id is not None:
            raise _error(
                "unexpected_child", "entry.child_id", f"{status.value} cannot contain a child id"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "entry_id": self.entry_id,
            "parent_id": self.parent_id,
            "plugin_id": self.plugin_id,
            "site_id": self.site_id,
            "candidate_id": self.candidate_id,
            "source_operation_id": self.source_operation_id,
            "source_url": self.source_url,
            "title": self.title,
            "strategy": self.strategy.value,
            "status": self.status.value,
            "child_id": self.child_id,
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }


@dataclass(frozen=True)
class DownloadChild:
    child_id: str
    parent_id: str
    entry_id: str
    package: DownloadPackage
    status: DownloadChildStatus = DownloadChildStatus.QUEUED
    failure: FailureData | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "child_id", _require_text(self.child_id, "child.child_id", maximum=128)
        )
        object.__setattr__(
            self, "parent_id", _require_text(self.parent_id, "child.parent_id", maximum=128)
        )
        object.__setattr__(
            self, "entry_id", _require_text(self.entry_id, "child.entry_id", maximum=128)
        )
        if type(self.package) is not DownloadPackage:
            raise _error(
                "incomplete_package", "child.package", "must be a complete DownloadPackage"
            )
        status = _require_enum(self.status, DownloadChildStatus, "child.status")
        if self.failure is not None:
            if type(self.failure) is not FailureData or self.failure.stage not in _CHILD_STAGES:
                raise _error(
                    "invalid_failure", "child.failure", "must be a child-stage system failure"
                )
        if status in {DownloadChildStatus.FAILED, DownloadChildStatus.PARTIALLY_COMPLETED}:
            if self.failure is None:
                raise _error(
                    "missing_failure", "child.failure", f"{status.value} requires a failure"
                )
        elif self.failure is not None:
            raise _error(
                "unexpected_failure", "child.failure", f"{status.value} cannot contain a failure"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "child_id": self.child_id,
            "parent_id": self.parent_id,
            "entry_id": self.entry_id,
            "package": self.package.to_dict(),
            "status": self.status.value,
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }


@dataclass(frozen=True)
class BatchCounts:
    unresolved: int = 0
    resolving: int = 0
    rejected: int = 0
    queued: int = 0
    downloading: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(
                self,
                name,
                _require_non_negative_int(getattr(self, name), f"batch_counts.{name}"),
            )

    @property
    def total(self) -> int:
        return sum(getattr(self, name) for name in self.__dataclass_fields__)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            **{name: getattr(self, name) for name in self.__dataclass_fields__},
            "total": self.total,
        }


@dataclass(frozen=True)
class BatchSnapshot:
    parent: BatchParent
    counts: BatchCounts
    execution_status: BatchExecutionStatus

    def __post_init__(self) -> None:
        if type(self.parent) is not BatchParent:
            raise _error("invalid_parent", "batch_snapshot.parent", "must be a BatchParent")
        if type(self.counts) is not BatchCounts:
            raise _error("invalid_counts", "batch_snapshot.counts", "must be BatchCounts")
        _require_enum(
            self.execution_status,
            BatchExecutionStatus,
            "batch_snapshot.execution_status",
        )
        if len(self.parent.entry_ids) != self.counts.total:
            raise _error(
                "batch_count_mismatch",
                "batch_snapshot.counts",
                "total must equal the number of parent entries",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "parent": self.parent.to_dict(),
            "counts": self.counts.to_dict(),
            "execution_status": self.execution_status.value,
        }


@dataclass(frozen=True)
class BatchSummary:
    """Lightweight aggregate projection for a Batch root row.

    Unlike :class:`BatchSnapshot`, this model deliberately carries no entry
    IDs, child packages, assets, attempts, or output paths.  It is safe to
    build for a large Batch while the user is still on the Tasks root page.
    """

    parent_id: str
    title: str | None
    first_entry_id: str | None
    site_id: str | None
    source_url: str | None
    counts: BatchCounts
    execution_status: BatchExecutionStatus
    completed_files: int = 0
    total_files: int = 0
    completed_bytes: int = 0
    total_bytes: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parent_id", _require_text(self.parent_id, "batch_summary.parent_id"))
        object.__setattr__(self, "title", _optional_text(self.title, "batch_summary.title"))
        object.__setattr__(
            self,
            "first_entry_id",
            _optional_text(self.first_entry_id, "batch_summary.first_entry_id"),
        )
        object.__setattr__(self, "site_id", _optional_text(self.site_id, "batch_summary.site_id"))
        if self.source_url is not None:
            object.__setattr__(
                self,
                "source_url",
                _require_url(self.source_url, "batch_summary.source_url"),
            )
        if type(self.counts) is not BatchCounts:
            raise _error("invalid_counts", "batch_summary.counts", "must be BatchCounts")
        _require_enum(
            self.execution_status,
            BatchExecutionStatus,
            "batch_summary.execution_status",
        )
        object.__setattr__(
            self,
            "completed_files",
            _require_non_negative_int(self.completed_files, "batch_summary.completed_files"),
        )
        object.__setattr__(
            self,
            "total_files",
            _require_non_negative_int(self.total_files, "batch_summary.total_files"),
        )
        if self.completed_files > self.total_files:
            object.__setattr__(self, "completed_files", self.total_files)
        object.__setattr__(
            self,
            "completed_bytes",
            _require_non_negative_int(self.completed_bytes, "batch_summary.completed_bytes"),
        )
        if self.total_bytes is not None:
            object.__setattr__(
                self,
                "total_bytes",
                _require_non_negative_int(self.total_bytes, "batch_summary.total_bytes"),
            )
        if self.total_bytes is not None:
            object.__setattr__(
                self,
                "completed_bytes",
                min(self.completed_bytes, self.total_bytes),
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "parent_id": self.parent_id,
            "title": self.title,
            "first_entry_id": self.first_entry_id,
            "site_id": self.site_id,
            "source_url": self.source_url,
            "counts": self.counts.to_dict(),
            "execution_status": self.execution_status.value,
            "completed_files": self.completed_files,
            "total_files": self.total_files,
            "completed_bytes": self.completed_bytes,
            "total_bytes": self.total_bytes,
        }


def derive_batch_snapshot(
    parent: BatchParent,
    entries: tuple[BatchEntry, ...],
    children: tuple[DownloadChild, ...],
) -> BatchSnapshot:
    if type(parent) is not BatchParent:
        raise _error("invalid_parent", "batch.parent", "must be a BatchParent")
    if any(type(entry) is not BatchEntry for entry in entries):
        raise _error("invalid_entry", "batch.entries", "must contain BatchEntry values")
    if any(type(child) is not DownloadChild for child in children):
        raise _error("invalid_child", "batch.children", "must contain DownloadChild values")

    entries_by_id = {entry.entry_id: entry for entry in entries}
    if len(entries_by_id) != len(entries) or set(entries_by_id) != set(parent.entry_ids):
        raise _error(
            "entry_set_mismatch",
            "batch.entries",
            "must contain each parent entry exactly once",
        )
    if any(entry.parent_id != parent.parent_id for entry in entries):
        raise _error("parent_mismatch", "batch.entries", "entries must belong to the parent")

    children_by_entry = {child.entry_id: child for child in children}
    if len(children_by_entry) != len(children):
        raise _error("duplicate_child", "batch.children", "entries may have at most one child")
    for child in children:
        entry = entries_by_id.get(child.entry_id)
        if entry is None or child.parent_id != parent.parent_id:
            raise _error("parent_mismatch", "batch.children", "children must belong to the parent")
        if entry.child_id != child.child_id:
            raise _error("child_mismatch", "batch.children", "child ids must match their entries")

    counts = {name: 0 for name in BatchCounts.__dataclass_fields__}
    for entry in entries:
        if entry.status is BatchEntryStatus.PENDING_RESOLUTION:
            counts["unresolved"] += 1
        elif entry.status is BatchEntryStatus.RESOLVING:
            counts["resolving"] += 1
        elif entry.status in {
            BatchEntryStatus.RESOLUTION_FAILED,
            BatchEntryStatus.PACKAGE_REJECTED,
        }:
            counts["rejected"] += 1
        elif entry.status is BatchEntryStatus.DUPLICATE_SKIPPED:
            counts["skipped"] += 1
        elif entry.status is BatchEntryStatus.CANCELLED:
            counts["cancelled"] += 1
        else:
            child = children_by_entry.get(entry.entry_id)
            if child is None:
                raise _error(
                    "missing_child", "batch.children", "package_ready entries require a child"
                )
            child_category = {
                DownloadChildStatus.QUEUED: "queued",
                DownloadChildStatus.DOWNLOADING: "downloading",
                DownloadChildStatus.COMPLETED: "completed",
                DownloadChildStatus.PARTIALLY_COMPLETED: "failed",
                DownloadChildStatus.FAILED: "failed",
                DownloadChildStatus.CANCELLED: "cancelled",
            }[child.status]
            counts[child_category] += 1

    result_counts = BatchCounts(**counts)
    status = derive_batch_execution_status(result_counts)
    return BatchSnapshot(parent=parent, counts=result_counts, execution_status=status)


def derive_batch_execution_status(counts: BatchCounts) -> BatchExecutionStatus:
    """Derive a Batch status from aggregate counts without materialising rows."""

    if type(counts) is not BatchCounts:
        raise _error("invalid_counts", "batch.counts", "must be BatchCounts")
    successful = counts.completed + counts.skipped
    active = counts.resolving + counts.queued + counts.downloading
    problems = counts.rejected + counts.failed + counts.cancelled
    if successful == counts.total:
        return BatchExecutionStatus.COMPLETED
    if active:
        return BatchExecutionStatus.RUNNING
    if counts.unresolved:
        return BatchExecutionStatus.PENDING
    if successful and problems:
        return BatchExecutionStatus.PARTIALLY_COMPLETED
    if counts.cancelled == counts.total:
        return BatchExecutionStatus.CANCELLED
    if problems:
        return BatchExecutionStatus.FAILED
    return BatchExecutionStatus.PENDING


@dataclass(frozen=True)
class AssetRecord:
    asset: DownloadAsset
    status: AssetStatus = AssetStatus.QUEUED
    attempt_count: int = 0
    failure: FailureData | None = None
    partial_path: str | None = None
    final_path: str | None = None
    content_sha256: str | None = None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.asset) is not DownloadAsset:
            raise _error("invalid_asset", "asset_record.asset", "must be a DownloadAsset")
        status = _require_enum(self.status, AssetStatus, "asset_record.status")
        object.__setattr__(
            self,
            "attempt_count",
            _require_non_negative_int(self.attempt_count, "asset_record.attempt_count"),
        )
        if self.failure is not None:
            if type(self.failure) is not FailureData or self.failure.stage not in _ASSET_STAGES:
                raise _error(
                    "invalid_failure",
                    "asset_record.failure",
                    "must be an asset-stage system failure",
                )
        if status is AssetStatus.FAILED and self.failure is None:
            raise _error("missing_failure", "asset_record.failure", "failed requires a failure")
        if status is not AssetStatus.FAILED and self.failure is not None:
            raise _error(
                "unexpected_failure",
                "asset_record.failure",
                f"{status.value} cannot contain a failure",
            )
        object.__setattr__(
            self,
            "partial_path",
            _optional_text(self.partial_path, "asset_record.partial_path", maximum=32767),
        )
        object.__setattr__(
            self,
            "final_path",
            _optional_text(self.final_path, "asset_record.final_path", maximum=32767),
        )
        if self.content_sha256 is not None:
            object.__setattr__(
                self,
                "content_sha256",
                _require_hex_digest(self.content_sha256, "asset_record.content_sha256"),
            )
        object.__setattr__(self, "metadata", _freeze_json_object(self.metadata, "asset_record.metadata"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "asset": self.asset.to_dict(),
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "failure": self.failure.to_dict() if self.failure is not None else None,
            "partial_path": self.partial_path,
            "final_path": self.final_path,
            "content_sha256": self.content_sha256,
            "metadata": _thaw_json(self.metadata),
        }


@dataclass(frozen=True)
class RetryPlan:
    plan_id: str
    start_at: FailureStage
    scope: RetryScope
    target_ids: tuple[str, ...]
    reason: str
    package_version: int | None = None
    automatic_refresh_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "plan_id", _require_text(self.plan_id, "retry_plan.plan_id", maximum=128)
        )
        start_at = _require_enum(self.start_at, FailureStage, "retry_plan.start_at")
        scope = _require_enum(self.scope, RetryScope, "retry_plan.scope")
        object.__setattr__(
            self, "target_ids", _require_unique_ids(self.target_ids, "retry_plan.target_ids")
        )
        object.__setattr__(self, "reason", _require_text(self.reason, "retry_plan.reason"))
        if self.package_version is not None:
            object.__setattr__(
                self,
                "package_version",
                _require_positive_int(self.package_version, "retry_plan.package_version"),
            )
        object.__setattr__(
            self,
            "automatic_refresh_count",
            _require_non_negative_int(
                self.automatic_refresh_count, "retry_plan.automatic_refresh_count"
            ),
        )
        if self.automatic_refresh_count > 1:
            raise _error(
                "refresh_limit_exceeded",
                "retry_plan.automatic_refresh_count",
                "must not exceed one automatic refresh",
            )
        if scope is RetryScope.ENTRY and start_at not in _PLUGIN_STAGES | _PRE_CHILD_STAGES:
            raise _error(
                "retry_scope_mismatch",
                "retry_plan.start_at",
                "entry retries must start before child creation",
            )
        if scope is RetryScope.ASSET and start_at not in _ASSET_STAGES:
            raise _error(
                "retry_scope_mismatch",
                "retry_plan.start_at",
                "asset retries must start at fetch, verify, or write",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "plan_id": self.plan_id,
            "start_at": self.start_at.value,
            "scope": self.scope.value,
            "target_ids": list(self.target_ids),
            "reason": self.reason,
            "package_version": self.package_version,
            "automatic_refresh_count": self.automatic_refresh_count,
        }


@dataclass(frozen=True)
class CheckpointData:
    checkpoint: Checkpoint
    target_id: str
    details: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_enum(self.checkpoint, Checkpoint, "checkpoint.checkpoint")
        object.__setattr__(
            self, "target_id", _require_text(self.target_id, "checkpoint.target_id", maximum=128)
        )
        object.__setattr__(self, "details", _freeze_json_object(self.details, "checkpoint.details"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "checkpoint": self.checkpoint.value,
            "target_id": self.target_id,
            "details": _thaw_json(self.details),
        }


@dataclass(frozen=True)
class AttemptData:
    attempt_id: str
    target_id: str
    number: int
    checkpoints: tuple[CheckpointData, ...] = ()
    retry_plan: RetryPlan | None = None
    failure: FailureData | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attempt_id", _require_text(self.attempt_id, "attempt.attempt_id", maximum=128)
        )
        object.__setattr__(
            self, "target_id", _require_text(self.target_id, "attempt.target_id", maximum=128)
        )
        object.__setattr__(self, "number", _require_positive_int(self.number, "attempt.number"))
        if not isinstance(self.checkpoints, (tuple, list)):
            raise _error("invalid_sequence", "attempt.checkpoints", "must be a sequence")
        checkpoints = tuple(self.checkpoints)
        if any(type(item) is not CheckpointData for item in checkpoints):
            raise _error("invalid_checkpoint", "attempt.checkpoints", "must contain CheckpointData")
        object.__setattr__(self, "checkpoints", checkpoints)
        if self.retry_plan is not None and type(self.retry_plan) is not RetryPlan:
            raise _error("invalid_retry_plan", "attempt.retry_plan", "must be a RetryPlan")
        if self.failure is not None and type(self.failure) is not FailureData:
            raise _error("invalid_failure", "attempt.failure", "must be FailureData")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "attempt_id": self.attempt_id,
            "target_id": self.target_id,
            "number": self.number,
            "checkpoints": [checkpoint.to_dict() for checkpoint in self.checkpoints],
            "retry_plan": self.retry_plan.to_dict() if self.retry_plan is not None else None,
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }
