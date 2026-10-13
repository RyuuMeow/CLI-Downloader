from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


def _write_smoke_plugin(local_app_data: Path) -> None:
    plugin_root = local_app_data / "CLI-Downloader" / "plugins" / "smoke"
    version_root = plugin_root / "0.1.0"
    (version_root / "manifests").mkdir(parents=True)
    (plugin_root / "active").write_text("0.1.0", encoding="utf-8")
    (version_root / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_id": "smoke",
                "display_name": "Smoke",
                "version": "0.1.0",
                "entrypoint": "plugin.py:Plugin",
                "manifest": "site.json",
            }
        ),
        encoding="utf-8",
    )
    (version_root / "manifests" / "site.json").write_text(
        json.dumps(
            {
                "site_id": "smoke",
                "display_name": "Smoke",
                "domains": ["smoke.invalid"],
                "url_rules": [],
                "operations": {},
            }
        ),
        encoding="utf-8",
    )
    (version_root / "plugin.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from html.parser import HTMLParser",
                "from cli_downloader.sites.manifest import site_manifest_from_json_file",
                "HTMLParser()",
                "class Plugin:",
                "    site_id = 'smoke'",
                "    display_name = 'Smoke'",
                "    def get_manifest(self):",
                "        return site_manifest_from_json_file(Path(__file__).parent / 'manifests' / 'site.json')",
                "    def can_handle_url(self, url):",
                "        return False",
                "    def classify_url(self, url):",
                "        return None",
                "    async def resolve(self, request):",
                "        raise NotImplementedError",
                "    async def list_assets(self, item):",
                "        raise NotImplementedError",
                "    async def choose_strategy(self, item):",
                "        raise NotImplementedError",
                "    async def execute(self, request):",
                "        raise NotImplementedError",
            )
        ),
        encoding="utf-8",
    )


def _run(
    executable: Path,
    args: list[str],
    *,
    environment: dict[str, str],
    input_text: str | None = None,
) -> str:
    result = subprocess.run(
        [str(executable), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        input=input_text,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed with {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def verify_bundle(bundle: Path) -> None:
    executable = bundle / "CLI-Downloader.exe"
    if not executable.is_file():
        raise FileNotFoundError(f"portable executable not found: {executable}")
    dependency_installer = bundle / "CLI-Downloader.PluginInstaller.exe"
    if not dependency_installer.is_file():
        raise FileNotFoundError(f"plugin dependency installer not found: {dependency_installer}")
    if not (bundle / "clid.cmd").is_file():
        raise FileNotFoundError("clid.cmd forwarding command was not packaged")

    with tempfile.TemporaryDirectory(prefix="clid-portable-smoke-") as directory:
        root = Path(directory)
        data_root = root / "user-data"
        local_app_data = root / "local-app-data"
        environment = {
            **os.environ,
            "CLI_DOWNLOADER_DATA_ROOT": str(data_root),
            "LOCALAPPDATA": str(local_app_data),
            "NO_COLOR": "1",
        }

        _run(executable, ["--version"], environment=environment)
        help_started = time.perf_counter()
        _run(executable, ["--help"], environment=environment)
        help_seconds = time.perf_counter() - help_started
        # The first one-folder launch can pay for Windows Defender and DLL
        # extraction. Repeated launches are much faster, so keep this as a
        # regression guard without making cold-start CI flaky.
        if help_seconds > 10:
            raise RuntimeError(f"packaged --help startup exceeded 10 seconds: {help_seconds:.2f}s")

        diagnose = json.loads(_run(executable, ["diagnose"], environment=environment))
        if Path(diagnose["paths"]["data_root"]) != data_root:
            raise RuntimeError("packaged application ignored CLI_DOWNLOADER_DATA_ROOT")

        absent_status = json.loads(
            _run(executable, ["shell", "status", "--json"], environment=environment)
        )
        if absent_status.get("status") not in {"absent", "stale"}:
            raise RuntimeError(f"packaged Shell status was not initially absent: {absent_status}")

        defaults = json.loads(_run(executable, ["config", "defaults"], environment=environment))
        if "download.strategy" not in defaults:
            raise RuntimeError("packaged configuration defaults did not load")

        _write_smoke_plugin(local_app_data)
        sites = _run(executable, ["sites"], environment=environment)
        if "smoke" not in sites:
            raise RuntimeError("packaged runtime did not activate a per-user plugin")

        shell = _run(executable, [], environment=environment, input_text="exit\n")
        if "CLI-Downloader shell" not in shell:
            raise RuntimeError("packaged executable did not default to Shell mode")

        _run(
            executable,
            ["plugins", "list", "--root", str(local_app_data / "CLI-Downloader" / "plugins")],
            environment=environment,
        )

        pip_version = _run(dependency_installer, ["--version"], environment=environment)
        if not pip_version.startswith("pip "):
            raise RuntimeError("bundled plugin dependency installer is unavailable")

        payload_entries = {path.name for path in bundle.iterdir()}
        mutable_names = {"config", "cache", "state", "downloads", "logs", "plugins"}
        unexpected = sorted(payload_entries & mutable_names)
        if unexpected:
            raise RuntimeError(f"mutable data was written into application bundle: {unexpected}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test an extracted portable bundle.")
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    started = time.perf_counter()
    verify_bundle(args.bundle.resolve())
    print(f"Portable smoke test: PASS ({time.perf_counter() - started:.2f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
