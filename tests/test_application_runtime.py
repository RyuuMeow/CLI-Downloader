import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.plugins.activation import PluginActivationStatus
from cli_downloader.plugins.runtime import build_application_runtime
from cli_downloader.sites.registry import hostname_from_url, normalize_hostname
from tests.example_site import build_example_runtime, example_runtime_plugin
from tests.test_plugin_activation import _write_plugin


class ApplicationRuntimeTest(TestCase):
    def test_production_runtime_has_no_builtin_site_plugins(self) -> None:
        runtime = build_application_runtime(plugin_roots=())

        self.assertEqual(runtime.plugins_by_id, {})
        self.assertEqual(runtime.plugins_by_site_id, {})
        self.assertEqual(runtime.site_registry.adapters, ())

    def test_fixed_project_plugin_overrides_installed_version_and_activates(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_marker = root / "project.txt"
            installed_marker = root / "installed.txt"
            _write_plugin(
                root / "app" / "Plugins" / "sample",
                site_id="sample",
                marker=project_marker,
            )
            _write_plugin(
                root / "local" / "CLI-Downloader" / "plugins" / "sample" / "1.0.0",
                site_id="sample",
                marker=installed_marker,
            )

            runtime = build_application_runtime(
                application_root=root / "app",
                local_app_data=root / "local",
            )

            self.assertTrue(project_marker.exists())
            self.assertFalse(installed_marker.exists())
            self.assertIsNotNone(runtime.plugin_for_id("sample"))
            self.assertIn("plugin_source_overridden", {item.code for item in runtime.diagnostics})

    def test_fixed_installed_root_uses_declared_active_version(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "local" / "CLI-Downloader" / "plugins" / "sample"
            old_marker = root / "old.txt"
            new_marker = root / "new.txt"
            _write_plugin(plugin_root / "1.0.0", site_id="sample", marker=old_marker)
            _write_plugin(plugin_root / "2.0.0", site_id="sample", marker=new_marker)
            (plugin_root / "active").write_text("1.0.0", encoding="utf-8")

            build_application_runtime(
                application_root=root / "app",
                local_app_data=root / "local",
            )

            self.assertTrue(old_marker.exists())
            self.assertFalse(new_marker.exists())

    def test_builtin_indexes_use_normalized_hostname_lookup(self) -> None:
        runtime = build_example_runtime()

        adapter = runtime.plugin_for_url("https://EXAMPLE.TEST.:443/gallery/welcome")

        self.assertIs(adapter, runtime.plugin_for_id("example"))
        self.assertIs(adapter, runtime.plugin_for_site_id("example"))
        self.assertEqual(runtime.manifest_for_plugin_id("example").site_id, "example")
        self.assertEqual(runtime.manifest_for_site_id("example").site_id, "example")
        self.assertIs(adapter, runtime.site_registry.adapter_for_site_id("example"))
        self.assertEqual(runtime.settings_registry.plugin_ids, ("example",))

    def test_hostname_normalization_supports_idna_and_rejects_relative_urls(self) -> None:
        self.assertEqual(normalize_hostname("Bücher.Example."), "xn--bcher-kva.example")
        self.assertEqual(
            hostname_from_url("https://Bücher.Example/gallery"),
            "xn--bcher-kva.example",
        )
        self.assertIsNone(hostname_from_url("example.test/gallery"))

    def test_external_plugin_is_scanned_but_not_executed_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "activated.txt"
            _write_plugin(root / "sample", site_id="sample", marker=marker)

            runtime = build_application_runtime(plugin_roots=(root,))

            self.assertFalse(marker.exists())
            self.assertIsNone(runtime.plugin_for_id("sample"))
            self.assertEqual(runtime.scan_report.valid_packages[0].metadata.plugin_id, "sample")
            self.assertEqual(runtime.diagnostics[0].code, "external_plugin_disabled")

    def test_enabled_external_plugin_is_registered_in_all_indexes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "activated.txt"
            _write_plugin(root / "sample", site_id="sample", marker=marker)

            runtime = build_application_runtime(
                plugin_roots=(root,),
                enable_external_code=True,
                builtin_plugins=(example_runtime_plugin(),),
            )

            self.assertTrue(marker.exists())
            adapter = runtime.plugin_for_id("sample")
            self.assertIsNotNone(adapter)
            self.assertIs(adapter, runtime.plugin_for_url("https://SAMPLE.TEST/g/1"))
            self.assertEqual(runtime.manifest_for_plugin_id("sample").site_id, "sample")
            self.assertEqual(
                runtime.activation_report.results[0].status, PluginActivationStatus.ACTIVATED
            )

    def test_builtin_plugin_id_conflict_is_rejected_before_import(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "activated.txt"
            _write_plugin(root / "example", site_id="example", marker=marker)

            runtime = build_application_runtime(
                plugin_roots=(root,),
                enable_external_code=True,
                builtin_plugins=(example_runtime_plugin(),),
            )

            self.assertFalse(marker.exists())
            self.assertEqual(
                runtime.activation_report.results[0].status, PluginActivationStatus.FAILED
            )
            self.assertIn("duplicate plugin_id", runtime.activation_report.results[0].error)

    def test_builtin_site_id_conflict_is_rejected_before_import(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "activated.txt"
            plugin_folder = root / "other"
            _write_plugin(plugin_folder, site_id="example", marker=marker)
            metadata_path = plugin_folder / "plugin.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["plugin_id"] = "other"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            runtime = build_application_runtime(
                plugin_roots=(root,),
                enable_external_code=True,
                builtin_plugins=(example_runtime_plugin(),),
            )

            self.assertFalse(marker.exists())
            self.assertIn(
                "site_id is already registered", runtime.activation_report.results[0].error
            )

    def test_builtin_hostname_conflict_is_rejected_before_import(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "activated.txt"
            plugin_folder = root / "sample"
            _write_plugin(plugin_folder, site_id="sample", marker=marker)
            manifest_path = plugin_folder / "manifests" / "site.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["domains"] = ["EXAMPLE.TEST."]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            runtime = build_application_runtime(
                plugin_roots=(root,),
                enable_external_code=True,
                builtin_plugins=(example_runtime_plugin(),),
            )

            self.assertFalse(marker.exists())
            self.assertIn(
                "hostname is already registered", runtime.activation_report.results[0].error
            )

    def test_runtime_indexes_are_immutable(self) -> None:
        runtime = build_example_runtime()

        with self.assertRaises(TypeError):
            runtime.plugins_by_id["other"] = runtime.plugins_by_id["example"]


if __name__ == "__main__":
    import unittest

    unittest.main()
