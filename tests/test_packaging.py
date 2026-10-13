from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackagingConfigurationTest(unittest.TestCase):
    def test_runtime_version_uses_the_single_project_version_source(self) -> None:
        from cli_downloader import __version__

        with (ROOT / "pyproject.toml").open("rb") as stream:
            project_version = tomllib.load(stream)["project"]["version"]
        spec = (ROOT / "Scripts" / "clid.spec").read_text(encoding="utf-8")

        self.assertEqual(__version__, project_version)
        self.assertIn('[(str(ROOT / "pyproject.toml"), ".")]', spec)

    def test_plugin_catalog_has_no_packaged_fallback_snapshot(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn('"cli_downloader.plugins.catalogs" = ["*.json"]', project)
        self.assertFalse((ROOT / "cli_downloader" / "plugins" / "catalogs").exists())

    def test_textual_styles_are_packaged_with_explicit_host_order(self) -> None:
        from cli_downloader.cli.presentation import CORE_PAGE_CSS, SHELL_CSS

        self.assertIn("#shell-title", SHELL_CSS)
        self.assertIn("#page-header", SHELL_CSS)
        self.assertLess(SHELL_CSS.index("#shell-title"), SHELL_CSS.index("#page-header"))
        self.assertIn(
            '"cli_downloader.cli.styles" = ["*.tcss"]',
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (ROOT / "cli_downloader" / "cli" / "styles" / "core_page.tcss").read_text(
                encoding="utf-8"
            ),
            CORE_PAGE_CSS,
        )
        self.assertIn(
            '"cli_downloader.cli.locales" = ["*.json"]',
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )

    def test_installer_is_per_user_and_owns_only_application_files(self) -> None:
        script = (ROOT / "Scripts" / "CLI-Downloader.iss").read_text(encoding="utf-8")

        self.assertIn("PrivilegesRequired=lowest", script)
        self.assertIn("DefaultDirName={localappdata}\\Programs\\CLI-Downloader", script)
        self.assertIn('Name: "addtopath"', script)
        self.assertIn("CurUninstallStepChanged", script)
        self.assertNotIn("[UninstallDelete]", script)

    def test_release_build_reuses_portable_bundle_and_publishes_checksums(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

        self.assertIn(
            'BuildPortable.ps1 -Python (Join-Path $env:pythonLocation "python.exe") '
            "-SkipVerification",
            workflow,
        )
        self.assertIn("BuildInstaller.ps1 -Version ($env:GITHUB_REF_NAME.Substring(1))", workflow)
        self.assertIn("choco install innosetup --no-progress --yes", workflow)
        self.assertIn("windows-x64-portable.zip", workflow)
        self.assertIn("windows-x64-setup.exe", workflow)
        self.assertIn("windows-x64.sha256", workflow)
        self.assertIn("--verify-tag", workflow)
