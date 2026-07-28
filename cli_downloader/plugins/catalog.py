from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


CATALOG_REPOSITORY = "https://github.com/RyuuMeow/CLI-Downloader-Plugin-Marketplace.git"
CATALOG_SCHEMA_VERSION = 1
CATALOG_REFRESH_SECONDS = 6 * 60 * 60
CATALOG_URLS = {
    "official": (
        "https://raw.githubusercontent.com/"
        "RyuuMeow/CLI-Downloader-Plugin-Marketplace/main/official.json"
    ),
    "community": (
        "https://raw.githubusercontent.com/"
        "RyuuMeow/CLI-Downloader-Plugin-Marketplace/main/community.json"
    ),
}

_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SOURCES = ("official", "community")


class PluginCatalogError(ValueError):
    """Raised when a remote or cached catalog violates the catalog contract."""


@dataclass(frozen=True)
class PluginCatalogEntry:
    plugin_id: str
    repository_url: str
    source: str
    author: str | None = None
    channel: str = "stable"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PluginCatalog:
    entries: tuple[PluginCatalogEntry, ...]
    status: str
    refreshed_at: float | None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class _CatalogDocument:
    source: str
    entries: tuple[PluginCatalogEntry, ...]


@dataclass(frozen=True)
class _CachedDocument:
    document: _CatalogDocument
    fetched_at: float
    etag: str | None = None
    last_modified: str | None = None


UrlOpen = Callable[..., object]


