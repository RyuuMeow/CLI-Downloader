from unittest import TestCase

from cli_downloader.core.errors import PluginValidationError
from cli_downloader.sites.manifest import InputKind, site_manifest_from_dict
from tests.example_site import get_example_manifest


class SiteManifestTest(TestCase):
    def test_example_manifest_routes_example_item(self) -> None:
        manifest = get_example_manifest()

        route = manifest.route_url("example://gallery/welcome")

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.site_id, "example")
        self.assertEqual(route.rule_name, "example_item")
        self.assertEqual(route.input_kind, InputKind.SINGLE)
        self.assertEqual(route.operation, "single_download")
        self.assertEqual(route.captures, {"section": "gallery", "slug": "welcome"})
        self.assertEqual(route.to_dict()["required_confirmations"], [])

    def test_manifest_does_not_route_unknown_url(self) -> None:
        manifest = get_example_manifest()

        self.assertIsNone(manifest.route_url("https://example.test/g/123/abcdef/"))

    def test_json_manifest_routes_url_after_parsing(self) -> None:
        manifest = site_manifest_from_dict(
            {
                "site_id": "sample",
                "display_name": "Sample Site",
                "version": "1",
                "domains": ["sample.test"],
                "browser_auth": {
                    "login_url": "https://accounts.sample.test/login",
                    "cookie_domains": ["sample.test"],
                    "required_cookies": ["session_id"],
                },
                "url_rules": [
                    {
                        "name": "gallery",
                        "pattern": r"^https://sample\.test/g/(?P<gallery_id>\d+)$",
                        "input_kind": "single",
                        "operation": "single_download",
                    }
                ],
                "operations": {
                    "single_download": {
                        "supported": True,
                        "requires_live_network_confirmation": False,
                        "params": {
                            "strategy": {
                                "type": "string",
                                "default": "auto",
                                "enum": ["auto", "direct"],
                                "editable": False,
                            }
                        },
                    }
                },
                "settings": {
                    "download.naming_template": {
                        "type": "string",
                        "default": "{gallery_id}",
                    }
                },
                "secrets": {
                    "cookies.session": {
                        "type": "string",
                        "required": False,
                    }
                },
                "output": {
                    "fields": ["author_name", "artwork_id"],
                    "default_folder_template": "{author_name}/{artwork_id}-{title}",
                },
                "network": {
                    "max_concurrency": 2,
                    "min_interval_seconds": 0.75,
                    "retryable_status_codes": [429, 503],
                    "max_attempts": 4,
                    "max_backoff_seconds": 120,
                },
            }
        )

        route = manifest.route_url("https://sample.test/g/123")

        self.assertIsNotNone(route)
        assert route is not None
        self.assertEqual(route.site_id, "sample")
        self.assertEqual(route.input_kind, InputKind.SINGLE)
        self.assertEqual(route.captures, {"gallery_id": "123"})
        self.assertFalse(route.to_dict()["params"]["strategy"]["editable"])
        self.assertTrue(manifest.secrets["cookies.session"].secret)
        self.assertEqual(manifest.browser_auth.required_cookies, ("session_id",))
        self.assertEqual(manifest.output.fields, ("author_name", "artwork_id"))
        self.assertEqual(
            manifest.output.default_folder_template,
            "{author_name}/{artwork_id}-{title}",
        )
        assert manifest.network is not None
        self.assertEqual(manifest.network.retryable_status_codes, (429, 503))
        self.assertEqual(manifest.network.max_attempts, 4)

    def test_json_manifest_rejects_invalid_network_policy(self) -> None:
        with self.assertRaises(PluginValidationError):
            site_manifest_from_dict(
                {
                    "site_id": "sample",
                    "display_name": "Sample Site",
                    "domains": ["sample.test"],
                    "url_rules": [],
                    "operations": {},
                    "network": {"max_concurrency": 0},
                }
            )

    def test_json_manifest_rejects_undeclared_output_field(self) -> None:
        with self.assertRaises(PluginValidationError):
            site_manifest_from_dict(
                {
                    "site_id": "sample",
                    "display_name": "Sample Site",
                    "domains": ["sample.test"],
                    "url_rules": [],
                    "operations": {},
                    "output": {"default_folder_template": "{author_name}/{title}"},
                }
            )

    def test_json_manifest_rejects_unknown_operation_reference(self) -> None:
        with self.assertRaises(PluginValidationError):
            site_manifest_from_dict(
                {
                    "site_id": "sample",
                    "display_name": "Sample Site",
                    "domains": ["sample.test"],
                    "url_rules": [
                        {
                            "name": "gallery",
                            "pattern": r"^https://sample\.test/g/(?P<gallery_id>\d+)$",
                            "input_kind": "single",
                            "operation": "missing_operation",
                        }
                    ],
                    "operations": {},
                }
            )

    def test_json_manifest_rejects_invalid_input_kind(self) -> None:
        with self.assertRaises(PluginValidationError):
            site_manifest_from_dict(
                {
                    "site_id": "sample",
                    "display_name": "Sample Site",
                    "domains": ["sample.test"],
                    "url_rules": [
                        {
                            "name": "gallery",
                            "pattern": r"^https://sample\.test/g/(?P<gallery_id>\d+)$",
                            "input_kind": "feed",
                            "operation": "single_download",
                        }
                    ],
                    "operations": {
                        "single_download": {
                            "params": {},
                        }
                    },
                }
            )
