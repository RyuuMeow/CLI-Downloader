"""Operation dispatch and URL inspection helpers for the CLI entry point.

The application services own operation execution.  This module contains the
small amount of CLI policy needed to construct requests, apply configured
strategy defaults, and inspect manifest routes without coupling those helpers
to the command parser in :mod:`cli_downloader.cli.app`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from uuid import uuid4

from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.config.loader import LoadedConfig
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.operations import OperationResult
from cli_downloader.plugins.runtime import ApplicationRuntime
from cli_downloader.sites.manifest import ManifestRoute, SiteManifest
from cli_downloader.sites.registry import hostname_from_url


def dispatch_operation(
    dispatcher: OperationDispatcher,
    url: str,
    *,
    params: Mapping[str, object] | None = None,
    profile: str | None = None,
    confirmations: tuple[str, ...] = (),
) -> OperationResult:
    """Dispatch a URL operation with a fresh request id."""

    return asyncio.run(
        dispatcher.dispatch(
            url,
            request_id=uuid4().hex,
            params=params,
            profile=profile,
            confirmations=confirmations,
        )
    )


def with_default_strategy(
    runtime: ApplicationRuntime,
    url: str,
    params: Mapping[str, object],
    config: LoadedConfig,
) -> dict[str, object]:
    """Apply the global strategy only to operations that declare it."""

    merged = dict(params)
    if "strategy" in merged or not operation_supports_strategy(runtime, url):
        return merged
    merged["strategy"] = effective_strategy(config).value
    return merged


def effective_strategy(
    config: LoadedConfig,
    requested: str | None = None,
) -> DownloadStrategy:
    value = (
        requested
        if requested is not None
        else config.values.get("download.strategy", DownloadStrategy.AUTO.value)
    )
    return DownloadStrategy(str(value))


def operation_supports_strategy(runtime: ApplicationRuntime, url: str) -> bool:
    if url.startswith("plugin://"):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        plugin_id = parsed.hostname
        if plugin_id is None:
            return False
        entry = runtime.plugins_by_id.get(plugin_id)
        if entry is None:
            return False
        operation = entry.manifest.operations.get(parsed.path.lstrip("/"))
        return operation is not None and "strategy" in operation.params

    entry = runtime.entry_for_url(url)
    if entry is None:
        return False
    route = entry.instance.classify_url(url)
    return route is not None and "strategy" in route.parameter_schema


def dispatch_plugin_operation(
    dispatcher: OperationDispatcher,
    plugin_id: str,
    operation_id: str,
    *,
    params: Mapping[str, object] | None = None,
    profile: str | None = None,
    confirmations: tuple[str, ...] = (),
) -> OperationResult:
    """Dispatch a plugin operation with a fresh request id."""

    return asyncio.run(
        dispatcher.dispatch_plugin_operation(
            plugin_id,
            operation_id,
            request_id=uuid4().hex,
            params=params,
            profile=profile,
            confirmations=confirmations,
        )
    )


def redacted_headers(headers: Mapping[str, str]) -> dict[str, str]:
    sensitive_names = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
    return {
        str(key): "<redacted>" if str(key).lower() in sensitive_names else str(value)
        for key, value in headers.items()
    }


def route_url_through_manifests(
    url: str,
    *,
    runtime: ApplicationRuntime,
) -> tuple[SiteManifest | None, ManifestRoute | None]:
    """Return the manifest and route that can handle ``url``."""

    entry = runtime.entry_for_url(url)
    if entry is not None:
        manifests = (entry.manifest,)
    elif hostname_from_url(url) is not None:
        return None, None
    else:
        manifests = tuple(runtime.manifests_by_plugin_id.values())
    for manifest in manifests:
        route = manifest.route_url(url)
        if route is not None:
            return manifest, route
    return None, None


# Keep the private names available to callers that used the pre-extraction
# helpers while the CLI migrates to the focused module API.
_dispatch_operation = dispatch_operation
_with_default_strategy = with_default_strategy
_effective_strategy = effective_strategy
_operation_supports_strategy = operation_supports_strategy
_dispatch_plugin_operation = dispatch_plugin_operation
_redacted_headers = redacted_headers
_route_url_through_manifests = route_url_through_manifests
