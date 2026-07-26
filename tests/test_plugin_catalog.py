import json
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.plugins.catalog import (
    CATALOG_REPOSITORY,
    CATALOG_URLS,
    PluginCatalogError,
    PluginCatalogService,
    merge_catalog_entries,
    parse_catalog_document,
)


def _catalog(source: str, plugins: list[dict[str, object]]) -> dict[str, object]:
    return {"schema_version": 1, "source": source, "plugins": plugins}


class _Response:
    def __init__(self, payload: dict[str, object], headers: dict[str, str] | None = None) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass

    def read(self) -> bytes:
        return self._body


class CatalogValidationTest(TestCase):
    def test_marketplace_urls_use_the_published_lowercase_files(self) -> None:
        self.assertEqual(
            CATALOG_REPOSITORY,
            "https://github.com/RyuuMeow/CLI-Downloader-Plugin-Marketplace.git",
        )
        self.assertTrue(CATALOG_URLS["official"].endswith("/main/official.json"))
        self.assertTrue(CATALOG_URLS["community"].endswith("/main/community.json"))

    def test_rejects_non_https_repository(self) -> None:
        payload = _catalog(
            "official",
            [{"id": "unsafe", "repository": "http://example.test/plugin.git"}],
        )
        with self.assertRaisesRegex(PluginCatalogError, "HTTPS URL"):
            parse_catalog_document(payload, expected_source="official")

    def test_rejects_duplicate_ids(self) -> None:
        payload = _catalog(
            "community",
            [
                {"id": "same", "repository": "https://example.test/one.git"},
                {"id": "same", "repository": "https://example.test/two.git"},
            ],
        )
        with self.assertRaisesRegex(PluginCatalogError, "duplicate plugin id"):
            parse_catalog_document(payload, expected_source="community")

    def test_official_wins_and_community_is_a_difference(self) -> None:
        official = parse_catalog_document(
            _catalog(
                "official",
                [{"id": "shared", "repository": "https://example.test/official.git"}],
            ),
            expected_source="official",
        ).entries
        community = parse_catalog_document(
            _catalog(
                "community",
                [
                    {"id": "shared", "repository": "https://example.test/community.git"},
                    {"id": "repo-copy", "repository": "https://example.test/official"},
                    {"id": "unique", "repository": "https://example.test/unique.git"},
                ],
            ),
            expected_source="community",
        ).entries

        merged = merge_catalog_entries(official, community)

        self.assertEqual([entry.plugin_id for entry in merged], ["shared", "unique"])
        self.assertEqual(merged[0].source, "official")
        self.assertEqual(merged[1].source, "community")


class CatalogServiceTest(TestCase):
    def test_fetches_both_lists_and_writes_cache(self) -> None:
        calls: list[object] = []

        def urlopen(request, timeout):
            calls.append(request)
            source = "official" if request.full_url == CATALOG_URLS["official"] else "community"
            plugins = [
                {
                    "id": source,
                    "repository": f"https://example.test/{source}.git",
                }
            ]
            return _Response(_catalog(source, plugins), {"ETag": f'"{source}-v1"'})

        with TemporaryDirectory() as directory:
            service = PluginCatalogService(Path(directory), urlopen=urlopen, clock=lambda: 100.0)
            result = service.load()

            self.assertEqual(result.status, "current")
            self.assertEqual(
                {entry.plugin_id for entry in result.entries}, {"official", "community"}
            )
            self.assertEqual(len(calls), 2)
            self.assertTrue((Path(directory) / "plugin-catalog" / "official.json").is_file())
            self.assertTrue((Path(directory) / "plugin-catalog" / "community.json").is_file())

    def test_fresh_cache_skips_network(self) -> None:
        with TemporaryDirectory() as directory:
            service = PluginCatalogService(
                Path(directory),
                urlopen=lambda request, timeout: _Response(
                    _catalog(
                        "official" if request.full_url == CATALOG_URLS["official"] else "community",
                        [],
                    )
                ),
                clock=lambda: 100.0,
            )
            service.load()
            offline = PluginCatalogService(
                Path(directory),
                urlopen=lambda *_args, **_kwargs: self.fail("network should not be used"),
                clock=lambda: 101.0,
            )

            result = offline.load()

            self.assertEqual(result.status, "cached")

    def test_network_failure_without_cache_returns_empty_catalog(self) -> None:
        def unavailable(request, timeout):
            raise urllib.error.URLError("offline")

        with TemporaryDirectory() as directory:
            result = PluginCatalogService(Path(directory), urlopen=unavailable).load()

            self.assertEqual(result.status, "offline")
            self.assertEqual(result.entries, ())
            self.assertEqual(len(result.diagnostics), 2)

    def test_no_refresh_without_cache_is_safe_and_empty(self) -> None:
        with TemporaryDirectory() as directory:
            result = PluginCatalogService(Path(directory)).load(refresh=False)

            self.assertEqual(result.status, "empty")
            self.assertEqual(result.entries, ())
            self.assertEqual(result.diagnostics, ())

    def test_invalid_remote_does_not_replace_valid_cache(self) -> None:
        clock = [100.0]

        def valid(request, timeout):
            source = "official" if request.full_url == CATALOG_URLS["official"] else "community"
            return _Response(
                _catalog(
                    source,
                    [{"id": source, "repository": f"https://example.test/{source}.git"}],
                )
            )

        with TemporaryDirectory() as directory:
            PluginCatalogService(Path(directory), urlopen=valid, clock=lambda: clock[0]).load()
            clock[0] = 100_000.0
            invalid = PluginCatalogService(
                Path(directory),
                urlopen=lambda request, timeout: _Response({"schema_version": 999}),
                clock=lambda: clock[0],
            ).load()

            self.assertEqual(invalid.status, "stale")
            self.assertEqual(
                {entry.plugin_id for entry in invalid.entries},
                {"official", "community"},
            )

    def test_conditional_request_keeps_cache_on_not_modified(self) -> None:
        headers_seen: list[dict[str, str]] = []

        def initial(request, timeout):
            source = "official" if request.full_url == CATALOG_URLS["official"] else "community"
            return _Response(_catalog(source, []), {"ETag": f'"{source}"'})

        with TemporaryDirectory() as directory:
            PluginCatalogService(Path(directory), urlopen=initial, clock=lambda: 1.0).load()

            def unchanged(request, timeout):
                headers_seen.append(dict(request.header_items()))
                raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

            result = PluginCatalogService(
                Path(directory), urlopen=unchanged, clock=lambda: 100_000.0
            ).load()

            self.assertEqual(result.status, "current")
            self.assertTrue(all("If-none-match" in headers for headers in headers_seen))
