from __future__ import annotations

import asyncio
from typing import Protocol, AsyncIterator
from urllib.request import Request, urlopen

from cli_downloader.core.models import Asset


class AssetFetcher(Protocol):
    async def fetch(self, asset: Asset) -> bytes: ...


class StreamingAssetFetcher(Protocol):
    async def fetch_stream(self, asset: Asset) -> AsyncIterator[tuple[bytes, int | None]]:
        """Yields (chunk, total_size_or_none)."""
        ...
        yield b"", None  # Make type checker happy


class UrlAssetFetcher:
    async def fetch(self, asset: Asset) -> bytes:
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
