import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.plugins.activation import (
    PluginActivationStatus,
    activate_local_plugin_packages,
)
from cli_downloader.plugins.loader import scan_local_plugin_packages


class PluginActivationTest(TestCase):
    def test_activation_imports_and_instantiates_valid_plugin_package(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "activated.txt"
            _write_plugin(root / "sample", site_id="sample", marker=marker)
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(scan_report.valid_packages)

            self.assertEqual(len(activation_report.results), 1)
            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.ACTIVATED,
            )
            self.assertTrue(marker.exists())
            self.assertEqual(len(activation_report.activated_packages), 1)

    def test_activation_imports_plugin_local_sibling_package(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_folder = root / "sample"
            _write_plugin(plugin_folder, site_id="sample")
            helpers = plugin_folder / "helpers"
            helpers.mkdir()
            (helpers / "__init__.py").write_text("MARKER = 'local'\n", encoding="utf-8")
            (plugin_folder / "plugin.py").write_text(
                "from helpers import MARKER\n"
                "from cli_downloader.sites.manifest import site_manifest_from_json_file\n"
                "class Plugin:\n"
                "    site_id = 'sample'\n"
                "    display_name = MARKER\n"
                "    def can_handle_url(self, url): return False\n"
                "    def classify_url(self, url): return None\n"
                "    async def resolve(self, request): raise NotImplementedError\n"
                "    async def list_assets(self, item): raise NotImplementedError\n"
                "    async def choose_strategy(self, item): raise NotImplementedError\n"
                "    async def execute(self, request): raise NotImplementedError\n"
                "    def get_manifest(self): return site_manifest_from_json_file(__file__.replace('plugin.py', 'manifests/site.json'))\n",
                encoding="utf-8",
            )
            report = activate_local_plugin_packages(
                scan_local_plugin_packages([root]).valid_packages
            )

            self.assertEqual(report.results[0].status, PluginActivationStatus.ACTIVATED)
            self.assertEqual(report.activated_packages[0].instance.display_name, "local")

    def test_activation_prefers_version_scoped_dependency_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin_folder = root / "sample"
            _write_plugin(plugin_folder, site_id="sample")
            deps = plugin_folder / "deps" / "dependency_helpers"
            deps.mkdir(parents=True)
            (deps / "__init__.py").write_text("MARKER = 'dependency'\n", encoding="utf-8")
            (plugin_folder / "plugin.py").write_text(
                "from dependency_helpers import MARKER\n"
                "from cli_downloader.sites.manifest import site_manifest_from_json_file\n"
                "class Plugin:\n"
                "    site_id = 'sample'\n"
                "    display_name = MARKER\n"
                "    def can_handle_url(self, url): return False\n"
                "    def classify_url(self, url): return None\n"
                "    async def resolve(self, request): raise NotImplementedError\n"
                "    async def list_assets(self, item): raise NotImplementedError\n"
                "    async def choose_strategy(self, item): raise NotImplementedError\n"
                "    async def execute(self, request): raise NotImplementedError\n"
                "    def get_manifest(self): return site_manifest_from_json_file(__file__.replace('plugin.py', 'manifests/site.json'))\n",
                encoding="utf-8",
            )
            report = activate_local_plugin_packages(
                scan_local_plugin_packages([root]).valid_packages
            )

            self.assertEqual(report.results[0].status, PluginActivationStatus.ACTIVATED)
            self.assertEqual(report.activated_packages[0].instance.display_name, "dependency")

    def test_activation_reports_missing_entrypoint_class_as_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", site_id="sample", class_name="Missing")
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(scan_report.valid_packages)

            self.assertEqual(len(activation_report.results), 1)
            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.FAILED,
            )
            self.assertIn("entrypoint class not found", activation_report.results[0].error)

    def test_activation_rejects_plugin_missing_required_adapter_method(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", site_id="sample", include_resolve=False)
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(scan_report.valid_packages)

            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.FAILED,
            )
            self.assertIn(
                "adapter.resolve must be async callable", activation_report.results[0].error
            )

    def test_activation_rejects_plugin_missing_generic_operation_method(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", site_id="sample", include_execute=False)
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(scan_report.valid_packages)

            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.FAILED,
            )
            self.assertIn(
                "adapter.execute must be async callable", activation_report.results[0].error
            )

    def test_activation_rejects_adapter_site_id_that_differs_from_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", site_id="sample", adapter_site_id="other")
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(scan_report.valid_packages)

            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.FAILED,
            )
            self.assertIn("adapter.site_id must match", activation_report.results[0].error)

    def test_activation_rejects_registered_site_id(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", site_id="sample")
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(
                scan_report.valid_packages,
                existing_site_ids=("sample",),
            )

            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.FAILED,
            )
            self.assertIn("already registered", activation_report.results[0].error)

    def test_activation_verifies_manifest_name_getter(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", site_id="sample", manifest_name="wrong.json")
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(scan_report.valid_packages)

            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.FAILED,
            )
            self.assertIn("get_manifest_name must match", activation_report.results[0].error)

    def test_activation_verifies_manifest_getter_data(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _write_plugin(root / "sample", site_id="sample", manifest_getter="mismatch")
            scan_report = scan_local_plugin_packages([root])

            activation_report = activate_local_plugin_packages(scan_report.valid_packages)

            self.assertEqual(
                activation_report.results[0].status,
                PluginActivationStatus.FAILED,
            )
            self.assertIn("get_manifest must match", activation_report.results[0].error)


def _write_plugin(
    plugin_folder: Path,
    *,
    site_id: str,
    adapter_site_id: str | None = None,
    marker: Path | None = None,
    class_name: str = "Plugin",
    include_resolve: bool = True,
    include_execute: bool = True,
    manifest_name: str | None = "site.json",
    manifest_getter: str | None = "matching",
) -> None:
    plugin_folder.mkdir(parents=True)
    (plugin_folder / "manifests").mkdir()
    _write_json(
        plugin_folder / "plugin.json",
        {
            "plugin_id": site_id,
            "display_name": site_id.title(),
            "version": "0.1.0",
            "entrypoint": f"plugin.py:{class_name}",
            "manifest": "site.json",
        },
    )
    _write_json(plugin_folder / "manifests" / "site.json", _manifest_payload(site_id))
    marker_line = ""
    if marker is not None:
        marker_line = f"Path(r'{marker}').write_text('activated', encoding='utf-8')\n"
    adapter_site_id = adapter_site_id or site_id
    lines = [
        "from pathlib import Path",
        "from cli_downloader.sites.manifest import site_manifest_from_dict, site_manifest_from_json_file",
        marker_line,
        "class Plugin:",
        f"    site_id = {adapter_site_id!r}",
        f"    display_name = {site_id.title()!r}",
        "    def can_handle_url(self, url):",
        f"        return url.startswith('https://{site_id}.test/')",
        "    def classify_url(self, url):",
        "        return None",
    ]
    if include_resolve:
        lines.extend(
            [
                "    async def resolve(self, request):",
                "        raise NotImplementedError",
            ]
        )
    lines.extend(
        [
            "    async def list_assets(self, item):",
            "        raise NotImplementedError",
            "    async def choose_strategy(self, item):",
            "        raise NotImplementedError",
        ]
    )
    if include_execute:
        lines.extend(
            [
                "    async def execute(self, request):",
                "        raise NotImplementedError",
            ]
        )
    if manifest_name is not None:
        lines.extend(
            [
                "    def get_manifest_name(self):",
                f"        return {manifest_name!r}",
            ]
        )
    if manifest_getter == "matching":
        lines.extend(
            [
                "    def get_manifest(self):",
                "        return site_manifest_from_json_file(Path(__file__).parent / 'manifests' / 'site.json')",
            ]
        )
    elif manifest_getter == "mismatch":
        lines.extend(
            [
                "    def get_manifest(self):",
                "        return site_manifest_from_dict({",
                "            'site_id': 'other',",
                "            'display_name': 'Other',",
                "            'domains': ['other.test'],",
                "            'url_rules': [{",
                "                'name': 'gallery',",
                "                'pattern': r'^https://other\\.test/g/(?P<gallery_id>\\d+)$',",
                "                'input_kind': 'single',",
                "                'operation': 'single_download',",
                "            }],",
                "            'operations': {'single_download': {'params': {}}},",
                "        })",
            ]
        )
    (plugin_folder / "plugin.py").write_text(
        "\n".join((*lines, "")),
        encoding="utf-8",
    )


def _manifest_payload(site_id: str) -> dict[str, object]:
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
