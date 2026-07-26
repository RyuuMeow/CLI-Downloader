import json
from unittest import TestCase
from urllib.error import HTTPError, URLError

from cli_downloader.application.release_update_service import (
    ReleaseUpdateService,
    ReleaseUpdateStatus,
)


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _service(current: str, payload: object) -> ReleaseUpdateService:
    return ReleaseUpdateService(
        repository_url="https://github.com/RyuuMeow/CLI-Downloader",
        current_version=current,
        fetcher=lambda request, timeout: _Response(payload),
    )


def _release(version: str) -> dict[str, str]:
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/RyuuMeow/CLI-Downloader/releases/tag/v{version}",
    }


class ReleaseUpdateServiceTest(TestCase):
    def test_reports_available_current_and_newer_versions(self) -> None:
        available = _service("0.1.3", _release("0.1.4")).check()
        current = _service("0.1.3", _release("0.1.3")).check()
        newer = _service("0.2.0", _release("0.1.4")).check()

        self.assertIs(available.status, ReleaseUpdateStatus.UPDATE_AVAILABLE)
        self.assertEqual(available.latest_version, "0.1.4")
        self.assertIsNotNone(available.release_url)
        self.assertIs(current.status, ReleaseUpdateStatus.UP_TO_DATE)
        self.assertIs(newer.status, ReleaseUpdateStatus.CURRENT_NEWER)

    def test_non_release_source_version_is_reported_as_development_build(self) -> None:
        result = _service("0.1.4.dev1", _release("0.1.3")).check()

        self.assertIs(result.status, ReleaseUpdateStatus.DEVELOPMENT_BUILD)
        self.assertEqual(result.latest_version, "0.1.3")

    def test_invalid_payload_is_rejected(self) -> None:
        result = _service("0.1.3", {"tag_name": "not-a-version"}).check()

        self.assertIs(result.status, ReleaseUpdateStatus.FAILED)
        self.assertEqual(result.error_code, "invalid_response")

    def test_missing_or_non_github_repository_is_rejected_without_network(self) -> None:
        calls: list[object] = []

        def unexpected_fetch(request, timeout):
            calls.append(request)
            raise AssertionError("network should not be called")

        service = ReleaseUpdateService(
            repository_url="https://example.test/project",
            current_version="0.1.3",
            fetcher=unexpected_fetch,
        )

        result = service.check()

        self.assertIs(result.status, ReleaseUpdateStatus.FAILED)
        self.assertEqual(result.error_code, "unsupported_repository")
        self.assertEqual(calls, [])

    def test_github_no_release_and_rate_limit_have_distinct_results(self) -> None:
        def no_release(request, timeout):
            raise HTTPError(request.full_url, 404, "Not Found", {}, None)

        def rate_limited(request, timeout):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

        no_release_result = ReleaseUpdateService(
            repository_url="https://github.com/RyuuMeow/CLI-Downloader",
            current_version="0.1.3",
            fetcher=no_release,
        ).check()
        rate_limit_result = ReleaseUpdateService(
            repository_url="https://github.com/RyuuMeow/CLI-Downloader",
            current_version="0.1.3",
            fetcher=rate_limited,
        ).check()

        self.assertIs(no_release_result.status, ReleaseUpdateStatus.NO_RELEASES)
        self.assertIs(rate_limit_result.status, ReleaseUpdateStatus.FAILED)
        self.assertEqual(rate_limit_result.error_code, "rate_limited")

    def test_offline_failure_is_safe(self) -> None:
        def offline(request, timeout):
            raise URLError("offline")

        result = ReleaseUpdateService(
            repository_url="https://github.com/RyuuMeow/CLI-Downloader.git",
            current_version="0.1.3",
            fetcher=offline,
        ).check()

        self.assertIs(result.status, ReleaseUpdateStatus.FAILED)
        self.assertEqual(result.error_code, "offline")
