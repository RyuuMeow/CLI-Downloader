from pathlib import Path
from unittest import TestCase

from cli_downloader.core.errors import PluginValidationError
from cli_downloader.plugins.metadata import (
    metadata_compatibility_error,
    plugin_metadata_from_dict,
)


class PluginPackageMetadataTest(TestCase):
    def test_valid_plugin_metadata_parses_paths(self) -> None:
        metadata = plugin_metadata_from_dict(
            {
                "plugin_id": "example",
                "display_name": "Example Site",
                "version": "0.1.0",
                "entrypoint": "plugin.py:Plugin",
                "manifest": "example.json",
            }
        )

        self.assertEqual(metadata.plugin_id, "example")
        self.assertEqual(metadata.entrypoint.file_name, "plugin.py")
        self.assertEqual(metadata.entrypoint.class_name, "Plugin")
        self.assertIsNone(metadata.requires_python)
        self.assertEqual(
            str(metadata.manifest_path("plugins/example")),
            str(Path("plugins/example/manifests/example.json")),
        )
        self.assertEqual(
            str(metadata.entrypoint_path("plugins/example")),
            str(Path("plugins/example/plugin.py")),
        )

    def test_missing_required_field_is_rejected(self) -> None:
        with self.assertRaises(PluginValidationError):
            plugin_metadata_from_dict(
                {
                    "plugin_id": "example",
                    "display_name": "Example Site",
                    "entrypoint": "plugin.py:Plugin",
                    "manifest": "example.json",
                }
            )

    def test_compatibility_requirements_are_parsed(self) -> None:
        metadata = plugin_metadata_from_dict(
            {
                "plugin_id": "sample",
                "display_name": "Sample",
                "version": "1.0.0",
                "entrypoint": "plugin.py:Plugin",
                "manifest": "site.json",
                "requires_python": ">=3.11",
                "requires_core_api": ">=0.1.0",
            }
        )

        self.assertEqual(metadata.requires_python, ">=3.11")
        self.assertEqual(metadata.requires_core_api, ">=0.1.0")
        self.assertIsNone(metadata_compatibility_error(metadata))

    def test_invalid_compatibility_requirement_is_rejected(self) -> None:
        with self.assertRaises(PluginValidationError):
            plugin_metadata_from_dict(
                {
                    "plugin_id": "sample",
                    "display_name": "Sample",
                    "version": "1.0.0",
                    "entrypoint": "plugin.py:Plugin",
                    "manifest": "site.json",
                    "requires_python": "latest",
                }
            )

    def test_manifest_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(PluginValidationError):
            plugin_metadata_from_dict(
                {
                    "plugin_id": "example",
                    "display_name": "Example Site",
                    "version": "0.1.0",
                    "entrypoint": "plugin.py:Plugin",
                    "manifest": "../example.json",
                }
            )

    def test_entrypoint_subfolder_is_rejected(self) -> None:
        with self.assertRaises(PluginValidationError):
            plugin_metadata_from_dict(
                {
                    "plugin_id": "example",
                    "display_name": "Example Site",
                    "version": "0.1.0",
                    "entrypoint": "src/plugin.py:Plugin",
                    "manifest": "example.json",
                }
            )