class PluginCatalogService:
    """Load Official and Community discovery data without executing plugin code."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        refresh_seconds: int = CATALOG_REFRESH_SECONDS,
        urlopen: UrlOpen = urllib.request.urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cache_dir = cache_dir / "plugin-catalog"
        self._refresh_seconds = refresh_seconds
        self._urlopen = urlopen
        self._clock = clock

    def load(self, *, refresh: bool = True, force: bool = False) -> PluginCatalog:
        now = self._clock()
        documents: dict[str, _CachedDocument] = {}
        diagnostics: list[str] = []
        used_remote = False
        used_cache = False

        for source in _SOURCES:
            cached = self._read_cache(source)
            selected = cached
            fresh = cached is not None and now - cached.fetched_at < self._refresh_seconds
            if refresh and (force or not fresh):
                try:
                    remote = self._fetch(source, cached, now)
                    selected = remote
                    used_remote = True
                    if remote is not cached:
                        self._write_cache(remote)
                except Exception as exc:
                    diagnostics.append(f"{source}: {exc}")

            if selected is None:
                # Remote-only catalogs intentionally have no bundled snapshot.
                # Keep the application usable when the first refresh is offline
                # by representing the unavailable source as an empty document.
                selected = _CachedDocument(_CatalogDocument(source, ()), now)
            elif not used_remote or selected is cached:
                used_cache = True
            documents[source] = selected

        entries = merge_catalog_entries(
            documents["official"].document.entries,
            documents["community"].document.entries,
        )
        refreshed_at = max(item.fetched_at for item in documents.values())
        if diagnostics:
            status = "stale" if used_cache else "offline"
        elif used_remote:
            status = "current"
        elif used_cache:
            status = "cached"
        else:
            status = "empty"
        return PluginCatalog(entries, status, refreshed_at, tuple(diagnostics))

    def _fetch(
        self,
        source: str,
        cached: _CachedDocument | None,
        now: float,
    ) -> _CachedDocument:
        headers = {"Accept": "application/json", "User-Agent": "CLI-Downloader"}
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached is not None and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        request = urllib.request.Request(CATALOG_URLS[source], headers=headers)
        try:
            response = self._urlopen(request, timeout=15)
            with response as stream:
                payload = json.loads(stream.read().decode("utf-8"))
                document = parse_catalog_document(payload, expected_source=source)
                response_headers = getattr(stream, "headers", {})
                return _CachedDocument(
                    document=document,
                    fetched_at=now,
                    etag=response_headers.get("ETag"),
                    last_modified=response_headers.get("Last-Modified"),
                )
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached is not None:
                return _CachedDocument(
                    cached.document,
                    now,
                    cached.etag,
                    cached.last_modified,
                )
            raise

    def _cache_path(self, source: str) -> Path:
        return self._cache_dir / f"{source}.json"

    def _read_cache(self, source: str) -> _CachedDocument | None:
        path = self._cache_path(source)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            document = parse_catalog_document(payload["catalog"], expected_source=source)
            return _CachedDocument(
                document=document,
                fetched_at=float(payload["fetched_at"]),
                etag=_optional_string(payload.get("etag")),
                last_modified=_optional_string(payload.get("last_modified")),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_cache(self, cached: _CachedDocument) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_path(cached.document.source)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "fetched_at": cached.fetched_at,
                    "etag": cached.etag,
                    "last_modified": cached.last_modified,
                    "catalog": _document_payload(cached.document),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)


def parse_catalog_document(payload: object, *, expected_source: str) -> _CatalogDocument:
    if not isinstance(payload, dict):
        raise PluginCatalogError("catalog root must be an object")
    if payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise PluginCatalogError("unsupported catalog schema_version")
    source = payload.get("source")
    if source != expected_source or source not in _SOURCES:
        raise PluginCatalogError(f"catalog source must be {expected_source}")
    raw_entries = payload.get("plugins")
    if not isinstance(raw_entries, list):
        raise PluginCatalogError("catalog plugins must be an array")

    entries: list[PluginCatalogEntry] = []
    identifiers: set[str] = set()
    repositories: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise PluginCatalogError(f"plugins[{index}] must be an object")
        plugin_id = raw.get("id")
        repository_url = raw.get("repository")
        if not isinstance(plugin_id, str) or _PLUGIN_ID_RE.fullmatch(plugin_id) is None:
            raise PluginCatalogError(f"plugins[{index}].id is invalid")
        if not isinstance(repository_url, str) or not _is_https_url(repository_url):
            raise PluginCatalogError(f"plugins[{index}].repository must be an HTTPS URL")
        repository_key = _repository_key(repository_url)
        if plugin_id in identifiers:
            raise PluginCatalogError(f"duplicate plugin id: {plugin_id}")
        if repository_key in repositories:
            raise PluginCatalogError(f"duplicate plugin repository: {repository_url}")
        identifiers.add(plugin_id)
        repositories.add(repository_key)

        tags = raw.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise PluginCatalogError(f"plugins[{index}].tags must be strings")
        channel = raw.get("channel", "stable")
        if not isinstance(channel, str) or not channel.strip():
            raise PluginCatalogError(f"plugins[{index}].channel is invalid")
        entries.append(
            PluginCatalogEntry(
                plugin_id=plugin_id,
                repository_url=repository_url,
                source=source,
                author=_optional_string(raw.get("author")),
                channel=channel,
                tags=tuple(tags),
            )
        )
    return _CatalogDocument(source, tuple(entries))


def merge_catalog_entries(
    official: tuple[PluginCatalogEntry, ...],
    community: tuple[PluginCatalogEntry, ...],
) -> tuple[PluginCatalogEntry, ...]:
    """Return Official plus the Community difference, preserving trust precedence."""
    official_ids = {entry.plugin_id for entry in official}
    official_repositories = {_repository_key(entry.repository_url) for entry in official}
    community_difference = (
        entry
        for entry in community
        if entry.plugin_id not in official_ids
        and _repository_key(entry.repository_url) not in official_repositories
    )
    return tuple(sorted((*official, *community_difference), key=lambda entry: entry.plugin_id))


def _document_payload(document: _CatalogDocument) -> dict[str, object]:
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "source": document.source,
        "plugins": [
            {
                "id": entry.plugin_id,
                "repository": entry.repository_url,
                "author": entry.author,
                "channel": entry.channel,
                "tags": list(entry.tags),
            }
            for entry in document.entries
        ],
    }


def _is_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username


def _repository_key(value: str) -> str:
    return value.removesuffix("/").removesuffix(".git").lower()


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
