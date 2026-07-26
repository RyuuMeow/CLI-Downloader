from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from cli_downloader.core.errors import DuplicateSiteAdapterError, PluginValidationError
from cli_downloader.sites.base import SiteAdapter


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def normalize_hostname(hostname: str) -> str:
    if not isinstance(hostname, str) or not hostname.strip():
        raise PluginValidationError("hostname must be a non-empty string")
    value = hostname.strip().rstrip(".")
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        pass
    try:
        value = value.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise PluginValidationError(f"invalid hostname: {hostname}") from error
    labels = value.split(".")
    if len(value) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise PluginValidationError(f"invalid hostname: {hostname}")
    return value


def hostname_from_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if hostname is None:
        return None
    try:
        return normalize_hostname(hostname)
    except PluginValidationError:
        return None


class SiteRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SiteAdapter] = {}

    @property
    def adapters(self) -> tuple[SiteAdapter, ...]:
        return tuple(self._adapters.values())

    def adapter_for_site_id(self, site_id: str) -> SiteAdapter | None:
        return self._adapters.get(site_id)

    def register(self, adapter: SiteAdapter) -> None:
        if adapter.site_id in self._adapters:
            raise DuplicateSiteAdapterError(f"duplicate site adapter: {adapter.site_id}")
        self._adapters[adapter.site_id] = adapter

    def adapter_for_url(self, url: str) -> SiteAdapter | None:
        for adapter in self._adapters.values():
            if adapter.can_handle_url(url):
                return adapter
        return None
