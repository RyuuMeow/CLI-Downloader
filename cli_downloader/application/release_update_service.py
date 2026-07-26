"""Check published application releases without coupling network work to the UI."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class ReleaseUpdateStatus(str, Enum):
    NOT_CHECKED = "not_checked"
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    CURRENT_NEWER = "current_newer"
    DEVELOPMENT_BUILD = "development_build"
    NO_RELEASES = "no_releases"
    FAILED = "failed"


@dataclass(frozen=True)
class ReleaseUpdateResult:
    status: ReleaseUpdateStatus
    current_version: str
    latest_version: str | None = None
    release_url: str | None = None
    error_code: str | None = None

    @classmethod
    def not_checked(cls, current_version: str) -> ReleaseUpdateResult:
        return cls(ReleaseUpdateStatus.NOT_CHECKED, current_version)


class _ReleaseResponse(Protocol):
    def read(self) -> bytes: ...

    def __enter__(self) -> _ReleaseResponse: ...

    def __exit__(self, *args: object) -> object: ...


ReleaseFetcher = Callable[[Request, float], _ReleaseResponse]

_VERSION_PATTERN = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _default_fetcher(request: Request, timeout: float) -> _ReleaseResponse:
    return urlopen(request, timeout=timeout)  # type: ignore[return-value]


def _release_version(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _latest_release_api(repository_url: str) -> str | None:
    parsed = urlsplit(repository_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
        "github.com",
        "www.github.com",
    }:
        return None
    parts = tuple(part for part in parsed.path.strip("/").split("/") if part)
    if len(parts) != 2:
        return None
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not owner or not repository:
        return None
    slug = f"{quote(owner, safe='')}/{quote(repository, safe='')}"
    return f"https://api.github.com/repos/{slug}/releases/latest"


class ReleaseUpdateService:
    def __init__(
        self,
        *,
        repository_url: str | None,
        current_version: str,
        fetcher: ReleaseFetcher = _default_fetcher,
        timeout: float = 5.0,
    ) -> None:
        self._repository_url = repository_url
        self._current_version = current_version
        self._fetcher = fetcher
        self._timeout = timeout

    def check(self) -> ReleaseUpdateResult:
        api_url = (
            _latest_release_api(self._repository_url) if self._repository_url is not None else None
        )
        if api_url is None:
            return self._failed("unsupported_repository")

        request = Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"CLI-Downloader/{self._current_version}",
            },
        )
        try:
            with self._fetcher(request, self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 404:
                return ReleaseUpdateResult(
                    ReleaseUpdateStatus.NO_RELEASES,
                    self._current_version,
                )
            if error.code in {403, 429}:
                return self._failed("rate_limited")
            return self._failed("request_failed")
        except (TimeoutError, URLError, OSError):
            return self._failed("offline")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._failed("invalid_response")
        except Exception:
            return self._failed("request_failed")

        return self._compare_payload(payload)

    def _compare_payload(self, payload: Any) -> ReleaseUpdateResult:
        if not isinstance(payload, Mapping):
            return self._failed("invalid_response")
        tag_name = payload.get("tag_name")
        release_url = payload.get("html_url")
        if not isinstance(tag_name, str) or not isinstance(release_url, str):
            return self._failed("invalid_response")
        latest = _release_version(tag_name)
        if latest is None or not release_url.startswith("https://github.com/"):
            return self._failed("invalid_response")

        latest_version = ".".join(str(part) for part in latest)
        current = _release_version(self._current_version)
        if current is None:
            return ReleaseUpdateResult(
                ReleaseUpdateStatus.DEVELOPMENT_BUILD,
                self._current_version,
                latest_version,
                release_url,
            )
        if latest > current:
            status = ReleaseUpdateStatus.UPDATE_AVAILABLE
        elif latest < current:
            status = ReleaseUpdateStatus.CURRENT_NEWER
        else:
            status = ReleaseUpdateStatus.UP_TO_DATE
        return ReleaseUpdateResult(
            status,
            self._current_version,
            latest_version,
            release_url,
        )

    def _failed(self, error_code: str) -> ReleaseUpdateResult:
        return ReleaseUpdateResult(
            ReleaseUpdateStatus.FAILED,
            self._current_version,
            error_code=error_code,
        )


__all__ = [
    "ReleaseFetcher",
    "ReleaseUpdateResult",
    "ReleaseUpdateService",
    "ReleaseUpdateStatus",
]
