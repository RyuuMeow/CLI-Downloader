from unittest import TestCase

from cli_downloader.core.errors import DuplicateSiteAdapterError
from cli_downloader.sites.registry import SiteRegistry
from tests.example_site import ExampleAdapter


class SiteRegistryTest(TestCase):
    def test_registry_prevents_duplicate_site_ids(self) -> None:
        registry = SiteRegistry()
        registry.register(ExampleAdapter())

        with self.assertRaises(DuplicateSiteAdapterError):
            registry.register(ExampleAdapter())

    def test_example_adapter_matches_example_scheme(self) -> None:
        adapter = ExampleAdapter()

        self.assertTrue(adapter.can_handle_url("example://gallery/welcome"))
        self.assertFalse(adapter.can_handle_url("https://example.com/gallery/welcome"))
