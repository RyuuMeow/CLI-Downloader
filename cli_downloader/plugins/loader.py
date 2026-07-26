from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re
from typing import Iterable

from cli_downloader.core.environment import (
    application_root as discover_application_root,
    installed_plugins_root,
)
from cli_downloader.core.errors import PluginValidationError
from cli_downloader.plugins.metadata import (
    PluginPackageMetadata,
    metadata_compatibility_error,
    plugin_metadata_from_json_file,
)
from cli_downloader.sites.manifest import SiteManifest, site_manifest_from_json_file


class PluginScanStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    IGNORED = "ignored"


@dataclass(frozen=True)
class LocalPluginPackage:
    folder: Path
    metadata: PluginPackageMetadata
    manifest: SiteManifest

    @property
    def requirements_path(self) -> Path:
        return self.folder / "requirements.txt"

    @property
    def dependency_path(self) -> Path:
        return self.folder / "deps"

    def to_dict(self) -> dict[str, object]:
        return {
            "folder": str(self.folder),
            "plugin": self.metadata.to_dict(),
            "manifest": self.manifest.to_dict(),
        }


@dataclass(frozen=True)
class PluginScanResult:
    folder: Path
    status: PluginScanStatus
    package: LocalPluginPackage | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "folder": str(self.folder),
            "status": self.status.value,
        }
        if self.package is not None:
            payload["package"] = self.package.to_dict()
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class PluginScanReport:
    results: tuple[PluginScanResult, ...]

    @property
    def valid_packages(self) -> tuple[LocalPluginPackage, ...]:
        return tuple(
            result.package
            for result in self.results
            if result.status == PluginScanStatus.VALID and result.package is not None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "results": [result.to_dict() for result in self.results],
            "valid_count": len(self.valid_packages),
        }


def scan_local_plugin_packages(
    roots: Iterable[str | Path],
    *,
    include_ignored: bool = False,
) -> PluginScanReport:
    results: list[PluginScanResult] = []
    seen_plugin_ids: dict[str, Path] = {}
    seen_site_ids: dict[str, Path] = {}

    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            results.append(
                PluginScanResult(
                    folder=root_path,
                    status=PluginScanStatus.INVALID,
                    error="plugin root does not exist",
                )
            )
            continue
        if not root_path.is_dir():
            results.append(
                PluginScanResult(
                    folder=root_path,
                    status=PluginScanStatus.INVALID,
                    error="plugin root is not a directory",
                )
            )
            continue

        for plugin_folder in sorted(
            (child for child in root_path.iterdir() if child.is_dir()),
            key=lambda path: path.name.lower(),
        ):
            scan_result = _scan_plugin_folder(
                plugin_folder,
                seen_plugin_ids=seen_plugin_ids,
                seen_site_ids=seen_site_ids,
                include_ignored=include_ignored,
            )
            if scan_result is None:
                # The folder may be a versioned install (e.g. pixiv/0.1.0/plugin.json)
                # Try the active version sub-folder instead.
                version_folder = _active_version_folder(plugin_folder)
                if version_folder is not None:
                    scan_result = _scan_plugin_folder(
                        version_folder,
                        seen_plugin_ids=seen_plugin_ids,
                        seen_site_ids=seen_site_ids,
                        include_ignored=include_ignored,
                    )
            if scan_result is not None:
                results.append(scan_result)

    return PluginScanReport(results=tuple(results))


