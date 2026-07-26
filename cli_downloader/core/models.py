from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AssetKind(StrEnum):
    FILE = "file"
    IMAGE = "image"
    VIDEO = "video"
    TORRENT = "torrent"


class DownloadStrategy(StrEnum):
    AUTO = "auto"
    DIRECT = "direct"
    TORRENT = "torrent"
    API = "api"


@dataclass(frozen=True)
class ResolveRequest:
    url: str
    site_hint: str | None = None
    profile: str | None = None
    strategy: DownloadStrategy = DownloadStrategy.AUTO


@dataclass(frozen=True)
class ResolvedItem:
    site_id: str
    source_url: str
    title: str
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Asset:
    url: str
    kind: AssetKind
    filename: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssetList:
    assets: tuple[Asset, ...]


@dataclass(frozen=True)
class DownloadStrategyPlan:
    strategy: DownloadStrategy
    reason: str
    assets: AssetList


@dataclass(frozen=True)
class EffectivePolicy:
    include_rules: tuple[str, ...] = ()
    exclude_rules: tuple[str, ...] = ()
    strategy: DownloadStrategy = DownloadStrategy.AUTO
    profile: str | None = None


@dataclass(frozen=True)
class TaskEnvelope:
    item: ResolvedItem
    policy: EffectivePolicy
    environment: dict[str, Any]
