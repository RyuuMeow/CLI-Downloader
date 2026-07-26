import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.plugins.loader import PluginScanStatus, scan_local_plugin_packages


class LocalPluginLoaderTest(TestCase):
    def test_scans_valid_direct_child_plugin(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", plugin_id="sample", site_id="sample")

            report = scan_local_plugin_packages([root])

            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.results[0].status, PluginScanStatus.VALID)
            self.assertEqual(len(report.valid_packages), 1)
            package = report.valid_packages[0]
            self.assertEqual(package.metadata.plugin_id, "sample")
            self.assertEqual(package.manifest.site_id, "sample")
            self.assertIsNotNone(package.manifest.route_url("https://sample.test/g/123"))

    def test_ignores_direct_child_without_plugin_json_by_default(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "notes").mkdir()
            _write_plugin(root / "sample", plugin_id="sample", site_id="sample")

            report = scan_local_plugin_packages([root])

            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.results[0].folder.name, "sample")

    def test_can_report_ignored_direct_child_without_plugin_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "notes").mkdir()

            report = scan_local_plugin_packages([root], include_ignored=True)

            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.results[0].status, PluginScanStatus.IGNORED)
            self.assertEqual(report.results[0].error, "plugin.json not found")

    def test_missing_manifest_marks_plugin_invalid(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_folder = root / "sample"
            plugin_folder.mkdir()
            _write_json(
                plugin_folder / "plugin.json",
                _plugin_payload(plugin_id="sample", manifest="missing.json"),
            )

            report = scan_local_plugin_packages([root])

            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.results[0].status, PluginScanStatus.INVALID)
            self.assertIn("Cannot read site manifest JSON", report.results[0].error)
            self.assertEqual(report.valid_packages, ())

    def test_incompatible_python_requirement_is_reported_without_importing_plugin(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_folder = root / "sample"
            _write_plugin(plugin_folder, plugin_id="sample", site_id="sample")
            metadata_path = plugin_folder / "plugin.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["requires_python"] = ">=99"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

            report = scan_local_plugin_packages([root])

            self.assertEqual(report.results[0].status, PluginScanStatus.INVALID)
            self.assertIn("requires_python", report.results[0].error)

    def test_duplicate_plugin_id_marks_second_plugin_invalid(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "alpha", plugin_id="duplicate", site_id="alpha")
            _write_plugin(root / "beta", plugin_id="duplicate", site_id="beta")

            report = scan_local_plugin_packages([root])

            self.assertEqual(
                [result.status for result in report.results],
                [
                    PluginScanStatus.VALID,
                    PluginScanStatus.INVALID,
                ],
            )
            self.assertIn("duplicate plugin_id", report.results[1].error)
            self.assertEqual(len(report.valid_packages), 1)

    def test_duplicate_site_id_marks_second_plugin_invalid(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "alpha", plugin_id="alpha", site_id="duplicate")
            _write_plugin(root / "beta", plugin_id="beta", site_id="duplicate")

            report = scan_local_plugin_packages([root])

            self.assertEqual(
                [result.status for result in report.results],
                [
                    PluginScanStatus.VALID,
                    PluginScanStatus.INVALID,
                ],
            )
            self.assertIn("duplicate site_id", report.results[1].error)
            self.assertEqual(len(report.valid_packages), 1)

    def test_missing_root_is_reported_invalid(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "missing"

            report = scan_local_plugin_packages([root])

            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.results[0].status, PluginScanStatus.INVALID)
            self.assertEqual(report.results[0].error, "plugin root does not exist")


def _write_plugin(plugin_folder: Path, *, plugin_id: str, site_id: str) -> None:
    plugin_folder.mkdir()
    (plugin_folder / "manifests").mkdir()
    _write_json(
        plugin_folder / "plugin.json",
        _plugin_payload(plugin_id=plugin_id, manifest="site.json"),
    )
    _write_json(
        plugin_folder / "manifests" / "site.json",
        _manifest_payload(site_id=site_id),
    )
    (plugin_folder / "plugin.py").write_text(
        "class Plugin:\n    pass\n",
        encoding="utf-8",
    )


def _plugin_payload(*, plugin_id: str, manifest: str) -> dict[str, object]:
    return {
        "plugin_id": plugin_id,
        "display_name": plugin_id.title(),
        "version": "0.1.0",
        "entrypoint": "plugin.py:Plugin",
        "manifest": manifest,
    }


def _manifest_payload(*, site_id: str) -> dict[str, object]:
    return {
        "site_id": site_id,
        "display_name": site_id.title(),
        "domains": [f"{site_id}.test"],
        "url_rules": [
            {
                "name": "gallery",
                "pattern": rf"^https://{site_id}\.test/g/(?P<gallery_id>\d+)$",
                "input_kind": "single",
                "operation": "single_download",
            }
        ],
        "operations": {
            "single_download": {
                "params": {
                    "strategy": {
                        "type": "string",
                        "default": "auto",
                    }
                }
            }
        },
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
