import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest import TestCase
from unittest.mock import patch

from cli_downloader.core.browser_auth import (
    browser_session_cookies,
    browser_session_has_cookies,
    cookie_header_for_url,
    serialize_browser_session,
)
from cli_downloader.integrations.browser_login import (
    _launch_browser,
    _write_extension,
    browser_session_is_authenticated,
)
from cli_downloader.sites.manifest import BrowserAuthSchema


class BrowserAuthTest(TestCase):
    @patch("cli_downloader.integrations.browser_login.subprocess.Popen")
    def test_login_uses_system_browser_with_temporary_extension(self, popen) -> None:
        _launch_browser(
            Path("msedge.exe"),
            Path("temporary-profile"),
            Path("login-extension"),
            "https://login.test",
        )

        command = popen.call_args.args[0]
        self.assertEqual(command[0], "msedge.exe")
        self.assertIn("--user-data-dir=temporary-profile", command)
        self.assertIn("--load-extension=login-extension", command)
        self.assertFalse(any("remote-debugging" in argument for argument in command))

    def test_login_extension_is_limited_to_declared_domains_and_localhost(self) -> None:
        auth = BrowserAuthSchema(
            login_url="https://accounts.pixiv.net/login",
            cookie_domains=("pixiv.net",),
            required_cookies=("PHPSESSID",),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory)
            _write_extension(path, auth, "http://127.0.0.1:32123/token")
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            background = (path / "background.js").read_text(encoding="utf-8")

        self.assertEqual(
            manifest["host_permissions"],
            [
                "https://pixiv.net/*",
                "https://*.pixiv.net/*",
                "http://127.0.0.1/*",
            ],
        )
        self.assertIn("http://127.0.0.1:32123/token", background)
        self.assertNotIn("recaptcha.net", background)

    def test_filters_domains_and_matches_cookie_request_rules(self) -> None:
        session = serialize_browser_session(
            {
                "cookies": [
                    {
                        "name": "PHPSESSID",
                        "value": "session",
                        "domain": ".pixiv.net",
                        "path": "/",
                        "expires": time() + 3600,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    },
                    {
                        "name": "tracking",
                        "value": "discarded",
                        "domain": ".tracker.test",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": False,
                        "secure": False,
                        "sameSite": "None",
                    },
                ],
                "origins": [],
            },
            allowed_domains=("pixiv.net",),
        )

        self.assertEqual(
            [cookie["name"] for cookie in browser_session_cookies(session)],
            ["PHPSESSID"],
        )
        self.assertTrue(browser_session_has_cookies(session, ("PHPSESSID",)))
        self.assertEqual(
            cookie_header_for_url(session, "https://www.pixiv.net/ajax/user/1"),
            "PHPSESSID=session",
        )
        self.assertIsNone(cookie_header_for_url(session, "https://i.pximg.net/image.jpg"))

        scoped = serialize_browser_session(
            {
                "cookies": [
                    {
                        "name": "scoped",
                        "value": "yes",
                        "domain": "www.pixiv.net",
                        "path": "/ajax",
                    }
                ]
            },
            allowed_domains=("pixiv.net",),
        )
        self.assertEqual(
            cookie_header_for_url(scoped, "https://www.pixiv.net/ajax/user/1"),
            "scoped=yes",
        )
        self.assertIsNone(cookie_header_for_url(scoped, "https://www.pixiv.net/ajaxx"))

    def test_plugin_validator_must_accept_login_session(self) -> None:
        auth = BrowserAuthSchema(
            login_url="https://accounts.pixiv.net/login",
            cookie_domains=("pixiv.net",),
            required_cookies=("PHPSESSID",),
        )
        session = serialize_browser_session(
            {
                "cookies": [
                    {
                        "name": "PHPSESSID",
                        "value": "anonymous",
                        "domain": ".pixiv.net",
                        "path": "/",
                    }
                ]
            },
            allowed_domains=("pixiv.net",),
        )

        self.assertFalse(browser_session_is_authenticated(session, auth, lambda value: False))