def scan_fixed_plugin_packages(
    *,
    application_root: str | Path | None = None,
    local_app_data: str | Path | None = None,
) -> PluginScanReport:
    """Scan project overrides and one active installed version per plugin."""
    app_root = (
        Path(application_root)
        if application_root is not None
        else discover_application_root()
    )
    project_report = _scan_existing_root(app_root / "Plugins")
    installed_report = _scan_installed_versions(
        installed_plugins_root(local_app_data=local_app_data)
    )
    project_ids = {package.metadata.plugin_id for package in project_report.valid_packages}
    results = [*project_report.results]
    for result in installed_report.results:
        package = result.package
        if package is not None and package.metadata.plugin_id in project_ids:
            results.append(
                PluginScanResult(
                    folder=result.folder,
                    status=PluginScanStatus.IGNORED,
                    error=(
                        f"installed plugin {package.metadata.plugin_id!r} is overridden "
                        "by the project Plugins directory"
                    ),
                )
            )
        else:
            results.append(result)
    return PluginScanReport(results=tuple(results))


def _scan_existing_root(root: Path) -> PluginScanReport:
    if not root.is_dir():
        return PluginScanReport(results=())
    return scan_local_plugin_packages((root,))


def _scan_installed_versions(root: Path) -> PluginScanReport:
    if not root.is_dir():
        return PluginScanReport(results=())
    results: list[PluginScanResult] = []
    seen_plugin_ids: dict[str, Path] = {}
    seen_site_ids: dict[str, Path] = {}
    for plugin_root in sorted(
        (child for child in root.iterdir() if child.is_dir()),
        key=lambda path: path.name.lower(),
    ):
        version_folder = _active_version_folder(plugin_root)
        if version_folder is None:
            continue
        result = _scan_plugin_folder(
            version_folder,
            seen_plugin_ids=seen_plugin_ids,
            seen_site_ids=seen_site_ids,
            include_ignored=True,
        )
        if result is not None:
            results.append(result)
    return PluginScanReport(results=tuple(results))


def _active_version_folder(plugin_root: Path) -> Path | None:
    versions = tuple(child for child in plugin_root.iterdir() if child.is_dir())
    if not versions:
        return None
    active_file = plugin_root / "active"
    if active_file.is_file():
        selected = plugin_root / active_file.read_text(encoding="utf-8").strip()
        if selected in versions:
            return selected
    return max(versions, key=lambda path: _version_key(path.name))


def _version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.findall(r"\d+|[^\d]+", version)
    )


def _scan_plugin_folder(
    plugin_folder: Path,
    *,
    seen_plugin_ids: dict[str, Path],
    seen_site_ids: dict[str, Path],
    include_ignored: bool,
) -> PluginScanResult | None:
    metadata_path = plugin_folder / "plugin.json"
    if not metadata_path.exists():
        if include_ignored:
            return PluginScanResult(
                folder=plugin_folder,
                status=PluginScanStatus.IGNORED,
                error="plugin.json not found",
            )
        return None

    try:
        metadata = plugin_metadata_from_json_file(metadata_path)
        compatibility_error = metadata_compatibility_error(metadata)
        if compatibility_error is not None:
            raise PluginValidationError(compatibility_error)
        manifest = site_manifest_from_json_file(metadata.manifest_path(plugin_folder))
        _reject_duplicate(
            key=metadata.plugin_id,
            seen=seen_plugin_ids,
            plugin_folder=plugin_folder,
            label="plugin_id",
        )
        _reject_duplicate(
            key=manifest.site_id,
            seen=seen_site_ids,
            plugin_folder=plugin_folder,
            label="site_id",
        )
    except PluginValidationError as exc:
        return PluginScanResult(
            folder=plugin_folder,
            status=PluginScanStatus.INVALID,
            error=str(exc),
        )

    package = LocalPluginPackage(
        folder=plugin_folder,
        metadata=metadata,
        manifest=manifest,
    )
    return PluginScanResult(
        folder=plugin_folder,
        status=PluginScanStatus.VALID,
        package=package,
    )


def _reject_duplicate(
    *,
    key: str,
    seen: dict[str, Path],
    plugin_folder: Path,
    label: str,
) -> None:
    previous = seen.get(key)
    if previous is not None:
        raise PluginValidationError(f"duplicate {label} {key!r}; already provided by {previous}")
    seen[key] = plugin_folder
