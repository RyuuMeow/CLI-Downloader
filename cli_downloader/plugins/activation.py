from __future__ import annotations

import importlib.util
import inspect
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Iterable

from cli_downloader.core.errors import PluginValidationError
from cli_downloader.plugins.loader import LocalPluginPackage
from cli_downloader.sites.manifest import SiteManifest
from cli_downloader.sites.registry import normalize_hostname


class PluginActivationStatus(StrEnum):
    ACTIVATED = "activated"
    FAILED = "failed"


@dataclass(frozen=True)
class ActivatedPluginPackage:
    package: LocalPluginPackage
    instance: object


@dataclass(frozen=True)
class PluginActivationResult:
    folder: Path
    status: PluginActivationStatus
    activated: ActivatedPluginPackage | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "folder": str(self.folder),
            "status": self.status.value,
        }
        if self.activated is not None:
            payload["plugin_id"] = self.activated.package.metadata.plugin_id
            payload["site_id"] = self.activated.package.manifest.site_id
            payload["class_name"] = self.activated.package.metadata.entrypoint.class_name
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class PluginActivationReport:
    results: tuple[PluginActivationResult, ...]

    @property
    def activated_packages(self) -> tuple[ActivatedPluginPackage, ...]:
        return tuple(
            result.activated
            for result in self.results
            if result.status == PluginActivationStatus.ACTIVATED and result.activated is not None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "results": [result.to_dict() for result in self.results],
            "activated_count": len(self.activated_packages),
        }


def activate_local_plugin_packages(
    packages: tuple[LocalPluginPackage, ...],
    *,
    existing_plugin_ids: Iterable[str] = (),
    existing_site_ids: Iterable[str] = (),
    existing_hostnames: Iterable[str] = (),
) -> PluginActivationReport:
    results: list[PluginActivationResult] = []
    seen_plugin_ids = set(existing_plugin_ids)
    reserved_site_ids = set(existing_site_ids)
    reserved_hostnames = {normalize_hostname(hostname) for hostname in existing_hostnames}
    for package in packages:
        if package.metadata.plugin_id in seen_plugin_ids:
            results.append(
                PluginActivationResult(
                    folder=package.folder,
                    status=PluginActivationStatus.FAILED,
                    error=(f"duplicate plugin_id during activation: {package.metadata.plugin_id}"),
                )
            )
            continue
        seen_plugin_ids.add(package.metadata.plugin_id)
        try:
            if package.manifest.site_id in reserved_site_ids:
                raise PluginValidationError(
                    f"adapter.site_id is already registered: {package.manifest.site_id}"
                )
            package_hostnames = tuple(
                normalize_hostname(hostname) for hostname in package.manifest.domains
            )
            if len(set(package_hostnames)) != len(package_hostnames):
                raise PluginValidationError("plugin manifest contains duplicate hostnames")
            duplicate_hostnames = reserved_hostnames.intersection(package_hostnames)
            if duplicate_hostnames:
                raise PluginValidationError(
                    f"plugin hostname is already registered: {sorted(duplicate_hostnames)[0]}"
                )
        except PluginValidationError as error:
            results.append(
                PluginActivationResult(
                    folder=package.folder,
                    status=PluginActivationStatus.FAILED,
                    error=str(error),
                )
            )
            continue
        result = _activate_package(
            package,
            reserved_site_ids=reserved_site_ids,
        )
        results.append(result)
        if result.status == PluginActivationStatus.ACTIVATED and result.activated is not None:
            reserved_site_ids.add(result.activated.package.manifest.site_id)
            reserved_hostnames.update(package_hostnames)
    return PluginActivationReport(results=tuple(results))


def _activate_package(
    package: LocalPluginPackage,
    *,
    reserved_site_ids: set[str],
) -> PluginActivationResult:
    try:
        module = _load_entrypoint_module(package)
        plugin_class = getattr(module, package.metadata.entrypoint.class_name, None)
        if plugin_class is None:
            raise PluginValidationError(
                f"entrypoint class not found: {package.metadata.entrypoint.class_name}"
            )
        instance = plugin_class()
        _verify_site_adapter(instance, package, reserved_site_ids=reserved_site_ids)
    except Exception as exc:
        return PluginActivationResult(
            folder=package.folder,
            status=PluginActivationStatus.FAILED,
            error=str(exc),
        )

    return PluginActivationResult(
        folder=package.folder,
        status=PluginActivationStatus.ACTIVATED,
        activated=ActivatedPluginPackage(package=package, instance=instance),
    )


