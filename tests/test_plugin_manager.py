import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cli_downloader.plugins.manager import (
    _extract_zip_archive,
    _installed_versions,
    _install_plugin_dependencies,
    _read_active_marker,
    _write_active_marker,
    _version_key,
    check_plugin_update,
    install_plugin,
    list_plugin_versions,
    remove_plugin,
    rollback_plugin,
    update_plugin,
)
from cli_downloader.plugins.registry import load_registry


def _make_plugin_repo(
    parent: Path,
    name: str,
    plugin_id: str,
    version: str,
    *,
    requires_core_api: str | None = None,
) -> str:
    """Create a local git repo with plugin.json and return its file:// URL."""
    repo = parent / name
    repo.mkdir()
    metadata = {
        "plugin_id": plugin_id,
        "display_name": name.title(),
        "version": version,
        "entrypoint": "plugin.py:Plugin",
        "manifest": "site.json",
        "description": f"A {name} plugin.",
    }
    if requires_core_api is not None:
        metadata["requires_core_api"] = requires_core_api
    (repo / "plugin.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    (repo / "manifests").mkdir()
    (repo / "manifests" / "site.json").write_text(
        json.dumps(
            {
                "site_id": plugin_id,
                "display_name": name.title(),
                "domains": [f"{plugin_id}.test"],
                "url_rules": [
                    {
                        "name": "gallery",
                        "pattern": rf"^https://{plugin_id}\.test/g/(?P<gallery_id>\d+)$",
                        "input_kind": "single",
                        "operation": "single_download",
                    }
                ],
                "operations": {
                    "single_download": {
                        "params": {"strategy": {"type": "string", "default": "auto"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (repo / "plugin.py").write_text("class Plugin:\n    site_id = 'test'\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.test"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    return f"file:///{repo.as_posix()}"


class VersionKeyTest(TestCase):
    def test_sorts_semantically(self) -> None:
        self.assertLess(_version_key("0.1.0"), _version_key("1.0.0"))
        self.assertLess(_version_key("1.0.0"), _version_key("2.0.0"))
        self.assertLess(_version_key("0.9.0"), _version_key("0.10.0"))

    def test_alphanumeric_parts(self) -> None:
        self.assertTrue(_version_key("2.0.0") > _version_key("1.9.9"))


class ActiveMarkerTest(TestCase):
    def test_write_and_read(self) -> None:
        with TemporaryDirectory() as d:
            plugin_dir = Path(d)
            version_dir = plugin_dir / "1.2.3"
            version_dir.mkdir()
            _write_active_marker(plugin_dir, "1.2.3")
            self.assertEqual(_read_active_marker(plugin_dir), "1.2.3")

    def test_read_returns_none_when_missing(self) -> None:
        with TemporaryDirectory() as d:
            self.assertIsNone(_read_active_marker(Path(d)))

    def test_read_ignores_invalid_version_dir(self) -> None:
        with TemporaryDirectory() as d:
            plugin_dir = Path(d)
            _write_active_marker(plugin_dir, "9.9.9")
            # version directory doesn't exist → None
            self.assertIsNone(_read_active_marker(plugin_dir))


class InstalledVersionsTest(TestCase):
    def test_lists_sorted_version_dirs(self) -> None:
        with TemporaryDirectory() as d:
            plugin_dir = Path(d)
            (plugin_dir / "2.0.0").mkdir()
            (plugin_dir / "1.0.0").mkdir()
            (plugin_dir / "0.1.0").mkdir()
            versions = _installed_versions(plugin_dir)
            self.assertEqual(versions, ["0.1.0", "1.0.0", "2.0.0"])

    def test_empty_for_missing_dir(self) -> None:
        self.assertEqual(_installed_versions(Path("/nonexistent/xyz")), [])


class DependencyInstallTest(TestCase):
    def test_runtime_requirements_are_installed_into_version_local_deps(self) -> None:
        with TemporaryDirectory() as d:
            plugin_dir = Path(d)
            (plugin_dir / "requirements.txt").write_text("sample-package==1.0\n", encoding="utf-8")

            def fake_run(command, **kwargs):
                target = Path(command[command.index("--target") + 1])
                (target / "sample_package").mkdir()
                (target / "sample_package" / "__init__.py").write_text("", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with patch(
                "cli_downloader.plugins.manager.subprocess.run", side_effect=fake_run
            ) as run:
                _install_plugin_dependencies(plugin_dir)

            self.assertTrue((plugin_dir / "deps" / "sample_package" / "__init__.py").exists())
            run.assert_called_once()
            self.assertIn(str(plugin_dir / "requirements.txt"), run.call_args.args[0])

    def test_frozen_runtime_uses_bundled_internal_pip_entrypoint(self) -> None:
        with TemporaryDirectory() as d:
            plugin_dir = Path(d)
            (plugin_dir / "requirements.txt").write_text("sample-package==1.0\n", encoding="utf-8")

            def fake_run(command, **kwargs):
                Path(command[command.index("--target") + 1]).mkdir(exist_ok=True)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with (
                patch("cli_downloader.plugins.manager.subprocess.run", side_effect=fake_run) as run,
                patch("cli_downloader.plugins.manager.sys.frozen", True, create=True),
            ):
                _install_plugin_dependencies(plugin_dir)

            self.assertEqual(
                run.call_args.args[0][0],
                str(Path(sys.executable).with_name("CLI-Downloader.PluginInstaller.exe")),
            )

    def test_dependency_failure_does_not_leave_partial_deps_directory(self) -> None:
        with TemporaryDirectory() as d:
            plugin_dir = Path(d)
            (plugin_dir / "requirements.txt").write_text("missing-package\n", encoding="utf-8")

            failure = subprocess.CalledProcessError(1, ["pip"], stderr="no matching distribution")
            with patch("cli_downloader.plugins.manager.subprocess.run", side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, "dependency installation failed"):
                    _install_plugin_dependencies(plugin_dir)

            self.assertFalse((plugin_dir / "deps").exists())


class InstallPluginTest(TestCase):
    def test_incompatible_core_version_is_rejected_before_install(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()
            url = _make_plugin_repo(
                repos,
                "future",
                "future-plugin",
                "1.0.0",
                requires_core_api=">=999.0.0",
            )

            with self.assertRaisesRegex(RuntimeError, "Plugin is incompatible"):
                install_plugin(plugins, url)

            self.assertFalse((plugins / "future-plugin").exists())

    def test_installs_to_versioned_directory(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "0.1.0")
            version = install_plugin(plugins, url)

            self.assertEqual(version, "0.1.0")
            self.assertTrue((plugins / "sample-plugin" / "0.1.0" / "plugin.json").exists())
            self.assertEqual(_read_active_marker(plugins / "sample-plugin"), "0.1.0")
            self.assertTrue((plugins / "sample-plugin" / "active").is_file())

    def test_second_install_same_version_raises(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "0.1.0")
            install_plugin(plugins, url)
            with self.assertRaises(RuntimeError):
                install_plugin(plugins, url)

    def test_id_differs_from_repo_name(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "myrepo", "actual-id", "2.0.0")
            version = install_plugin(plugins, url)

            self.assertEqual(version, "2.0.0")
            self.assertTrue((plugins / "actual-id" / "2.0.0").exists())

    def test_catalog_source_is_persisted_in_registry(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()
            url = _make_plugin_repo(repos, "sample", "sample-plugin", "0.1.0")

            install_plugin(
                plugins,
                url,
                source="community",
                catalog_id="community",
            )

            entry = load_registry(plugins)["sample-plugin"]
            self.assertEqual(entry["source"], "community")
            self.assertEqual(entry["catalog_id"], "community")


class UpdatePluginTest(TestCase):
    def test_updates_to_new_version(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "1.0.0")
            install_plugin(plugins, url)

            # Simulate a newer version by making a new repo
            url2 = _make_plugin_repo(repos, "sample-v2", "sample-plugin", "2.0.0")
            # The registry has the original URL, but update uses registry URL.
            # We need to update registry to point to the new URL for this test.
            from cli_downloader.plugins.registry import register_plugin

            register_plugin(plugins, "sample-plugin", url2)

            new_version = update_plugin(plugins, "sample-plugin")
            self.assertEqual(new_version, "2.0.0")
            self.assertEqual(_read_active_marker(plugins / "sample-plugin"), "2.0.0")
            self.assertTrue((plugins / "sample-plugin" / "1.0.0").exists())
            self.assertTrue((plugins / "sample-plugin" / "2.0.0").exists())

    def test_same_version_returns_none(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "1.0.0")
            install_plugin(plugins, url)

            self.assertIsNone(update_plugin(plugins, "sample-plugin"))


class CheckUpdateTest(TestCase):
    def test_new_version_available(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "1.0.0")
            install_plugin(plugins, url)
            # Register with a newer version URL (simulating remote update)
            from cli_downloader.plugins.registry import register_plugin

            url2 = _make_plugin_repo(repos, "sample-v2", "sample-plugin", "2.0.0")
            register_plugin(plugins, "sample-plugin", url2)

            self.assertTrue(check_plugin_update(plugins, "sample-plugin"))

    def test_no_update_for_current(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "1.0.0")
            install_plugin(plugins, url)
            self.assertFalse(check_plugin_update(plugins, "sample-plugin"))


class RollbackTest(TestCase):
    def test_rollback_to_previous_version(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url_v1 = _make_plugin_repo(repos, "v1", "sample-plugin", "1.0.0")
            url_v2 = _make_plugin_repo(repos, "v2", "sample-plugin", "2.0.0")

            install_plugin(plugins, url_v1)
            from cli_downloader.plugins.registry import register_plugin

            register_plugin(plugins, "sample-plugin", url_v2)
            update_plugin(plugins, "sample-plugin")

            prev = rollback_plugin(plugins, "sample-plugin")
            self.assertEqual(prev, "1.0.0")
            self.assertEqual(_read_active_marker(plugins / "sample-plugin"), "1.0.0")

    def test_single_version_returns_none(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "1.0.0")
            install_plugin(plugins, url)
            self.assertIsNone(rollback_plugin(plugins, "sample-plugin"))


class ListVersionsTest(TestCase):
    def test_lists_with_active_marker(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "2.0.0")
            install_plugin(plugins, url)

            versions = list_plugin_versions(plugins, "sample-plugin")
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["version"], "2.0.0")
            self.assertTrue(versions[0]["active"])

    def test_empty_for_uninstalled(self) -> None:
        with TemporaryDirectory() as d:
            plugins = Path(d) / "plugins"
            plugins.mkdir()
            self.assertEqual(list_plugin_versions(plugins, "unknown"), [])


class RemovePluginTest(TestCase):
    def test_removes_all_versions(self) -> None:
        with TemporaryDirectory() as d:
            repos = Path(d) / "repos"
            repos.mkdir()
            plugins = Path(d) / "plugins"
            plugins.mkdir()

            url = _make_plugin_repo(repos, "sample", "sample-plugin", "1.0.0")
            install_plugin(plugins, url)
            remove_plugin(plugins, "sample-plugin")

            self.assertFalse((plugins / "sample-plugin").exists())

    def test_missing_raises(self) -> None:
        with TemporaryDirectory() as d:
            plugins = Path(d) / "plugins"
            plugins.mkdir()
            with self.assertRaises(RuntimeError):
                remove_plugin(plugins, "unknown")


class PreparePluginSourceTest(TestCase):
    def test_zip_extraction_unwraps_single_folder(self) -> None:
        import zipfile

        with TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            zip_file = tmp_path / "test.zip"
            target_dir = tmp_path / "target"

            with zipfile.ZipFile(zip_file, "w") as zf:
                zf.writestr("subfolder/plugin.json", '{"plugin_id": "test"}')
                zf.writestr("subfolder/manifests/site.json", "{}")

            _extract_zip_archive(zip_file, target_dir)
            self.assertTrue((target_dir / "plugin.json").exists())
            self.assertTrue((target_dir / "manifests" / "site.json").exists())

    def test_skips_pip_install_when_deps_exist(self) -> None:
        with TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            (plugin_dir / "requirements.txt").write_text(
                "invalid_package_name_xyz_9999>=99.0", encoding="utf-8"
            )
            deps_dir = plugin_dir / "deps"
            deps_dir.mkdir()
            (deps_dir / "dummy.py").write_text("# vendor dep", encoding="utf-8")

            # Should return immediately without raising pip error
            _install_plugin_dependencies(plugin_dir)

    @patch("cli_downloader.plugins.manager.urllib.request.urlopen")
    def test_github_release_zip_auto_detection(self, mock_urlopen) -> None:
        from cli_downloader.plugins.manager import _find_github_latest_release_zip_url

        class FakeResponse:
            def __init__(self, data: bytes):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        api_payload = json.dumps(
            {
                "tag_name": "v1.0.0",
                "assets": [
                    {"name": "notes.txt", "browser_download_url": "https://example.test/notes.txt"},
                    {
                        "name": "douyin-plugin-v1.0.0.zip",
                        "browser_download_url": "https://example.test/douyin.zip",
                    },
                ],
            }
        ).encode("utf-8")

        mock_urlopen.side_effect = lambda req, timeout=15: FakeResponse(api_payload)

        url = "https://github.com/RyuuMeow/Douyin-Plugin"
        found_url = _find_github_latest_release_zip_url(url)
        self.assertEqual(found_url, "https://example.test/douyin.zip")
