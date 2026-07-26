from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cli_downloader.cli.rendering import RenderMode
from cli_downloader.cli.formatting import abbreviate_summary_title
from cli_downloader.cli.i18n import TranslationService, current_translator, translate_status
from cli_downloader.cli.table import TableColumn, TableSpec, print_rich_table
from cli_downloader.core.errors import CLIDownloaderError
from cli_downloader.core.models import Asset
from cli_downloader.core.operations import GalleryData
from cli_downloader.core.progress import byte_percentage
from cli_downloader.core.task_tree import TaskNode
from cli_downloader.plugins.activation import PluginActivationReport
from cli_downloader.plugins.loader import PluginScanResult
from cli_downloader.sites.manifest import SiteManifest
from cli_downloader.sites.registry import SiteRegistry

if TYPE_CHECKING:
    from cli_downloader.plugins.loader import PluginScanReport
    from cli_downloader.plugins.runtime import ApplicationRuntime
    from cli_downloader.application.task_hierarchy_service import TaskHierarchyService

ProgressSink = Callable[[str, Mapping[str, object]], None]
_EXPLICIT_DOWNLOAD_CONFIRMATIONS = ("live_network",)

_last_progress_emit: dict[str, float] = {}


def _report_progress(
    sink: ProgressSink | None,
    reason: str,
    payload: Mapping[str, object],
) -> None:
    if sink is not None:
        actual_reason = reason
        if reason == "batch_progress":
            actual_reason = str(payload.get("progress_reason", ""))

        if actual_reason == "byte_progress":
            asset_id = payload.get("asset_id")
            if isinstance(asset_id, str):
                now = time.monotonic()
                last = _last_progress_emit.get(asset_id, 0.0)
                if now - last < 0.1 and payload.get("bytes_received") != payload.get("total_bytes"):
                    return
                _last_progress_emit[asset_id] = now
        sink(reason, payload)


def _print_generic_download_error(
    data: GalleryData,
    *,
    render_mode: RenderMode,
    error: CLIDownloaderError,
    metadata: Mapping[str, object],
) -> int:
    print(
        json.dumps(
            {
                "command": "download",
                "requested_url": data.source_url,
                "render_mode": render_mode.value,
                "error_type": type(error).__name__,
                "error": str(error),
                **metadata,
            },
            indent=None if render_mode is RenderMode.JSON else 2,
        )
    )
    return 1


def _asset_to_dict(asset: Asset) -> dict[str, object]:
    return {
        "url": asset.url,
        "kind": asset.kind.value,
        "filename": asset.filename,
        "stage": asset.metadata.get("asset_stage"),
        "index": asset.metadata.get("index"),
    }


def _download_dry_run_to_dict(
    url: str,
    *,
    runtime: ApplicationRuntime,
) -> dict[str, object]:
    # Import locally to avoid importing the command parser during output-only
    # operations.
    from cli_downloader.cli._inspect_ops import route_url_through_manifests

    manifest, route = route_url_through_manifests(url, runtime=runtime)
    if route is None or manifest is None:
        return {
            "command": "download",
            "dry_run": True,
            "requested_url": url,
            "matched": False,
            "error_type": "UnsupportedInputUrl",
            "error": "No registered site manifest rule can handle this URL.",
            "network_request_performed": False,
        }

    route_payload = route.to_dict()
    return {
        "command": "download",
        "dry_run": True,
        "requested_url": url,
        "matched": True,
        "network_request_performed": False,
        **route_payload,
        "settings_keys": sorted(manifest.settings),
        "secret_keys": sorted(manifest.secrets),
    }


def _registered_site_manifests(runtime: ApplicationRuntime) -> tuple[SiteManifest, ...]:
    return tuple(runtime.manifests_by_plugin_id.values())


def _plugin_roots(roots: Sequence[str]) -> tuple[str, ...]:
    if roots:
        return tuple(roots)
    return ("plugins",)


def _plugin_list_to_dict(
    *,
    report: PluginScanReport,
    roots: Sequence[str],
) -> dict[str, object]:
    return {
        "command": "plugins list",
        "network_request_performed": False,
        "external_plugin_code_executed": False,
        "roots": list(roots),
        "valid_count": len(report.valid_packages),
        "results": [_plugin_scan_summary_to_dict(result) for result in report.results],
    }


def _plugin_inspect_to_dict(
    *,
    report: PluginScanReport,
    roots: Sequence[str],
) -> dict[str, object]:
    return {
        "command": "plugins inspect",
        "network_request_performed": False,
        "external_plugin_code_executed": False,
        "roots": list(roots),
        **report.to_dict(),
    }


def _plugin_activation_refusal_to_dict(
    *,
    roots: Sequence[str],
) -> dict[str, object]:
    return {
        "command": "plugins activate",
        "network_request_performed": False,
        "external_plugin_code_executed": False,
        "roots": list(roots),
        "error_type": "ExternalPluginCodeGateRequired",
        "error": "Pass --enable-external-code to import local plugin.py files.",
    }


def _plugin_activation_to_dict(
    *,
    scan_report: PluginScanReport,
    activation_report: PluginActivationReport,
    registry: SiteRegistry,
    roots: Sequence[str],
) -> dict[str, object]:
    external_site_ids = sorted(
        plugin.instance.site_id for plugin in activation_report.activated_packages
    )
    return {
        "command": "plugins activate",
        "network_request_performed": False,
        "external_plugin_code_executed": True,
        "roots": list(roots),
        "valid_count": len(scan_report.valid_packages),
        "registered_external_count": len(external_site_ids),
        "registered_external_site_ids": external_site_ids,
        "registry_site_count": len(registry.adapters),
        **activation_report.to_dict(),
    }


