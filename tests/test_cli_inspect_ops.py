from unittest import TestCase

from cli_downloader.cli._inspect_ops import (
    redacted_headers,
    route_url_through_manifests,
    with_default_strategy,
)
from cli_downloader.config.loader import LoadedConfig
from cli_downloader.core.models import DownloadStrategy
from tests.example_site import build_example_runtime


class CLIInspectOpsTest(TestCase):
    def test_redacted_headers_hide_authentication_values(self) -> None:
        self.assertEqual(
            redacted_headers(
                {
                    "Authorization": "Bearer secret",
                    "Cookie": "session=secret",
                    "X-Trace": "visible",
                }
            ),
            {
                "Authorization": "<redacted>",
                "Cookie": "<redacted>",
                "X-Trace": "visible",
            },
        )

    def test_manifest_routing_stays_read_only_for_unknown_url(self) -> None:
        runtime = build_example_runtime()

        self.assertEqual(
            route_url_through_manifests(
                "https://unknown.invalid/gallery/1",
                runtime=runtime,
            ),
            (None, None),
        )

    def test_example_manifest_route_is_available_without_network(self) -> None:
        runtime = build_example_runtime()
        manifest, route = route_url_through_manifests(
            "https://example.test/gallery/welcome",
            runtime=runtime,
        )

        self.assertIsNotNone(manifest)
        self.assertIsNotNone(route)
        self.assertEqual(route.operation, "resolve_gallery")

    def test_strategy_default_is_applied_by_inspection_module(self) -> None:
        runtime = build_example_runtime()
        config = LoadedConfig(
            values={"download.strategy": DownloadStrategy.TORRENT.value},
            secrets={},
            sources=(),
        )
        self.assertEqual(
            with_default_strategy(runtime, "plugin://example/single_download", {}, config),
            {"strategy": "torrent"},
        )
