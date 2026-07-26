from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

from cli_downloader.config.schema import SettingsRegistry
from cli_downloader.core.errors import PluginValidationError
from cli_downloader.core.fetch import AssetFetcher, UrlAssetFetcher
from cli_downloader.plugins.activation import (
    PluginActivationReport,
    PluginActivationStatus,
    activate_local_plugin_packages,
)
from cli_downloader.plugins.loader import (
    PluginScanReport,
    PluginScanStatus,
    scan_fixed_plugin_packages,
    scan_local_plugin_packages,
)
from cli_downloader.sites.base import SiteAdapter
from cli_downloader.sites.manifest import SiteManifest
from cli_downloader.sites.registry import SiteRegistry, hostname_from_url, normalize_hostname


class RuntimeDiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class RuntimeDiagnostic:
    code: str
    message: str
    severity: RuntimeDiagnosticSeverity
    plugin_id: str | None = None
    site_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity.value,
            "plugin_id": self.plugin_id,
            "site_id": self.site_id,
        }


@dataclass(frozen=True)
class RuntimePlugin:
    plugin_id: str
    instance: SiteAdapter
    manifest: SiteManifest
    asset_fetcher: AssetFetcher = field(default_factory=UrlAssetFetcher)
    external: bool = False

    @property
    def site_id(self) -> str:
        return self.manifest.site_id


@dataclass(frozen=True)
class ApplicationRuntime:
    scan_report: PluginScanReport
    activation_report: PluginActivationReport
    site_registry: SiteRegistry
    plugins_by_id: Mapping[str, RuntimePlugin]
    plugins_by_site_id: Mapping[str, RuntimePlugin]
    plugins_by_hostname: Mapping[str, RuntimePlugin]
    manifests_by_plugin_id: Mapping[str, SiteManifest]
    settings_registry: SettingsRegistry
    asset_fetchers_by_site_id: Mapping[str, AssetFetcher]
    diagnostics: tuple[RuntimeDiagnostic, ...]
    external_code_enabled: bool = False

    def plugin_for_id(self, plugin_id: str) -> SiteAdapter | None:
        entry = self.plugins_by_id.get(plugin_id)
        return entry.instance if entry is not None else None

    def plugin_for_site_id(self, site_id: str) -> SiteAdapter | None:
        entry = self.plugins_by_site_id.get(site_id)
        return entry.instance if entry is not None else None

    def plugin_for_url(self, url: str) -> SiteAdapter | None:
        entry = self.entry_for_url(url)
        return entry.instance if entry is not None else None

    def entry_for_url(self, url: str) -> RuntimePlugin | None:
        hostname = hostname_from_url(url)
        return self.plugins_by_hostname.get(hostname) if hostname is not None else None

    def manifest_for_plugin_id(self, plugin_id: str) -> SiteManifest | None:
        return self.manifests_by_plugin_id.get(plugin_id)

    def manifest_for_site_id(self, site_id: str) -> SiteManifest | None:
        entry = self.plugins_by_site_id.get(site_id)
        return entry.manifest if entry is not None else None

    def fetcher_for_site_id(self, site_id: str) -> AssetFetcher | None:
        return self.asset_fetchers_by_site_id.get(site_id)

    def configure_plugins(
        self,
        values: Mapping[str, object],
        secrets: Mapping[str, object],
    ) -> None:
        for plugin_id, entry in self.plugins_by_id.items():
            configure = getattr(entry.instance, "configure", None)
            if not callable(configure):
                continue
            context = self.settings_registry.context(
                plugin_id,
                values=dict(values),
                secrets=dict(secrets),
            )
            prefix = f"plugins.{plugin_id}."
            plugin_secrets = {
                key.removeprefix(prefix): value
                for key, value in secrets.items()
                if key.startswith(prefix)
            }
            configure(context.settings, plugin_secrets)

    def to_dict(self) -> dict[str, object]:
        return {
            "external_code_enabled": self.external_code_enabled,
            "plugin_ids": sorted(self.plugins_by_id),
            "site_ids": sorted(self.plugins_by_site_id),
            "hostnames": sorted(self.plugins_by_hostname),
            "scan": self.scan_report.to_dict(),
            "activation": self.activation_report.to_dict(),
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def build_application_runtime(
    *,
    plugin_roots: Sequence[str | Path] | None = None,
    enable_external_code: bool = False,
    application_root: str | Path | None = None,
    local_app_data: str | Path | None = None,
    builtin_plugins: Sequence[RuntimePlugin] = (),
) -> ApplicationRuntime:
    builtins = tuple(builtin_plugins)
    fixed_roots = plugin_roots is None
    scan_report = (
        scan_fixed_plugin_packages(
            application_root=application_root,
            local_app_data=local_app_data,
        )
        if fixed_roots
        else scan_local_plugin_packages(plugin_roots)
    )
    builtin_plugin_ids = tuple(entry.plugin_id for entry in builtins)
    builtin_site_ids = tuple(entry.site_id for entry in builtins)
    builtin_hostnames = tuple(domain for entry in builtins for domain in entry.manifest.domains)
    external_code_enabled = enable_external_code or fixed_roots
    if external_code_enabled:
        activation_report = activate_local_plugin_packages(
            scan_report.valid_packages,
            existing_plugin_ids=builtin_plugin_ids,
            existing_site_ids=builtin_site_ids,
            existing_hostnames=builtin_hostnames,
        )
    else:
        activation_report = PluginActivationReport(results=())

    entries = [*builtins]
    entries.extend(
        RuntimePlugin(
            plugin_id=activated.package.metadata.plugin_id,
            instance=cast(SiteAdapter, activated.instance),
            manifest=activated.package.manifest,
            asset_fetcher=_plugin_fetcher(activated.instance),
            external=True,
        )
        for activated in activation_report.activated_packages
    )
    site_registry = SiteRegistry()
    plugins_by_id: dict[str, RuntimePlugin] = {}
    plugins_by_site_id: dict[str, RuntimePlugin] = {}
    plugins_by_hostname: dict[str, RuntimePlugin] = {}
    manifests_by_plugin_id: dict[str, SiteManifest] = {}
    settings_registry = SettingsRegistry()
    asset_fetchers_by_site_id: dict[str, AssetFetcher] = {}
    for entry in entries:
        if entry.plugin_id in plugins_by_id:
            raise PluginValidationError(f"duplicate runtime plugin_id: {entry.plugin_id}")
        if entry.site_id in plugins_by_site_id:
            raise PluginValidationError(f"duplicate runtime site_id: {entry.site_id}")
        hostnames = tuple(normalize_hostname(domain) for domain in entry.manifest.domains)
        for hostname in hostnames:
            if hostname in plugins_by_hostname:
                raise PluginValidationError(f"duplicate runtime hostname: {hostname}")
        site_registry.register(entry.instance)
        plugins_by_id[entry.plugin_id] = entry
        plugins_by_site_id[entry.site_id] = entry
        manifests_by_plugin_id[entry.plugin_id] = entry.manifest
        plugins_by_hostname.update((hostname, entry) for hostname in hostnames)
        settings_registry.register_plugin(entry.plugin_id, entry.manifest)
        asset_fetchers_by_site_id[entry.site_id] = entry.asset_fetcher

    diagnostics = _runtime_diagnostics(
        scan_report,
        activation_report,
        external_code_enabled=external_code_enabled,
    )
    return ApplicationRuntime(
        scan_report=scan_report,
        activation_report=activation_report,
        site_registry=site_registry,
        plugins_by_id=MappingProxyType(plugins_by_id),
        plugins_by_site_id=MappingProxyType(plugins_by_site_id),
        plugins_by_hostname=MappingProxyType(plugins_by_hostname),
        manifests_by_plugin_id=MappingProxyType(manifests_by_plugin_id),
        settings_registry=settings_registry,
        asset_fetchers_by_site_id=MappingProxyType(asset_fetchers_by_site_id),
        diagnostics=diagnostics,
        external_code_enabled=external_code_enabled,
    )


def _plugin_fetcher(instance: object) -> AssetFetcher:
    return (
        cast(AssetFetcher, instance)
        if callable(getattr(instance, "fetch", None))
        else UrlAssetFetcher()
    )


def _runtime_diagnostics(
    scan_report: PluginScanReport,
    activation_report: PluginActivationReport,
    *,
    external_code_enabled: bool,
) -> tuple[RuntimeDiagnostic, ...]:
    diagnostics: list[RuntimeDiagnostic] = []
    for result in scan_report.results:
        if result.status is PluginScanStatus.INVALID:
            diagnostics.append(
                RuntimeDiagnostic(
                    code="plugin_scan_failed",
                    message=result.error or "Plugin scan failed",
                    severity=RuntimeDiagnosticSeverity.ERROR,
                )
            )
        elif result.status is PluginScanStatus.IGNORED and result.error:
            diagnostics.append(
                RuntimeDiagnostic(
                    code="plugin_source_overridden",
                    message=result.error,
                    severity=RuntimeDiagnosticSeverity.INFO,
                )
            )
    for package in scan_report.valid_packages:
        if package.requirements_path.is_file() and not package.dependency_path.is_dir():
            diagnostics.append(
                RuntimeDiagnostic(
                    code="plugin_dependencies_missing",
                    message=(
                        f"Runtime dependencies are not installed for plugin "
                        f"{package.metadata.plugin_id}; install the plugin or populate "
                        "its version-scoped deps directory"
                    ),
                    severity=RuntimeDiagnosticSeverity.WARNING,
                    plugin_id=package.metadata.plugin_id,
                    site_id=package.manifest.site_id,
                )
            )
    if not external_code_enabled:
        diagnostics.extend(
            RuntimeDiagnostic(
                code="external_plugin_disabled",
                message="External plugin passed static validation but code execution is disabled",
                severity=RuntimeDiagnosticSeverity.INFO,
                plugin_id=package.metadata.plugin_id,
                site_id=package.manifest.site_id,
            )
            for package in scan_report.valid_packages
        )
    for result in activation_report.results:
        if result.status is PluginActivationStatus.FAILED:
            diagnostics.append(
                RuntimeDiagnostic(
                    code="plugin_activation_failed",
                    message=result.error or "Plugin activation failed",
                    severity=RuntimeDiagnosticSeverity.ERROR,
                )
            )
    return tuple(diagnostics)