def _plugin_scan_summary_to_dict(result: PluginScanResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "folder": str(result.folder),
        "status": result.status.value,
    }
    if result.package is not None:
        payload["plugin_id"] = result.package.metadata.plugin_id
        payload["display_name"] = result.package.metadata.display_name
        payload["version"] = result.package.metadata.version
        payload["site_id"] = result.package.manifest.site_id
        payload["domains"] = list(result.package.manifest.domains)
    if result.error is not None:
        payload["error"] = result.error
    return payload


def _task_summary(node: TaskNode) -> dict[str, object]:
    return {
        "task_id": node.task_id,
        "type": node.kind.value,
        "name": node.title,
        "progress": _task_progress_text(node),
        "status": node.state.value,
        "created_at": node.details.get("created_at"),
    }


def _task_details(service: TaskHierarchyService, node: TaskNode) -> dict[str, object]:
    files = service.list_files(node.task_id) if node.kind.value == "item" else ()
    failure = node.details.get("failure")
    output_folder = None
    if node.output_files:
        output_folder = str(Path(node.output_files[0]).parent)
    return {
        **_task_summary(node),
        "site": node.site_id,
        "source": node.source_url,
        "output_folder": output_folder,
        "file_count": len(files) if files else len(node.output_files),
        "failed_files": sum(file.state == "failed" for file in files),
        "failure": failure,
    }


def _task_progress_text(node: TaskNode) -> str:
    total = "?" if node.progress.total_files is None else str(node.progress.total_files)
    progress = f"{node.progress.completed_files}/{total}"
    if node.progress.preparing_items:
        progress += f" +{node.progress.preparing_items} preparing"
    if node.progress.completed_bytes > 0 or node.progress.total_bytes is not None:
        completed = _format_bytes(node.progress.completed_bytes)
        if node.progress.total_bytes is not None:
            total_bytes = _format_bytes(node.progress.total_bytes)
            percent = byte_percentage(node.progress.completed_bytes, node.progress.total_bytes)
            progress += f" ({completed} / {total_bytes} {percent}%)"
        else:
            progress += f" ({completed})"
    return progress


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    return f"{value / (1024 * 1024 * 1024):.1f} GB"


def _task_created_at(node: TaskNode) -> datetime | None:
    value = node.details.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return created_at.replace(tzinfo=UTC) if created_at.tzinfo is None else created_at


def _print_task_list(
    nodes: tuple[TaskNode, ...],
    *,
    translator: TranslationService | None = None,
) -> None:
    translator = translator or current_translator()
    rows: list[tuple[str, ...]] = []
    for node in nodes:
        task_type = node.kind.value.capitalize()
        if node.site_id:
            task_type += f" ({node.site_id})"
        created_at = None
        value = node.details.get("created_at")
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                created_at = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        rows.append(
            (
                node.task_id,
                task_type,
                abbreviate_summary_title(node.title),
                translate_status(node.state.value, translator),
                _task_progress_text(node),
                created_at or "-",
            )
        )
    print_rich_table(
        TableSpec(
            title=translator("table.tasks"),
            columns=(
                TableColumn(translator("table.task_id"), style="cyan"),
                TableColumn(translator("table.type"), style="magenta"),
                TableColumn(translator("table.title"), style="green"),
                TableColumn(translator("table.status")),
                TableColumn(translator("table.progress")),
                TableColumn(translator("table.time"), style="blue"),
            ),
            rows=tuple(rows),
        )
    )
    print(f"{translator('table.total')}: {len(nodes)}")


def _print_task_detail(
    service: object,
    node: TaskNode,
    *,
    translator: TranslationService | None = None,
) -> None:
    translator = translator or current_translator()
    from cli_downloader.application.task_hierarchy_service import TaskHierarchyService

    if isinstance(service, TaskHierarchyService):
        files = service.list_files(node.task_id) if node.kind.value == "item" else ()
    else:
        files = ()

    rows: list[tuple[str, str]] = []

    created_at = None
    value = node.details.get("created_at")
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            created_at = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    rows.append((translator("table.task_id"), node.task_id))
    task_type = node.kind.value.capitalize()
    if node.site_id:
        task_type += f" ({node.site_id})"
    rows.extend(
        (
            (translator("table.type"), task_type),
            (translator("table.title"), node.title),
            (translator("table.status"), translate_status(node.state.value, translator)),
            (translator("table.progress"), _task_progress_text(node)),
            (translator("table.time"), created_at or "-"),
            (translator("table.source_url"), node.source_url or "-"),
        )
    )
    if node.output_files:
        rows.append((translator("table.output_folder"), str(Path(node.output_files[0]).parent)))
    file_count = len(files) if files else len(node.output_files)
    rows.append((translator("table.file_count"), str(file_count)))
    if files:
        failed = sum(f.state == "failed" for f in files)
        if failed:
            rows.append((translator("table.failed_files"), str(failed)))
    failure = node.details.get("failure")
    if failure:
        rows.append((translator("table.failure"), str(failure)))
    print_rich_table(
        TableSpec(
            title=f"{translator('table.task_detail')}: {node.task_id}",
            show_header=False,
            columns=(
                TableColumn("Key", style="cyan", justify="right", no_wrap=False),
                TableColumn("Value", style="green", no_wrap=False, overflow="fold"),
            ),
            rows=tuple(rows),
        )
    )
