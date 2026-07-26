from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from secrets import token_urlsafe
from shutil import which
from tempfile import TemporaryDirectory
from time import monotonic

from cli_downloader.core.browser_auth import (
    browser_session_has_cookies,
    serialize_browser_session,
)
from cli_downloader.sites.manifest import BrowserAuthSchema


class BrowserLoginError(RuntimeError):
    pass


class BrowserLoginService:
    def __init__(
        self,
        *,
        profile_root: Path | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self._profile_root = profile_root
        self._timeout_seconds = timeout_seconds

    def capture(
        self,
        auth: BrowserAuthSchema,
        *,
        validator: Callable[[str], bool] | None = None,
    ) -> str:
        browsers = _browser_executables()
        if not browsers:
            raise BrowserLoginError("Microsoft Edge or Google Chrome is not installed.")
        if self._profile_root is not None:
            self._profile_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix="cli-downloader-auth-",
            dir=self._profile_root,
            ignore_cleanup_errors=True,
        ) as temporary:
            root = Path(temporary)
            result: dict[str, str] = {}
            token = token_urlsafe(24)
            server = HTTPServer(
                ("127.0.0.1", 0),
                _session_handler(result, token, auth, validator),
            )
            callback_url = f"http://127.0.0.1:{server.server_port}/{token}"
            extension = root / "extension"
            _write_extension(extension, auth, callback_url)
            process = _start_available_browser(
                browsers,
                root / "profile",
                extension,
                auth.login_url,
            )
            try:
                server.timeout = 0.5
                deadline = monotonic() + self._timeout_seconds
                bridge_deadline = monotonic() + 30.0
                while monotonic() < deadline:
                    server.handle_request()
                    if session := result.get("session"):
                        return session
                    if monotonic() >= bridge_deadline and "bridge_seen" not in result:
                        raise BrowserLoginError(
                            "The temporary login bridge did not start in this browser."
                        )
                    if process.poll() is not None:
                        raise BrowserLoginError(
                            "The login browser was closed before login completed."
                        )
                raise BrowserLoginError("Browser login timed out before a valid session was found.")
            finally:
                server.server_close()
                _stop_process(process)


def browser_session_is_authenticated(
    session: str,
    auth: BrowserAuthSchema,
    validator: Callable[[str], bool] | None = None,
) -> bool:
    return browser_session_has_cookies(session, auth.required_cookies) and (
        validator is None or validator(session)
    )


def _session_handler(result, token, auth, validator):
    class SessionHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != f"/{token}":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_000_000:
                    raise ValueError("invalid browser session size")
                state = json.loads(self.rfile.read(length))
                if not isinstance(state, dict):
                    raise ValueError("invalid browser session payload")
                session = serialize_browser_session(
                    state,
                    allowed_domains=auth.cookie_domains,
                    include_local_storage=auth.include_local_storage,
                )
                result["bridge_seen"] = "1"
                if browser_session_is_authenticated(session, auth, validator):
                    result["session"] = session
            except (ValueError, json.JSONDecodeError):
                self.send_error(400)
                return
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return SessionHandler


def _write_extension(path: Path, auth: BrowserAuthSchema, callback_url: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    domains = tuple(dict.fromkeys(domain.lstrip(".").lower() for domain in auth.cookie_domains))
    matches = [pattern for domain in domains for pattern in _domain_patterns(domain)]
    manifest = {
        "manifest_version": 3,
        "name": "CLI-Downloader Login Bridge",
        "version": "1.0.0",
        "permissions": ["cookies"],
        "host_permissions": [*matches, "http://127.0.0.1/*"],
        "background": {"service_worker": "background.js"},
        "content_scripts": [
            {
                "matches": matches,
                "js": ["content.js"],
                "run_at": "document_idle",
            }
        ],
    }
    (path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    config = json.dumps(
        {"callback": callback_url, "domains": domains},
        ensure_ascii=True,
    )
    (path / "background.js").write_text(
        _BACKGROUND_SCRIPT.replace("__CONFIG__", config),
        encoding="utf-8",
    )
    include_storage = "true" if auth.include_local_storage else "false"
    (path / "content.js").write_text(
        _CONTENT_SCRIPT.replace("__INCLUDE_STORAGE__", include_storage),
        encoding="utf-8",
    )


def _domain_patterns(domain: str) -> tuple[str, str]:
    return (f"https://{domain}/*", f"https://*.{domain}/*")


def _browser_executables() -> tuple[Path, ...]:
    candidates = [
        Path(value) / relative
        for variable, relative in (
            ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
            ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
            ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
            ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
        )
        if (value := os.environ.get(variable))
    ]
    candidates.extend(
        Path(path)
        for name in ("msedge", "chrome", "google-chrome", "chromium")
        if (path := which(name))
    )
    return tuple(dict.fromkeys(path for path in candidates if path.is_file()))


def _launch_browser(executable: Path, profile: Path, extension: Path, login_url: str):
    return subprocess.Popen(
        (
            str(executable),
            f"--user-data-dir={profile}",
            f"--disable-extensions-except={extension}",
            f"--load-extension={extension}",
            "--no-first-run",
            "--no-default-browser-check",
            login_url,
        ),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _start_available_browser(
    browsers: tuple[Path, ...],
    profile: Path,
    extension: Path,
    login_url: str,
):
    errors: list[str] = []
    for browser in browsers:
        try:
            return _launch_browser(browser, profile, extension, login_url)
        except OSError as error:
            errors.append(f"{browser.name}: {error}")
    raise BrowserLoginError("Could not start a login browser. " + " | ".join(errors))


def _stop_process(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


_BACKGROUND_SCRIPT = r"""
const CONFIG = __CONFIG__;
chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "probe") return;
  Promise.all(CONFIG.domains.map((domain) => chrome.cookies.getAll({domain})))
    .then((groups) => {
      const unique = new Map();
      for (const cookie of groups.flat()) {
        unique.set(`${cookie.name}\n${cookie.domain}\n${cookie.path}`, {
          name: cookie.name,
          value: cookie.value,
          domain: cookie.domain,
          path: cookie.path,
          expires: cookie.expirationDate ?? -1,
          httpOnly: cookie.httpOnly,
          secure: cookie.secure,
          sameSite: {strict: "Strict", lax: "Lax", no_restriction: "None"}[cookie.sameSite] ?? "Lax"
        });
      }
      return fetch(CONFIG.callback, {
        method: "POST",
        body: JSON.stringify({cookies: [...unique.values()], origins: message.origins ?? []})
      });
    })
    .catch(() => {});
});
""".strip()


_CONTENT_SCRIPT = r"""
const includeStorage = __INCLUDE_STORAGE__;
function probe() {
  let origins = [];
  if (includeStorage) {
    try {
      origins = [{
        origin: location.origin,
        localStorage: Object.entries(localStorage).map(([name, value]) => ({name, value}))
      }];
    } catch (_) {}
  }
  chrome.runtime.sendMessage({type: "probe", origins}, () => void chrome.runtime.lastError);
}
probe();
setInterval(probe, 1000);
""".strip()
