import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.config.loader import ConfigLoader
from cli_downloader.config.schema import SettingsRegistry
from cli_downloader.core.browser_auth import serialize_browser_session
from cli_downloader.core.environment import AppPaths
from cli_downloader.core.errors import ConfigValidationError
from cli_downloader.sites.manifest import BrowserAuthSchema, SiteManifest


class ConfigLoaderTest(TestCase):
    def test_imports_legacy_download_limit_without_overriding_explicit_scheduler(self) -> None:
        for canonical in (None, 4):
            with self.subTest(canonical=canonical), TemporaryDirectory() as directory:
                paths = _paths(Path(directory))
                paths.config_dir.mkdir(parents=True)
                contents = "[download]\nmax_concurrent_galleries = 8\n"
                if canonical is not None:
                    contents += f"[scheduler]\nmax_concurrent_downloads = {canonical}\n"
                (paths.config_dir / "config.toml").write_text(contents, encoding="utf-8")
                loaded = ConfigLoader(paths=paths).load()
                self.assertEqual(
                    loaded.values["scheduler.max_concurrent_downloads"], canonical or 8
                )

    def test_loads_defaults_when_files_are_missing(self) -> None:
        with TemporaryDirectory() as directory:
            paths = _paths(Path(directory))

            loaded = ConfigLoader(paths=paths).load()

        self.assertEqual(loaded.values["download.strategy"], "auto")
        self.assertEqual(loaded.values["interface.language"], "en-US")
        self.assertEqual(loaded.secrets, {})
        self.assertEqual(loaded.sources, ())

    def test_loads_config_and_redacts_secrets(self) -> None:
        with TemporaryDirectory() as directory:
            paths = _paths(Path(directory))
            paths.config_dir.mkdir(parents=True)
            (paths.config_dir / "config.toml").write_text(
                "\n".join(
                    (
                        "[download]",
                        'strategy = "direct"',
                        "[example.http]",
                        'user_agent = "Config Agent"',
                        "timeout_seconds = 45.0",
                        "retry_attempts = 3",
                    )
                ),
                encoding="utf-8",
            )
            (paths.config_dir / "secrets.toml").write_text(
                '[example]\ncookie = "secret"\n',
                encoding="utf-8",
            )

            loaded = ConfigLoader(paths=paths).load()

        self.assertEqual(loaded.values["download.strategy"], "direct")
        self.assertEqual(loaded.values["example.http.user_agent"], "Config Agent")
        self.assertEqual(loaded.values["example.http.timeout_seconds"], 45.0)
        self.assertEqual(loaded.values["example.http.retry_attempts"], 3)
        self.assertEqual(loaded.secrets["example.cookie"], "secret")
        self.assertEqual(loaded.to_dict()["secrets"]["example.cookie"], "<redacted>")

    def test_loads_expired_browser_auth_session_for_reauthentication(self) -> None:
        with TemporaryDirectory() as directory:
            paths = _paths(Path(directory))
            session = serialize_browser_session(
                {
                    "cookies": [
                        {
                            "name": "PHPSESSID",
                            "value": "expired-session",
                            "domain": ".pixiv.net",
                            "path": "/",
                            "expires": 1,
                        }
                    ]
                },
                allowed_domains=("pixiv.net",),
            )
            _write_browser_session(paths, session)

            loaded = ConfigLoader(paths=paths, registry=_browser_auth_registry()).load()

        self.assertEqual(loaded.secrets["plugins.pixiv.auth.session"], session)

    def test_rejects_unsafe_browser_auth_sessions(self) -> None:
        disallowed_domain = json.dumps(
            {
                "cookies": [
                    {
                        "name": "PHPSESSID",
                        "value": "session",
                        "domain": ".example.test",
                        "path": "/",
                    }
                ]
            }
        )
        for session in ("not-json", disallowed_domain):
            with self.subTest(session=session), TemporaryDirectory() as directory:
                paths = _paths(Path(directory))
                _write_browser_session(paths, session)

                with self.assertRaises(ConfigValidationError):
                    ConfigLoader(paths=paths, registry=_browser_auth_registry()).load()


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        mode="test",
        data_root=root,
        config_dir=root / "config",
        cache_dir=root / "cache",
        state_dir=root / "state",
        downloads_dir=root / "downloads",
        logs_dir=root / "logs",
    )


def _browser_auth_registry() -> SettingsRegistry:
    registry = SettingsRegistry()
    registry.register_plugin(
        "pixiv",
        SiteManifest(
            site_id="pixiv",
            display_name="Pixiv",
            domains=("pixiv.net",),
            url_rules=(),
            browser_auth=BrowserAuthSchema(
                login_url="https://accounts.pixiv.net/login",
                cookie_domains=("pixiv.net",),
                required_cookies=("PHPSESSID",),
            ),
        ),
    )
    return registry


def _write_browser_session(paths: AppPaths, session: str) -> None:
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / "secrets.toml").write_text(
        f"[plugins.pixiv.auth]\nsession = {json.dumps(session)}\n",
        encoding="utf-8",
    )
