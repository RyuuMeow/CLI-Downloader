from __future__ import annotations

import asyncio
from typing import Protocol, AsyncIterator
from urllib.request import Request, urlopen

from cli_downloader.core.models import Asset
from cli_downloader.core.http import HttpRequest, PluginHttpClient, PluginServices


class AssetFetcher(Protocol):
    async def fetch(self, asset: Asset) -> bytes: ...


class StreamingAssetFetcher(Protocol):
    async def fetch_stream(self, asset: Asset) -> AsyncIterator[tuple[bytes, int | None]]:
        """Yields (chunk, total_size_or_none)."""
        ...
        yield b"", None  # Make type checker happy


class UrlAssetFetcher:
    def __init__(self) -> None:
        self._http: PluginHttpClient | None = None

    @property
    def managed_http_version(self) -> int | None:
        return 1 if self._http is not None else None

    def bind_services(self, services: PluginServices) -> None:
        self._http = services.http

    async def fetch(self, asset: Asset) -> bytes:
        if self._http is not None:
            response = await self._http.request(_http_request(asset))
            return response.body

        def fetch_url() -> bytes:
            headers = asset.metadata.get("headers", {})
            request = Request(
                asset.url,
                headers=dict(headers) if isinstance(headers, dict) else {},
            )
            with urlopen(request) as response:
                return response.read()

        return await asyncio.to_thread(fetch_url)

    async def fetch_stream(self, asset: Asset) -> AsyncIterator[tuple[bytes, int | None]]:
        if self._http is not None:
            async with self._http.stream(_http_request(asset)) as response:
                raw_size = response.headers.get("content-length")
                total = int(raw_size) if raw_size and raw_size.isdigit() else None
                while chunk := await response.read(262144):
                    yield chunk, total
            return

        loop = asyncio.get_running_loop()
        headers = asset.metadata.get("headers", {})
        request = Request(
            asset.url,
            headers=dict(headers) if isinstance(headers, dict) else {},
        )

        def _open_req():
            return urlopen(request)

        response = await loop.run_in_executor(None, _open_req)

        try:
            total_size_str = response.headers.get("Content-Length")
            total_size = (
                int(total_size_str) if total_size_str and total_size_str.isdigit() else None
            )

            while True:
                chunk = await loop.run_in_executor(None, response.read, 262144)  # 256KB chunks
                if not chunk:
                    break
                yield chunk, total_size
        finally:
            await loop.run_in_executor(None, response.close)


def _http_request(asset: Asset) -> HttpRequest:
    headers = asset.metadata.get("headers", {})
    return HttpRequest(asset.url, headers=dict(headers) if isinstance(headers, dict) else {})
