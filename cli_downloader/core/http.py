"""Versioned HTTP services available to site plugins.

Credentials belong to each request, never to the shared site admission budget.
Plugins declaring managed_http_version=1 must use these services for all site I/O.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from ssl import SSLContext
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = 30.0
    retry_safe: bool | None = None
    proxies: Mapping[str, str] | None = None
    tls_context: SSLContext | None = field(default=None, repr=False)
    cookie_jar: CookieJar | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "method", self.method.upper())
        if self.proxies is not None:
            object.__setattr__(self, "proxies", MappingProxyType(dict(self.proxies)))
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.timeout > 300:
            raise ValueError("HTTP timeout must be between 0 and 300 seconds")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding)

    def json(self) -> object:
        return json.loads(self.body)


class HttpStream(Protocol):
    status: int
    headers: Mapping[str, str]

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...

    async def interrupt(self) -> None: ...


class PluginHttpClient(Protocol):
    async def request(self, request: HttpRequest) -> HttpResponse: ...

    def stream(self, request: HttpRequest) -> AbstractAsyncContextManager[HttpStream]: ...


@dataclass(frozen=True)
class PluginServices:
    http: PluginHttpClient
    version: int = 1


class HttpStreamInterrupted(Exception):
    """The consumer paused a response; restart the unfinished asset after resume."""


@dataclass(frozen=True)
class _ActiveStream:
    task: asyncio.Task
    stream: HttpStream


_streams: ContextVar[tuple[_ActiveStream, ...]] = ContextVar("active_http_streams", default=())


@contextmanager
def active_http_stream(stream: HttpStream):
    task = asyncio.current_task()
    token = _streams.set((*_streams.get(), _ActiveStream(task, stream)))
    try:
        yield
    finally:
        _streams.reset(token)


async def interrupt_http_streams() -> None:
    """Close real responses before a control path acknowledges its safe boundary."""
    task = asyncio.current_task()
    for active in reversed(_streams.get()):
        if active.task is task:
            await active.stream.interrupt()


def has_managed_http(instance: object) -> bool:
    return getattr(instance, "managed_http_version", None) == 1


@asynccontextmanager
async def closing_stream(iterator):
    try:
        yield iterator
    finally:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()


async def restart_interrupted_download(operation):
    """Restart only a locally interrupted unfinished asset, never HTTP failures."""
    while True:
        try:
            return await operation()
        except Exception as error:
            cause = error
            seen = set()
            while not isinstance(cause, HttpStreamInterrupted) and id(cause) not in seen:
                seen.add(id(cause))
                if not isinstance(cause.__cause__, Exception):
                    raise error
                cause = cause.__cause__
            if not isinstance(cause, HttpStreamInterrupted):
                raise
