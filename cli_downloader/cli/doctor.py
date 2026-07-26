"""Actionable, deterministic local health checks for ``clid doctor``."""

from __future__ import annotations

import importlib.resources
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from cli_downloader.config.loader import ConfigLoader
from cli_downloader.core.environment import EnvironmentContext, diagnose_environment
from cli_downloader.plugins.loader import scan_fixed_plugin_packages
from cli_downloader.storage.state_database import StateDatabase


@dataclass(frozen=True)
class DoctorCheck:
    check_id: str
    severity: str
    status: str
    summary: str
    details: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.check_id,
            "severity": self.severity,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
        }


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def failed(self) -> bool:
        return any(check.status == "fail" for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": not self.failed,
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)


def run_doctor(
    *,
    environment: EnvironmentContext | None = None,
    config_loader: ConfigLoader | None = None,
    network: bool = False,
    network_probe: Callable[[str], str] | None = None,
) -> DoctorReport:
    environment = environment or diagnose_environment()
    paths = environment.paths
    checks: list[DoctorCheck] = []

    config = None
    try:
        config = (config_loader or ConfigLoader(paths=paths)).load()
        checks.append(_passed("config.load", "Configuration is valid."))
    except Exception as error:
        checks.append(_failed("config.load", "Configuration could not be loaded.", error))

    for path in (
        paths.config_dir,
        paths.cache_dir,
        paths.state_dir,
        paths.downloads_dir,
        paths.logs_dir,
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".clid-doctor-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as error:
            checks.append(
                _failed(
                    "storage.directories",
                    "Required application directories are not writable.",
                    error,
                )
            )
            break
    else:
        checks.append(_passed("storage.directories", "Required application directories are writable."))

    try:
        database = StateDatabase(paths.state_dir / "state.sqlite3")
        checks.append(
            _passed(
                "state.database",
                "State database opened successfully.",
                f"schema_version={database.schema_version}",
            )
        )
    except Exception as error:
        checks.append(_failed("state.database", "State database could not be opened.", error))

    try:
        report = scan_fixed_plugin_packages()
        invalid = tuple(result for result in report.results if result.status.value == "invalid")
        if invalid:
            checks.append(
                DoctorCheck(
                    "plugins.manifests",
                    "error",
                    "fail",
                    "One or more plugin manifests are invalid.",
                    "; ".join(str(result.error or result.folder) for result in invalid),
                )
            )
        else:
            checks.append(
                _passed(
                    "plugins.manifests",
                    "Plugin manifests are valid.",
                    f"valid_count={len(report.valid_packages)}",
                )
            )
    except Exception as error:
        checks.append(_failed("plugins.manifests", "Plugin manifests could not be scanned.", error))

    try:
        resource = importlib.resources.files("cli_downloader.cli.locales").joinpath("en-US.json")
        if not resource.is_file():
            raise FileNotFoundError(str(resource))
        checks.append(_passed("resources.packaged", "Packaged locale resources are available."))
    except Exception as error:
        checks.append(_failed("resources.packaged", "Packaged resources are incomplete.", error))

    if config is not None:
        client = str(config.values.get("torrent.client", "none"))
        if client == "qbittorrent":
            url = str(config.values.get("torrent.qbittorrent.url", ""))
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                checks.append(_failed("torrent.qbittorrent", "qBittorrent URL is invalid.", url))
            elif network:
                probe = network_probe or _probe_url
                try:
                    detail = probe(url)
                    checks.append(_passed("torrent.qbittorrent", "qBittorrent endpoint is reachable.", detail))
                except Exception as error:
                    checks.append(_failed("torrent.qbittorrent", "qBittorrent endpoint is unreachable.", error))
            else:
                checks.append(
                    DoctorCheck(
                        "torrent.qbittorrent",
                        "warning",
                        "warn",
                        "qBittorrent URL is valid; network check was skipped.",
                        "rerun with --network",
                    )
                )
        else:
            checks.append(_passed("torrent.qbittorrent", "No qBittorrent integration is configured."))

    if network:
        checks.append(_passed("network.enabled", "Network checks were explicitly enabled."))

    return DoctorReport(checks=tuple(checks))


def _probe_url(url: str) -> str:
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return f"http_status={response.status}"
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 404}:
            return f"http_status={error.code}"
        raise


def _passed(check_id: str, summary: str, details: object = "") -> DoctorCheck:
    return DoctorCheck(check_id, "info", "pass", summary, str(details))


def _failed(check_id: str, summary: str, error: object) -> DoctorCheck:
    return DoctorCheck(check_id, "error", "fail", summary, str(error).strip() or type(error).__name__)
