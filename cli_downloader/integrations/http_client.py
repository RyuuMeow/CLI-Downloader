"""HTTP attempts controlled by the Owner's site request coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from threading import Event
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from cli_downloader.application.site_request_coordinator import (
    SiteRequestCoordinator,
    TransportDeferred,
    retry_after_seconds,
)
from cli_downloader.core.http import (
    HttpRequest,
    HttpResponse,
    HttpStreamInterrupted,
    active_http_stream,
)


class HttpStatusError(Exception):
    def __init__(self, status: int, headers: dict[str, str]) -> None:
        # No URL or response body: these frequently contain signed credentials.
        super().__init__(f"HTTP request failed with status {status}")
        self.code = status
        self.headers = headers


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _open(request: HttpRequest):
    handlers = [_NoRedirect()]
    if request.proxies is not None:
        handlers.append(ProxyHandler(dict(request.proxies)))
    if request.tls_context is not None:
        handlers.append(HTTPSHandler(context=request.tls_context))
    if request.cookie_jar is not None:
        handlers.append(HTTPCookieProcessor(request.cookie_jar))
    opener = build_opener(*handlers)
    native = Request(
        request.url, data=request.body, headers=dict(request.headers), method=request.method
    )
    try:
        return opener.open(native, timeout=request.timeout)
    except HTTPError as response:
        return response


async def _blocking(
    call: Callable, *args, on_cancel: Callable | None = None, cancelled: Event | None = None
):
    """A canceled await is not evidence that a blocking socket operation stopped."""
    pending = asyncio.create_task(asyncio.to_thread(call, *args))
    canceled = False
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            canceled = True
            if cancelled is not None:
                cancelled.set()
            if pending.cancelled():
                raise
            continue
        except BaseException:
            if canceled:
                raise asyncio.CancelledError from None
            raise
    if canceled:
        if on_cancel is not None:
            await _blocking(on_cancel, result)
        raise asyncio.CancelledError
    return result


class _Stream:
    def __init__(self, response, permit) -> None:
        self._response = response
        self._permit = permit
        self._closed = False
        self._interrupted = False
        self.status = response.status if response.status is not None else 200
        self.headers = {str(key).lower(): str(value) for key, value in response.headers.items()}

    async def read(self, size: int = -1) -> bytes:
        if self._interrupted:
            raise HttpStreamInterrupted
        if self._closed:
            return b""
        try:
            content = await _blocking(self._response.read, size)
        except BaseException:
            await self.close()
            raise
        if not content or size < 0:
            await self.close()
        return content

    async def close(self) -> None:
        if not self._closed:
            # Do not return a permit until close has actually finished.
            def close_response():
                self._response.close()
                self._closed = True

            try:
                await _blocking(close_response)
            finally:
                if self._closed:
                    self._permit.release()

    async def interrupt(self) -> None:
        if not self._closed:
            self._interrupted = True
            await self.close()


class ManagedHttpClient:
    def __init__(
        self, site_id: str, coordinator: SiteRequestCoordinator, *, opener: Callable = _open
    ) -> None:
        self.site_id = site_id
        self.coordinator = coordinator
        self._open = opener

    async def request(self, request: HttpRequest) -> HttpResponse:
        async with self.stream(request) as stream:
            body = await stream.read()
            return HttpResponse(stream.status, stream.headers, body)

    @asynccontextmanager
    async def stream(self, request: HttpRequest):
        current = request
        redirects = 0
        attempt = 0
        policy = self.coordinator.policy(self.site_id)
        while True:
            if urlsplit(current.url).scheme.lower() not in ("http", "https", "file"):
                raise ValueError("unsupported HTTP transport URL scheme")
            permit = await self.coordinator.acquire(self.site_id)
            stream = None
            retry = False
            try:
                cancelled = Event()

                def open_attempt():
                    self.coordinator.start_transport(permit, cancelled)
                    return self._open(current)

                response = await _blocking(
                    open_attempt, on_cancel=lambda response: response.close(), cancelled=cancelled
                )
                stream = _Stream(response, permit)
                if stream.status in (301, 302, 303, 307, 308) and stream.headers.get("location"):
                    after = retry_after_seconds(
                        HttpStatusError(
                            stream.status, {k.title(): v for k, v in stream.headers.items()}
                        )
                    )
                    if after is not None:
                        self.coordinator.defer(self.site_id, after, status=stream.status)
                    if redirects >= 10:
                        raise RuntimeError("HTTP redirect limit exceeded")
                    next_url = urljoin(current.url, stream.headers["location"])
                    if urlsplit(next_url).scheme.lower() not in ("http", "https"):
                        raise ValueError("unsupported redirect URL scheme")
                    headers = dict(current.headers)
                    if _origin(current.url) != _origin(next_url):
                        headers = {
                            key: value
                            for key, value in headers.items()
                            if key.lower()
                            not in (
                                "authorization",
                                "cookie",
                                "proxy-authorization",
                                "x-api-key",
                                "host",
                            )
                        }
                    method, body = current.method, current.body
                    if (
                        stream.status == 303
                        and method != "HEAD"
                        or stream.status in (301, 302)
                        and method == "POST"
                    ):
                        method, body = "GET", None
                        headers = {
                            key: value
                            for key, value in headers.items()
                            if key.lower() not in ("content-length", "content-type")
                        }
                    current = replace(
                        current, url=next_url, method=method, body=body, headers=headers
                    )
                    redirects += 1
                    continue
                if stream.status >= 400:
                    attempt += 1
                    error = HttpStatusError(
                        stream.status, {key.title(): value for key, value in stream.headers.items()}
                    )
                    retry_at = self.coordinator.observe_error(self.site_id, error, attempt)
                    error.request_attempt_count = attempt
                    error.next_retry_at = retry_at
                    safe = (
                        current.retry_safe
                        if current.retry_safe is not None
                        else current.method in ("GET", "HEAD", "OPTIONS")
                    )
                    if retry_at is None or not safe or attempt >= policy.max_attempts:
                        raise error
                    retry = True
                else:
                    with active_http_stream(stream):
                        yield stream
                    return
            except TransportDeferred:
                retry = True
            finally:
                if stream is not None:
                    await stream.close()
                else:
                    permit.release()
            if retry:
                continue


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower(),
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )
