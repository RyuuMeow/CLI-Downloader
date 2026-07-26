"""Plugin package loading helpers."""

from cli_downloader.plugins.activation import (
    ActivatedPluginPackage,
    PluginActivationReport,
    PluginActivationResult,
    PluginActivationStatus,
    activate_local_plugin_packages,
)
from cli_downloader.plugins.loader import (
    LocalPluginPackage,
    PluginScanReport,
    PluginScanResult,
    PluginScanStatus,
    scan_local_plugin_packages,
)
from cli_downloader.plugins.metadata import (
    PluginEntrypoint,
    PluginPackageMetadata,
    plugin_metadata_from_dict,
    plugin_metadata_from_json_file,
)
from cli_downloader.plugins.runtime import (
    ApplicationRuntime,
    RuntimeDiagnostic,
    RuntimeDiagnosticSeverity,
    RuntimePlugin,
    build_application_runtime,
)

__all__ = [
    "ActivatedPluginPackage",
    "ApplicationRuntime",
    "LocalPluginPackage",
    "PluginActivationReport",
    "PluginActivationResult",
    "PluginActivationStatus",
    "PluginEntrypoint",
    "PluginPackageMetadata",
    "PluginScanReport",
    "PluginScanResult",
    "PluginScanStatus",
    "RuntimeDiagnostic",
    "RuntimeDiagnosticSeverity",
    "RuntimePlugin",
    "activate_local_plugin_packages",
    "build_application_runtime",
    "plugin_metadata_from_dict",
    "plugin_metadata_from_json_file",
    "scan_local_plugin_packages",
]