def _load_entrypoint_module(package: LocalPluginPackage) -> ModuleType:
    entrypoint_path = package.metadata.entrypoint_path(package.folder).resolve()
    plugin_folder = package.folder.resolve()
    if entrypoint_path.parent != plugin_folder:
        raise PluginValidationError("entrypoint path must stay inside the plugin folder")
    if not entrypoint_path.exists():
        raise PluginValidationError(f"entrypoint file not found: {entrypoint_path}")

    # Keep the plugin folder importable for the lifetime of the loaded module.
    # External plugins may keep their implementation in a sibling package (for
    # example ``shared_client/``) and may import it lazily from an operation method.
    # The folder is version-scoped by the package loader, so this does not make
    # site-specific modules part of the Core package.
    plugin_path = str(plugin_folder)
    if plugin_path not in sys.path:
        sys.path.insert(0, plugin_path)
    dependency_path = package.folder / "deps"
    if dependency_path.is_dir():
        dependency_path_text = str(dependency_path.resolve())
        if dependency_path_text not in sys.path:
            sys.path.insert(0, dependency_path_text)

    module_name = (
        f"_cli_downloader_plugin_{package.metadata.plugin_id}_{abs(hash(str(entrypoint_path)))}"
    )
    spec = importlib.util.spec_from_file_location(module_name, entrypoint_path)
    if spec is None or spec.loader is None:
        raise PluginValidationError(f"cannot load entrypoint file: {entrypoint_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_site_adapter(
    instance: object,
    package: LocalPluginPackage,
    *,
    reserved_site_ids: set[str],
) -> None:
    site_id = getattr(instance, "site_id", None)
    if not isinstance(site_id, str) or site_id.strip() == "":
        raise PluginValidationError("adapter.site_id must be a non-empty string")
    if site_id != package.manifest.site_id:
        raise PluginValidationError(
            f"adapter.site_id must match JSON manifest site_id: {package.manifest.site_id}"
        )
    if site_id in reserved_site_ids:
        raise PluginValidationError(f"adapter.site_id is already registered: {site_id}")

    display_name = getattr(instance, "display_name", None)
    if not isinstance(display_name, str) or display_name.strip() == "":
        raise PluginValidationError("adapter.display_name must be a non-empty string")

    _require_callable(instance, "can_handle_url")
    _require_callable(instance, "classify_url")
    _require_async_callable(instance, "resolve")
    _require_async_callable(instance, "list_assets")
    _require_async_callable(instance, "choose_strategy")
    _require_async_callable(instance, "execute")
    fetch = getattr(instance, "fetch", None)
    if fetch is not None and (not callable(fetch) or not inspect.iscoroutinefunction(fetch)):
        raise PluginValidationError("adapter.fetch must be async callable when provided")
    configure = getattr(instance, "configure", None)
    if configure is not None and (
        not callable(configure) or inspect.iscoroutinefunction(configure)
    ):
        raise PluginValidationError("adapter.configure must be synchronous callable when provided")
    _verify_manifest_name_getter(instance, package)
    _verify_manifest_getter(instance, package)


def _require_callable(instance: object, name: str) -> None:
    method = getattr(instance, name, None)
    if not callable(method):
        raise PluginValidationError(f"adapter.{name} must be callable")


def _require_async_callable(instance: object, name: str) -> None:
    method = getattr(instance, name, None)
    if not callable(method) or not inspect.iscoroutinefunction(method):
        raise PluginValidationError(f"adapter.{name} must be async callable")


def _verify_manifest_name_getter(
    instance: object,
    package: LocalPluginPackage,
) -> None:
    getter = getattr(instance, "get_manifest_name", None)
    if getter is None:
        return
    if not callable(getter):
        raise PluginValidationError("get_manifest_name must be callable")
    manifest_name = getter()
    if manifest_name != package.metadata.manifest:
        raise PluginValidationError(
            f"get_manifest_name must match plugin.json manifest: {package.metadata.manifest}"
        )


def _verify_manifest_getter(
    instance: object,
    package: LocalPluginPackage,
) -> None:
    getter = getattr(instance, "get_manifest", None)
    if getter is None:
        return
    if not callable(getter):
        raise PluginValidationError("get_manifest must be callable")
    manifest = getter()
    if not isinstance(manifest, SiteManifest):
        raise PluginValidationError("get_manifest must return SiteManifest")
    if manifest.to_dict() != package.manifest.to_dict():
        raise PluginValidationError("get_manifest must match the JSON manifest data")
