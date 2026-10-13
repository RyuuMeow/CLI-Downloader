from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import contextmanager, nullcontext
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

from cli_downloader.application.download_service import DownloadService
from cli_downloader.application.download_limiter import GalleryDownloadLimiter
from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.application.task_lifecycle_service import TaskLifecycleService
from cli_downloader.cli._output import (
    ProgressSink,
    _EXPLICIT_DOWNLOAD_CONFIRMATIONS,
    _download_dry_run_to_dict,
    _plugin_activation_refusal_to_dict,
    _plugin_activation_to_dict,
    _plugin_inspect_to_dict,
    _plugin_list_to_dict,
    _plugin_roots,
    _print_task_detail,
    _print_task_list,
    _task_created_at,
    _task_details,
    _task_summary,
)
from cli_downloader.cli._download_ops import (
    AcceptanceSink,
    _build_download_limiter,
    ListSelectionPolicy,
    _batch_details,
    _run_batch_retry,
    _run_generic_download,
)
from cli_downloader.cli._owner_operations import forward_operation
from cli_downloader.storage.site_cooldown_store import SiteCooldownStore
from cli_downloader.cli import _inspect_ops
from cli_downloader.cli import _shell_wiring
from cli_downloader.cli.history import (
    history_records_to_dicts,
    print_history_record_detail,
    print_history_record_summary,
)
from cli_downloader.cli.doctor import run_doctor
from cli_downloader.cli.i18n import current_translator
from cli_downloader.cli.rendering import RenderMode, select_render_mode, serialize_operation_result
from cli_downloader.cli.table import TableColumn, TableSpec, print_rich_table
from cli_downloader.cli.textual_renderer import render_interactive_result
from cli_downloader.config.loader import ConfigLoader
from cli_downloader.core.environment import diagnose_environment, installed_plugins_root
from cli_downloader.about import __version__
from cli_downloader.core.errors import CLIDownloaderError, OwnerError
from cli_downloader.core.events import TaskEvent
from cli_downloader.core.history_records import DownloadHistoryRecord
from cli_downloader.core.batch import (
    BatchParent,
    BatchEntryStatus,
)
from cli_downloader.core.task_tree import TaskState
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerEventKind,
    OwnerHandshake,
    OwnerLifecycleState,
    OwnerMode,
    OwnerRequest,
    OwnerResponseMode,
    OwnerSource,
)
from cli_downloader.core.owner_scope import (
    OwnerDiscoveryMetadata,
    OwnerManager,
    OwnerScope,
    new_owner_session_id,
)
from cli_downloader.core.shell_control import (
    ShellController,
    ShellStatusKind,
    ShellStatusSnapshot,
)
from cli_downloader.core.owner_transport import OwnerPipeClient, OwnerTransportError
from cli_downloader.core.operations import (
    ListPageData,
    ResultKind,
)
from cli_downloader.plugins.catalog import PluginCatalogService
from cli_downloader.plugins.loader import (
    scan_local_plugin_packages,
)
from cli_downloader.plugins.manager import (
    _write_active_marker,
    install_plugin,
    list_plugin_versions,
    remove_plugin,
    rollback_plugin,
    update_plugin,
)
from cli_downloader.plugins.runtime import ApplicationRuntime, build_application_runtime
from cli_downloader.application.release_update_service import ReleaseUpdateService
from cli_downloader.sites.registry import SiteRegistry
from cli_downloader.storage.completion_store import CompletionStore
from cli_downloader.storage.paths import StorageLayout
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import TaskDesiredControl, TaskRecord, TaskStatus


# Transitional aliases keep the previous private helper imports working while
# the implementation lives in the focused inspection module.
_dispatch_operation = _inspect_ops.dispatch_operation
_dispatch_plugin_operation = _inspect_ops.dispatch_plugin_operation
_effective_strategy = _inspect_ops.effective_strategy
_operation_supports_strategy = _inspect_ops.operation_supports_strategy
_redacted_headers = _inspect_ops.redacted_headers
_route_url_through_manifests = _inspect_ops.route_url_through_manifests
_with_default_strategy = _inspect_ops.with_default_strategy
_recover_interrupted_downloads = _shell_wiring._recover_interrupted_downloads
build_interactive_shell = _shell_wiring.build_interactive_shell


@contextmanager
def _owned_execution(*, owner_claimed: bool, owner_mode: OwnerMode = OwnerMode.FOREGROUND):
    """Claim the process-wide execution scope for a live foreground action."""

    if owner_claimed:
        with nullcontext():
            yield
        return

    environment = diagnose_environment()
    scope = OwnerScope.from_app_paths(environment.paths)
    metadata = OwnerDiscoveryMetadata.for_current_process(
        scope,
        application_version=__version__,
        owner_mode=owner_mode,
        lifecycle_state=OwnerLifecycleState.BUSY,
        session_id=new_owner_session_id(),
        capabilities=("download", "task_control", "task_query", "history_query"),
    )
    with OwnerManager(scope).acquire(metadata):
        yield


def _emit_owner_error(error: OwnerError, *, json_output: bool) -> None:
    payload = {
        "error": str(error),
        "code": getattr(error, "code", "owner_error"),
    }
    if json_output:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Owner error [{payload['code']}]: {payload['error']}")


def _emit_shell_status(snapshot: ShellStatusSnapshot, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(snapshot.to_dict(), indent=2))
        return
    print(f"Shell: {snapshot.kind.value}")
    print(f"Status: {snapshot.message}")
    metadata = snapshot.metadata
    if metadata is not None:
        print(f"Owner mode: {metadata.owner_mode.value}")
        print(f"PID: {metadata.pid}")
        print(f"Version: {metadata.application_version}")
        print(f"Protocol: {metadata.protocol_version}")
        print(f"Session: {metadata.session_id}")
        print(f"Recovery: {metadata.lifecycle_state.value}")


def _forward_download_to_shell(args: argparse.Namespace) -> int | None:
    """Forward a live download when this data-root already has a Shell owner.

    ``None`` means no ready Shell was discovered and the caller may claim the
    foreground owner.  A concrete exit code means the request was handled by
    the Shell (including a structured IPC failure).
    """

    environment = diagnose_environment()
    scope = OwnerScope.from_app_paths(environment.paths)
    snapshot = ShellController(scope).status()
    metadata = snapshot.metadata
    if metadata is None or metadata.owner_mode is not OwnerMode.SHELL:
        return None
    if snapshot.kind not in {ShellStatusKind.READY, ShellStatusKind.RECOVERING}:
        return None
    if metadata.lifecycle_state not in {OwnerLifecycleState.READY, OwnerLifecycleState.RECOVERING}:
        return None

    response_mode = OwnerResponseMode.DETACH if args.detach else OwnerResponseMode.WAIT
    session_id = f"cli-{uuid4().hex}"
    selection_policy = ListSelectionPolicy.EXPLICIT.value if args.select else args.selection_policy
    payload: dict[str, object] = {
        "url": args.url,
        "force": bool(args.force),
        "selection_policy": selection_policy,
    }
    for name in ("profile", "strategy"):
        value = getattr(args, name, None)
        if value is not None:
            payload[name] = value
    if args.select:
        payload["select"] = tuple(args.select)
    request = OwnerRequest(
        protocol_version=OWNER_PROTOCOL_VERSION,
        request_id=uuid4().hex,
        source=OwnerSource.CLI,
        command=OwnerCommand.DOWNLOAD,
        response_mode=response_mode,
        payload=payload,
        client_session_id=session_id,
    )
    handshake = OwnerHandshake(
        protocol_version=OWNER_PROTOCOL_VERSION,
        session_id=session_id,
        mode=OwnerMode.FOREGROUND,
        application_version=__version__,
        capabilities=("download",),
        required_capabilities=("download",),
        scope_fingerprint=scope.scope_fingerprint,
    )
    try:
        events = OwnerPipeClient(scope).request(request, handshake=handshake)
    except OwnerTransportError as error:
        _emit_owner_error(error, json_output=args.json)
        return 1
    if not events:
        _emit_owner_error(
            OwnerTransportError("owner returned no response events"),
            json_output=args.json,
        )
        return 1
    accepted = next((event for event in events if event.kind is OwnerEventKind.ACCEPTED), None)
    if args.detach and accepted is not None:
        payload = {"accepted": True, "request_id": request.request_id, **dict(accepted.payload)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            task_id = payload.get("task_id") or "(pending)"
            print(f"Accepted by Shell: {task_id} ({request.request_id})")
        return 0
    terminal = next((event for event in events if event.kind is OwnerEventKind.TERMINAL), None)
    if terminal is None:
        error = next(
            (event for event in events if event.kind is OwnerEventKind.OWNER_DISCONNECTED), None
        )
        _emit_owner_error(
            OwnerTransportError(
                str(dict(error.payload))
                if error is not None
                else "owner disconnected before completion"
            ),
            json_output=args.json,
        )
        return 1
    result = terminal.payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("output"), str):
        output = result["output"]
        if output:
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
    elif args.json:
        print(json.dumps(dict(terminal.payload), ensure_ascii=False))
    else:
        print(f"Shell download {terminal.payload.get('status', 'finished')}")
    exit_code = terminal.payload.get("exit_code")
    return int(exit_code) if isinstance(exit_code, int) else 0


def _forward_task_control_to_shell(
    task_id: str,
    action: str,
    *,
    json_output: bool,
) -> int | None:
    """Forward one live Task control to the current Shell owner."""

    environment = diagnose_environment()
    scope = OwnerScope.from_app_paths(environment.paths)
    snapshot = ShellController(scope).status()
    metadata = snapshot.metadata
    if metadata is None or metadata.owner_mode is not OwnerMode.SHELL:
        return None
    if snapshot.kind not in {ShellStatusKind.READY, ShellStatusKind.RECOVERING}:
        return None
    if metadata.lifecycle_state not in {OwnerLifecycleState.READY, OwnerLifecycleState.RECOVERING}:
        return None
    session_id = f"cli-{uuid4().hex}"
    request = OwnerRequest(
        protocol_version=OWNER_PROTOCOL_VERSION,
        request_id=uuid4().hex,
        source=OwnerSource.CLI,
        command=OwnerCommand.TASK_CONTROL,
        response_mode=OwnerResponseMode.WAIT,
        payload={"task_id": task_id, "action": action},
        client_session_id=session_id,
    )
    handshake = OwnerHandshake(
        protocol_version=OWNER_PROTOCOL_VERSION,
        session_id=session_id,
        mode=OwnerMode.FOREGROUND,
        application_version=__version__,
        capabilities=("task_control",),
        required_capabilities=("task_control",),
        scope_fingerprint=scope.scope_fingerprint,
    )
    try:
        events = OwnerPipeClient(scope).request(request, handshake=handshake)
    except OwnerTransportError as error:
        _emit_owner_error(error, json_output=json_output)
        return 1
    terminal = next((event for event in events if event.kind is OwnerEventKind.TERMINAL), None)
    if terminal is None:
        _emit_owner_error(
            OwnerTransportError("owner disconnected before control acknowledgement"),
            json_output=json_output,
        )
        return 1
    result = terminal.payload.get("result")
    if json_output:
        print(
            json.dumps(
                dict(result) if isinstance(result, dict) else dict(terminal.payload),
                ensure_ascii=False,
            )
        )
    elif isinstance(result, dict) and isinstance(result.get("output"), str) and result["output"]:
        sys.stdout.write(result["output"])
        if not result["output"].endswith("\n"):
            sys.stdout.write("\n")
    else:
        print(f"Task {task_id}: {action} acknowledged")
    exit_code = terminal.payload.get("exit_code")
    return int(exit_code) if isinstance(exit_code, int) else 0


def _shell_launch_args(args: argparse.Namespace) -> tuple[str, ...]:
    values: list[str] = []
    for plugin_root in getattr(args, "plugin_root", ()):
        values.extend(("--plugin-root", plugin_root))
    if getattr(args, "runtime_enable_external_code", False):
        values.append("--enable-external-code")
    return tuple(values)


def _run_shell_command(args: argparse.Namespace) -> int:
    environment = diagnose_environment()
    scope = OwnerScope.from_app_paths(environment.paths)
    controller = ShellController(scope)
    command = getattr(args, "shell_command", None)
    if command == "status":
        snapshot = controller.status()
        _emit_shell_status(snapshot, json_output=args.json)
        return 1 if snapshot.kind is ShellStatusKind.INCOMPATIBLE else 0

    timeout = max(0.01, float(getattr(args, "timeout", 10.0)))
    if command == "ensure":
        result = controller.ensure(
            timeout=timeout,
            extra_args=_shell_launch_args(args),
        )
    else:
        result = controller.start(
            wait_ready=bool(getattr(args, "wait_ready", False)),
            timeout=timeout,
            extra_args=_shell_launch_args(args),
        )
    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _emit_shell_status(result.status, json_output=False)
        print(result.message)
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clid",
        description="Modular CLI download manager.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Print the installed CLI-Downloader version.",
    )
    parser.add_argument(
        "--plugin-root",
        action="append",
        default=[],
        help="Plugin root scanned during application startup. Can be repeated.",
    )
    parser.add_argument(
        "--enable-external-code",
        dest="runtime_enable_external_code",
        action="store_true",
        help="Allow validated external plugins to execute during application startup.",
    )
    subparsers = parser.add_subparsers(dest="command")

    network_parser = subparsers.add_parser("network", help="Inspect live owner HTTP admission.")
    network_parser.add_argument("network_command", choices=("status",))
    network_parser.add_argument("--json", action="store_true", help="Emit JSON diagnostics.")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run actionable local health checks.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable check results.",
    )
    doctor_parser.add_argument(
        "--network",
        action="store_true",
        help="Enable configured integration and remote endpoint checks.",
    )

    subparsers.add_parser(
        "diagnose",
        help="Print environment, path, and terminal capability information.",
    )
    subparsers.add_parser(
        "sites",
        help="List registered site adapters.",
    )
    shell_parser = subparsers.add_parser(
        "shell",
        help="Inspect or start the single visible Shell owner.",
    )
    shell_subparsers = shell_parser.add_subparsers(dest="shell_command")
    shell_status_parser = shell_subparsers.add_parser(
        "status",
        help="Inspect the current Shell owner without starting one.",
    )
    shell_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable status.",
    )
    shell_start_parser = shell_subparsers.add_parser(
        "start",
        help="Start a visible Shell when no owner is active.",
    )
    shell_start_parser.add_argument(
        "--visible",
        action="store_true",
        required=True,
        help="Open the Shell in a new visible console window.",
    )
    shell_start_parser.add_argument(
        "--wait-ready",
        action="store_true",
        help="Wait until the Shell advertises that it is ready.",
    )
    shell_start_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Maximum readiness wait (default: 10).",
    )
    shell_start_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable result.",
    )
    shell_ensure_parser = shell_subparsers.add_parser(
        "ensure",
        help="Idempotently start a visible Shell and wait until it is ready.",
    )
    shell_ensure_parser.add_argument(
        "--visible",
        action="store_true",
        required=True,
        help="Open the Shell in a new visible console window when needed.",
    )
    shell_ensure_parser.add_argument(
        "--wait-ready",
        action="store_true",
        required=True,
        help="Wait until recovery and plugin loading have completed.",
    )
    shell_ensure_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Maximum readiness wait (default: 10).",
    )
    shell_ensure_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable result.",
    )
    plugins_parser = subparsers.add_parser(
        "plugins",
        help="Inspect local plugin packages without executing plugin code.",
    )
    plugins_subparsers = plugins_parser.add_subparsers(dest="plugins_command")
    plugins_list_parser = plugins_subparsers.add_parser(
        "list",
        help="List local plugin package diagnostics.",
    )
    plugins_list_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Plugin root directory to scan. Can be provided more than once.",
    )
    plugins_list_parser.add_argument(
        "--include-ignored",
        action="store_true",
        help="Include direct child folders that do not contain plugin.json.",
    )
    plugins_inspect_parser = plugins_subparsers.add_parser(
        "inspect",
        help="Print full local plugin package diagnostics.",
    )
    plugins_inspect_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Plugin root directory to scan. Can be provided more than once.",
    )
    plugins_inspect_parser.add_argument(
        "--include-ignored",
        action="store_true",
        help="Include direct child folders that do not contain plugin.json.",
    )
    plugins_activate_parser = plugins_subparsers.add_parser(
        "activate",
        help="Explicitly import local plugin packages after validation.",
    )
    plugins_activate_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Plugin root directory to scan. Can be provided more than once.",
    )
    plugins_activate_parser.add_argument(
        "--enable-external-code",
        action="store_true",
        help="Required confirmation before external plugin.py files are imported.",
    )
    plugins_remote_parser = plugins_subparsers.add_parser(
        "remote",
        help="List available plugins from remote marketplace.",
    )
    plugins_remote_parser.add_argument(
        "--force",
        action="store_true",
        help="Force a live network fetch from the remote catalog.",
    )
    plugins_remote_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )
    plugins_refresh_parser = plugins_subparsers.add_parser(
        "refresh",
        help="Force refresh the remote marketplace plugin catalog cache.",
    )
    plugins_refresh_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )

    plugins_install_parser = plugins_subparsers.add_parser(
        "install",
        help="Install a plugin from a Git URL or catalog ID.",
    )
    plugins_install_parser.add_argument(
        "target",
        help="Git URL or catalog plugin ID.",
    )
    plugins_install_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Target plugins directory.",
    )
    plugins_install_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )
    plugins_update_parser = plugins_subparsers.add_parser(
        "update",
        help="Update an installed plugin to its latest version.",
    )
    plugins_update_parser.add_argument(
        "plugin_id",
        help="Plugin ID to update.",
    )
    plugins_update_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Target plugins directory.",
    )
    plugins_update_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )
    plugins_remove_parser = plugins_subparsers.add_parser(
        "remove",
        help="Remove an installed plugin.",
    )
    plugins_remove_parser.add_argument(
        "plugin_id",
        help="Plugin ID to remove.",
    )
    plugins_remove_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Target plugins directory.",
    )
    plugins_remove_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )
    plugins_rollback_parser = plugins_subparsers.add_parser(
        "rollback",
        help="Rollback a plugin to its previous installed version.",
    )
    plugins_rollback_parser.add_argument(
        "plugin_id",
        help="Plugin ID to rollback.",
    )
    plugins_rollback_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Target plugins directory.",
    )
    plugins_rollback_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )
    plugins_switch_parser = plugins_subparsers.add_parser(
        "switch",
        help="Switch active version of an installed plugin.",
    )
    plugins_switch_parser.add_argument(
        "plugin_id",
        help="Plugin ID to switch.",
    )
    plugins_switch_parser.add_argument(
        "version",
        help="Version string to set as active.",
    )
    plugins_switch_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Target plugins directory.",
    )
    plugins_switch_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )
    plugins_versions_parser = plugins_subparsers.add_parser(
        "versions",
        help="List installed versions of a plugin.",
    )
    plugins_versions_parser.add_argument(
        "plugin_id",
        help="Plugin ID to inspect.",
    )
    plugins_versions_parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Target plugins directory.",
    )
    plugins_versions_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )

    download_parser = subparsers.add_parser(
        "download",
        help="Route a URL through the site manifest contract.",
    )
    download_parser.add_argument(
        "url",
        help="Supported item, page, listing, search, or batch URL.",
    )
    download_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify the URL and show the planned site operation without downloading.",
    )
    download_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON and never prompt for interaction.",
    )
    download_parser.add_argument(
        "--profile",
        help="Profile name passed to the selected site operation.",
    )
    download_parser.add_argument(
        "--strategy",
        choices=("auto", "direct", "torrent"),
        help="Download strategy passed to the selected site operation.",
    )
    download_parser.add_argument(
        "--select",
        action="append",
        default=[],
        metavar="ITEM_ID",
        help="Restrict a list Batch to this item ID. Can be repeated; omitted means all returned items.",
    )
    download_parser.add_argument(
        "--selection-policy",
        choices=tuple(policy.value for policy in ListSelectionPolicy),
        default=ListSelectionPolicy.ALL.value,
        help=argparse.SUPPRESS,
    )
    download_parser.add_argument(
        "--force",
        action="store_true",
        help="Create a new batch attempt even when a selected gallery is active or completed.",
    )
    response_mode = download_parser.add_mutually_exclusive_group()
    response_mode.add_argument(
        "--detach",
        action="store_true",
        help="Submit to an active visible Shell owner and return after durable acceptance (not final success).",
    )
    response_mode.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the final download result through the Shell owner or a foreground owner (the default).",
    )

    operation_parser = subparsers.add_parser(
        "operation",
        help="Run a plugin operation directly.",
    )
    operation_parser.add_argument("plugin_id")
    operation_parser.add_argument("operation_id", nargs="?", default="help")
    operation_parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Operation parameters in key=value format.",
    )
    operation_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON and never prompt for interaction.",
    )

    batches_parser = subparsers.add_parser(
        "batches",
        help="Inspect, resume, or retry persisted batch parents.",
    )
    batches_subparsers = batches_parser.add_subparsers(dest="batches_command")
    batches_show_parser = batches_subparsers.add_parser(
        "show",
        help="Show a batch parent, entries, children, assets, and attempts.",
    )
    batches_show_parser.add_argument("parent_id")
    batches_retry_parser = batches_subparsers.add_parser(
        "retry",
        help="Retry failed batch work from its persisted checkpoint.",
    )
    batches_retry_parser.add_argument("parent_id")
    batches_retry_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON and never prompt for interaction.",
    )
    batches_retry_parser.add_argument(
        "--profile",
        help="Profile passed to plugin resolution when package refresh is required.",
    )
    batches_resume_parser = batches_subparsers.add_parser(
        "resume",
        help="Resume interrupted batch work without converting it to a retry.",
    )
    batches_resume_parser.add_argument("parent_id")
    batches_resume_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON and never prompt for interaction.",
    )
    batches_resume_parser.add_argument(
        "--profile",
        help="Profile passed to plugin resolution when package refresh is required.",
    )
    tasks_parser = subparsers.add_parser(
        "tasks",
        help="List or transition tasks persisted in the local task store.",
    )
    tasks_subparsers = tasks_parser.add_subparsers(dest="tasks_command")
    tasks_list_parser = tasks_subparsers.add_parser(
        "list",
        help="List tasks persisted in the local task store.",
    )
    tasks_parser.add_argument(
        "--status",
        choices=tuple(state.value for state in TaskState),
        help="Only show tasks with this status.",
    )
    tasks_list_parser.add_argument(
        "--status",
        choices=tuple(state.value for state in TaskState),
        help="Only show tasks with this status.",
    )
    tasks_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data instead of a summary table.",
    )
    tasks_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data instead of a summary table.",
    )
    tasks_retry_parser = tasks_subparsers.add_parser(
        "retry",
        help="Move a retryable task back to planned.",
    )
    tasks_retry_parser.add_argument("task_id")
    tasks_retry_parser.add_argument("--json", action="store_true")
    tasks_resume_parser = tasks_subparsers.add_parser(
        "resume",
        help="Move a resumable task back to planned.",
    )
    tasks_resume_parser.add_argument("task_id")
    tasks_resume_parser.add_argument("--json", action="store_true")
    tasks_pause_parser = tasks_subparsers.add_parser(
        "pause",
        help="Pause an Item or Batch task through the active owner.",
    )
    tasks_pause_parser.add_argument("task_id")
    tasks_pause_parser.add_argument("--json", action="store_true")
    tasks_show_parser = tasks_subparsers.add_parser(
        "show",
        help="Show user-facing details for one Item or Batch.",
    )
    tasks_show_parser.add_argument("task_id")
    tasks_delete_parser = tasks_subparsers.add_parser(
        "delete",
        help="Delete matching task records while preserving downloaded files.",
    )
    tasks_delete_parser.add_argument("task_ids", nargs="*")
    tasks_delete_parser.add_argument(
        "--status",
        choices=tuple(state.value for state in TaskState),
        help="Only match tasks with this status.",
    )
    tasks_delete_parser.add_argument(
        "--older-than",
        type=float,
        metavar="DAYS",
        help="Only match tasks created at least this many days ago.",
    )
    tasks_delete_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview matching records without deleting them.",
    )
    tasks_delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm deletion without an interactive prompt.",
    )
    tasks_delete_parser.add_argument(
        "--all",
        action="store_true",
        help="Delete all task records (requires --yes).",
    )
    tasks_hard_retry_parser = tasks_subparsers.add_parser(
        "hard-retry",
        help="Delete and completely rebuild a task from its source URL.",
    )
    tasks_hard_retry_parser.add_argument("task_id")
    tasks_hard_retry_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of a summary.",
    )
    tasks_cancel_parser = tasks_subparsers.add_parser(
        "cancel",
        help="Cancel tasks (stop running downloads and mark as canceled).",
    )
    tasks_cancel_parser.add_argument("task_id", nargs="?")
    tasks_cancel_parser.add_argument(
        "--all",
        action="store_true",
        help="Cancel all cancellable tasks (requires --yes).",
    )
    tasks_cancel_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm cancellation without an interactive prompt.",
    )
    tasks_cancel_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of a summary.",
    )
    history_parser = subparsers.add_parser(
        "history",
        help="List durable download records persisted in the local history store.",
    )
    history_parser.add_argument(
        "--page",
        type=int,
        default=1,
        help="Page number to display (default: 1).",
    )
    history_parser.add_argument(
        "--query",
        type=str,
        default="",
        help="Filter events by keyword matching title or plugin/operation.",
    )
    history_parser.add_argument(
        "--json",
        action="store_true",
        help="Output the same history record projection as JSON.",
    )
    history_parser.add_argument(
        "--child",
        action="store_true",
        help="Include child tasks in history listing",
    )
    history_subparsers = history_parser.add_subparsers(dest="history_command")
    history_show_parser = history_subparsers.add_parser(
        "show",
        help="Show one durable history record.",
    )
    history_show_parser.add_argument("task_id")
    history_show_parser.add_argument("--page", type=int, default=1)
    history_show_parser.add_argument("--query", default="")
    history_show_parser.add_argument("--json", action="store_true")
    history_show_parser.add_argument("--child", action="store_true")
    history_delete_parser = history_subparsers.add_parser(
        "delete",
        help="Delete task events and download records from history.",
    )
    history_delete_parser.add_argument("task_id", nargs="?")
    history_delete_parser.add_argument("--all", action="store_true", help="Delete all history.")
    history_delete_parser.add_argument(
        "--older-than",
        type=float,
        help="Delete history older than X days.",
    )
    history_delete_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Confirm deletion without an interactive prompt.",
    )
    config_parser = subparsers.add_parser(
        "config",
        help="Inspect config defaults or the effective loaded config.",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_subparsers.add_parser(
        "defaults",
        help="Print schema defaults.",
    )
    config_subparsers.add_parser(
        "show",
        help="Print the effective config loaded from the app config directory.",
    )
    check_update_parser = subparsers.add_parser(
        "check-update",
        help="Check for published application release updates.",
    )
    check_update_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON data.",
    )
    return parser


def _run_local_plugin_operation(args, runtime, download_limiter=None) -> int:
    render_mode = select_render_mode(json_requested=args.json)
    environment = diagnose_environment()
    loaded_config = ConfigLoader(
        paths=environment.paths,
        registry=runtime.settings_registry,
    ).load()
    runtime.configure_plugins(loaded_config.values, loaded_config.secrets)

    if args.operation_id != "help":
        limiter = download_limiter or _build_download_limiter(runtime, loaded_config.values)
        runtime.bind_http_services(limiter)
        limiter.requests.bind_cooldown_store(
            SiteCooldownStore(
                StateDatabase(StorageLayout.from_app_paths(environment.paths).state_db)
            )
        )

    dispatcher = OperationDispatcher(
        runtime,
        config=loaded_config,
    )
    params = {}
    for arg in args.args:
        if "=" in arg:
            k, v = arg.split("=", 1)
            params[k] = v

    result = _dispatch_plugin_operation(
        dispatcher,
        args.plugin_id,
        args.operation_id,
        params=params,
        confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
    )
    if result.result_kind is ResultKind.ERROR:
        print(
            serialize_operation_result(
                result,
                RenderMode.PLAIN if render_mode is RenderMode.INTERACTIVE else render_mode,
            )
        )
        return 1

    if result.result_kind is not ResultKind.LIST_PAGE:
        print(
            serialize_operation_result(
                result,
                RenderMode.PLAIN if render_mode is RenderMode.INTERACTIVE else render_mode,
            )
        )
        return 0

    data = result.data
    if not isinstance(data, ListPageData):
        print(
            serialize_operation_result(
                result,
                RenderMode.PLAIN if render_mode is RenderMode.INTERACTIVE else render_mode,
            )
        )
        return 1

    print(serialize_operation_result(result, render_mode))
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: ApplicationRuntime | None = None,
    cancellation: Event | None = None,
    progress: ProgressSink | None = None,
    download_limiter: GalleryDownloadLimiter | None = None,
    task_service: TaskHierarchyService | None = None,
    _owner_claimed: bool = False,
    on_accept: AcceptanceSink | None = None,
) -> int:
    if argv is None:
        import sys

        effective_argv = sys.argv[1:] or ["shell"]
    else:
        effective_argv = list(argv)

    parser = build_parser()
    try:
        args = parser.parse_args(effective_argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    application_runtime = runtime

    def get_runtime() -> ApplicationRuntime:
        nonlocal application_runtime
        if application_runtime is None:
            application_runtime = build_application_runtime(
                plugin_roots=(tuple(args.plugin_root) if args.plugin_root else None),
                enable_external_code=args.runtime_enable_external_code,
            )
        return application_runtime

    if args.command == "network":
        from cli_downloader.cli._network_status import print_network_status

        return print_network_status()

    if args.command == "diagnose":
        print(json.dumps(diagnose_environment().to_dict(), indent=2))
        return 0

    if args.command == "doctor":
        environment = diagnose_environment()
        report = run_doctor(
            environment=environment,
            config_loader=ConfigLoader(paths=environment.paths),
            network=args.network,
        )
        if args.json:
            print(report.to_json())
        else:
            for check in report.checks:
                print(f"[{check.status.upper():4}] {check.check_id}: {check.summary}")
                if check.details:
                    print(f"       {check.details}")
            print("Doctor: " + ("failed" if report.failed else "passed"))
        return 1 if report.failed else 0

    if args.command == "sites":
        for adapter in get_runtime().site_registry.adapters:
            print(f"{adapter.site_id}\t{adapter.display_name}")
        return 0

    if args.command == "shell":
        if getattr(args, "shell_command", None) is not None:
            return _run_shell_command(args)
        shell_runtime = get_runtime()
        plugins_dir = Path(args.plugin_root[0]) if args.plugin_root else installed_plugins_root()
        return build_interactive_shell(
            runtime=shell_runtime,
            plugins_dir=plugins_dir,
            runner=lambda shell_argv: main(
                shell_argv,
                runtime=shell_runtime,
                _owner_claimed=True,
            ),
            background_runner=lambda shell_argv, cancellation, progress, download_limiter, on_accept=None, task_service=None: (
                main(
                    shell_argv,
                    runtime=shell_runtime,
                    cancellation=cancellation,
                    progress=progress,
                    download_limiter=download_limiter,
                    task_service=task_service,
                    _owner_claimed=True,
                    on_accept=on_accept,
                )
            ),
        )

    if args.command == "plugins":
        if not getattr(args, "plugins_command", None):
            try:
                parser.parse_args(["plugins", "--help"])
            except SystemExit as exc:
                return exc.code if isinstance(exc.code, int) else 0
            return 0
        user_specified_roots = (
            getattr(args, "root", None) or getattr(args, "plugin_root", None) or []
        )
        roots = _plugin_roots(user_specified_roots)
        if args.plugins_command == "list":
            report = scan_local_plugin_packages(
                roots,
                include_ignored=args.include_ignored,
            )
            print(json.dumps(_plugin_list_to_dict(report=report, roots=roots), indent=2))
            return 0
        if args.plugins_command == "inspect":
            report = scan_local_plugin_packages(
                roots,
                include_ignored=args.include_ignored,
            )
            print(json.dumps(_plugin_inspect_to_dict(report=report, roots=roots), indent=2))
            return 0
        if args.plugins_command == "activate":
            if not args.enable_external_code:
                print(json.dumps(_plugin_activation_refusal_to_dict(roots=roots), indent=2))
                return 1
            plugin_runtime = build_application_runtime(
                plugin_roots=roots,
                enable_external_code=True,
            )
            print(
                json.dumps(
                    _plugin_activation_to_dict(
                        scan_report=plugin_runtime.scan_report,
                        activation_report=plugin_runtime.activation_report,
                        registry=plugin_runtime.site_registry,
                        roots=roots,
                    ),
                    indent=2,
                )
            )
            return 0
        plugins_dir = (
            Path(user_specified_roots[0]) if user_specified_roots else installed_plugins_root()
        )
        if args.plugins_command in {"remote", "refresh"}:
            environment = diagnose_environment()
            service = PluginCatalogService(environment.paths.cache_dir)
            force = True if args.plugins_command == "refresh" else getattr(args, "force", False)
            catalog = service.load(force=force)
            if getattr(args, "json", False):
                payload = {
                    "status": catalog.status,
                    "refreshed_at": catalog.refreshed_at,
                    "diagnostics": list(catalog.diagnostics),
                    "entries": [
                        {
                            "plugin_id": entry.plugin_id,
                            "repository_url": entry.repository_url,
                            "source": entry.source,
                            "author": entry.author,
                            "channel": entry.channel,
                            "tags": list(entry.tags),
                        }
                        for entry in catalog.entries
                    ],
                }
                print(json.dumps(payload, indent=2))
            else:
                label = (
                    "Refreshed plugin catalog"
                    if args.plugins_command == "refresh"
                    else "Plugin catalog"
                )
                print(f"{label}: {catalog.status} ({len(catalog.entries)} entries)")
                for entry in catalog.entries:
                    author = f" by {entry.author}" if entry.author else ""
                    print(f" - {entry.plugin_id}: {entry.repository_url} [{entry.source}]{author}")
            return 0

        if args.plugins_command == "install":
            target = args.target
            catalog_id = None
            if not (
                target.startswith("http://")
                or target.startswith("https://")
                or target.startswith("file://")
            ):
                environment = diagnose_environment()
                service = PluginCatalogService(environment.paths.cache_dir)
                catalog = service.load()
                matched = next(
                    (entry for entry in catalog.entries if entry.plugin_id == target), None
                )
                if matched is None:
                    err = f"Plugin ID {target!r} not found in remote catalog."
                    if getattr(args, "json", False):
                        print(json.dumps({"error": err}, indent=2))
                    else:
                        print(err)
                    return 1
                catalog_id = matched.plugin_id
                target = matched.repository_url
            try:
                version = install_plugin(plugins_dir, target, catalog_id=catalog_id)
                if getattr(args, "json", False):
                    print(
                        json.dumps(
                            {"status": "installed", "target": target, "version": version}, indent=2
                        )
                    )
                else:
                    print(f"Successfully installed plugin from {target} (version: {version})")
                return 0
            except Exception as exc:
                if getattr(args, "json", False):
                    print(json.dumps({"error": str(exc)}, indent=2))
                else:
                    print(f"Failed to install plugin: {exc}")
                return 1

        if args.plugins_command == "update":
            try:
                new_version = update_plugin(plugins_dir, args.plugin_id)
                if new_version:
                    if getattr(args, "json", False):
                        print(
                            json.dumps(
                                {
                                    "status": "updated",
                                    "plugin_id": args.plugin_id,
                                    "version": new_version,
                                },
                                indent=2,
                            )
                        )
                    else:
                        print(
                            f"Successfully updated plugin {args.plugin_id} to version {new_version}"
                        )
                else:
                    if getattr(args, "json", False):
                        print(
                            json.dumps(
                                {"status": "up_to_date", "plugin_id": args.plugin_id}, indent=2
                            )
                        )
                    else:
                        print(f"Plugin {args.plugin_id} is already up to date.")
                return 0
            except Exception as exc:
                if getattr(args, "json", False):
                    print(json.dumps({"error": str(exc)}, indent=2))
                else:
                    print(f"Failed to update plugin {args.plugin_id}: {exc}")
                return 1

        if args.plugins_command == "remove":
            try:
                remove_plugin(plugins_dir, args.plugin_id)
                if getattr(args, "json", False):
                    print(json.dumps({"status": "removed", "plugin_id": args.plugin_id}, indent=2))
                else:
                    print(f"Successfully removed plugin {args.plugin_id}")
                return 0
            except Exception as exc:
                if getattr(args, "json", False):
                    print(json.dumps({"error": str(exc)}, indent=2))
                else:
                    print(f"Failed to remove plugin {args.plugin_id}: {exc}")
                return 1

        if args.plugins_command == "rollback":
            try:
                prev_version = rollback_plugin(plugins_dir, args.plugin_id)
                if prev_version:
                    if getattr(args, "json", False):
                        print(
                            json.dumps(
                                {
                                    "status": "rolled_back",
                                    "plugin_id": args.plugin_id,
                                    "active_version": prev_version,
                                },
                                indent=2,
                            )
                        )
                    else:
                        print(f"Rolled back plugin {args.plugin_id} to version {prev_version}")
                else:
                    if getattr(args, "json", False):
                        print(
                            json.dumps(
                                {"status": "no_previous_version", "plugin_id": args.plugin_id},
                                indent=2,
                            )
                        )
                    else:
                        print(f"No previous version available for plugin {args.plugin_id}")
                return 0
            except Exception as exc:
                if getattr(args, "json", False):
                    print(json.dumps({"error": str(exc)}, indent=2))
                else:
                    print(f"Failed to rollback plugin {args.plugin_id}: {exc}")
                return 1

        if args.plugins_command == "switch":
            target_base = plugins_dir / args.plugin_id
            if not target_base.is_dir() or not (target_base / args.version).is_dir():
                err = f"Version {args.version!r} for plugin {args.plugin_id!r} is not installed."
                if getattr(args, "json", False):
                    print(json.dumps({"error": err}, indent=2))
                else:
                    print(err)
                return 1
            _write_active_marker(target_base, args.version)
            if getattr(args, "json", False):
                print(
                    json.dumps(
                        {
                            "status": "switched",
                            "plugin_id": args.plugin_id,
                            "active_version": args.version,
                        },
                        indent=2,
                    )
                )
            else:
                print(f"Switched plugin {args.plugin_id} active version to {args.version}")
            return 0

        if args.plugins_command == "versions":
            versions = list_plugin_versions(plugins_dir, args.plugin_id)
            if getattr(args, "json", False):
                print(json.dumps({"plugin_id": args.plugin_id, "versions": versions}, indent=2))
            else:
                print(f"Versions for plugin {args.plugin_id}:")
                for v in versions:
                    active = " [active]" if v.get("active") else ""
                    print(f" - {v.get('version')}{active}")
            return 0

        parser.print_help()
        return 0

    if args.command == "download":
        render_mode = select_render_mode(json_requested=args.json)
        if args.dry_run:
            result = _download_dry_run_to_dict(args.url, runtime=get_runtime())
            result["render_mode"] = render_mode.value
            print(json.dumps(result, indent=None if render_mode is RenderMode.JSON else 2))
            return 0 if result["matched"] else 1
        try:
            if not _owner_claimed:
                forwarded = _forward_download_to_shell(args)
                if forwarded is not None:
                    return forwarded
                if args.detach:
                    _emit_owner_error(
                        OwnerTransportError("--detach requires an active Shell owner"),
                        json_output=args.json,
                    )
                    return 1
            with _owned_execution(owner_claimed=_owner_claimed):
                return _run_generic_download(
                    args.url,
                    runtime=get_runtime(),
                    render_mode=render_mode,
                    profile=args.profile,
                    strategy=args.strategy,
                    selected_item_ids=tuple(args.select),
                    selection_policy=(
                        ListSelectionPolicy.EXPLICIT
                        if args.select
                        else ListSelectionPolicy(args.selection_policy)
                    ),
                    force=args.force,
                    cancellation=cancellation,
                    progress=progress,
                    download_limiter=download_limiter,
                    task_service=task_service,
                    task_id=None,
                    interactive_renderer=render_interactive_result,
                    on_accept=on_accept,
                )
        except OwnerError as error:
            _emit_owner_error(error, json_output=args.json)
            return 1

    if args.command == "operation":
        try:
            if args.operation_id != "help" and not _owner_claimed:
                forwarded = forward_operation(
                    args,
                    confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
                    emit_error=_emit_owner_error,
                )
                if forwarded is not None:
                    return forwarded
            ownership = (
                nullcontext()
                if args.operation_id == "help"
                else _owned_execution(owner_claimed=_owner_claimed)
            )
            with ownership:
                return _run_local_plugin_operation(args, get_runtime(), download_limiter)
        except OwnerError as error:
            _emit_owner_error(error, json_output=args.json)
            return 1

    if args.command == "batches":
        render_mode = select_render_mode(json_requested=getattr(args, "json", False))
        environment = diagnose_environment()
        layout = StorageLayout.from_app_paths(environment.paths)
        store = StateBatchStore(StateDatabase(layout.state_db))
        try:
            if args.batches_command == "show":
                print(json.dumps(_batch_details(store, args.parent_id), indent=2))
                return 0
            if args.batches_command == "retry":
                if not _owner_claimed:
                    forwarded = _forward_task_control_to_shell(
                        args.parent_id,
                        "retry",
                        json_output=getattr(args, "json", False),
                    )
                    if forwarded is not None:
                        return forwarded
                with _owned_execution(owner_claimed=_owner_claimed):
                    return _run_batch_retry(
                        args.parent_id,
                        runtime=get_runtime(),
                        download_limiter=download_limiter,
                        task_service=task_service,
                        store=store,
                        layout=layout,
                        render_mode=render_mode,
                        config=ConfigLoader(
                            paths=environment.paths,
                            registry=get_runtime().settings_registry,
                        ).load(),
                        profile=args.profile,
                        cancellation=cancellation,
                        progress=progress,
                    )
            if args.batches_command == "resume":
                if not _owner_claimed:
                    forwarded = _forward_task_control_to_shell(
                        args.parent_id,
                        "resume",
                        json_output=getattr(args, "json", False),
                    )
                    if forwarded is not None:
                        return forwarded
                with _owned_execution(owner_claimed=_owner_claimed):
                    task_record = StateTaskStore(StateDatabase(layout.state_db)).get(args.parent_id)
                    enumeration = (
                        task_record.metadata.get("batch_enumeration")
                        if task_record is not None
                        else None
                    )
                    if (
                        task_record is not None
                        and task_record.source_url
                        and isinstance(enumeration, dict)
                        and enumeration.get("state") == "enumerating"
                    ):
                        return _run_generic_download(
                            task_record.source_url,
                            runtime=get_runtime(),
                            render_mode=render_mode,
                            profile=args.profile,
                            selection_policy=ListSelectionPolicy.ALL,
                            cancellation=cancellation,
                            progress=progress,
                            download_limiter=download_limiter,
                            task_service=task_service,
                            task_id=args.parent_id,
                        )
                    return _run_batch_retry(
                        args.parent_id,
                        runtime=get_runtime(),
                        download_limiter=download_limiter,
                        task_service=task_service,
                        store=store,
                        layout=layout,
                        render_mode=render_mode,
                        config=ConfigLoader(
                            paths=environment.paths,
                            registry=get_runtime().settings_registry,
                        ).load(),
                        profile=args.profile,
                        cancellation=cancellation,
                        progress=progress,
                        resume=True,
                    )
        except OwnerError as error:
            _emit_owner_error(error, json_output=getattr(args, "json", False))
            return 1
        except CLIDownloaderError as error:
            print(json.dumps({"error": str(error)}, indent=2))
            return 1
        parser.print_help()
        return 0

    if args.command == "tasks":
        if args.tasks_command in {None, "list"}:
            if not _task_storage_exists():
                print(current_translator()("cli.no_tasks"))
                return 0
        service = task_service or _build_task_hierarchy_service()
        task_id = getattr(args, "task_id", None)
        if (
            task_id
            and args.tasks_command in {"pause", "resume", "retry", "cancel", "hard-retry"}
            and not _owner_claimed
        ):
            forwarded = _forward_task_control_to_shell(
                task_id,
                "hard-retry" if args.tasks_command == "hard-retry" else args.tasks_command,
                json_output=getattr(args, "json", False),
            )
            if forwarded is not None:
                return forwarded
        if args.tasks_command in {None, "list"}:
            nodes = service.list_roots()
            if task_id:
                nodes = tuple(node for node in nodes if node.task_id == task_id)
            status = args.status if args.tasks_command is None else args.status
            if status:
                nodes = tuple(node for node in nodes if node.state.value == status)
            if getattr(args, "json", False):
                print(json.dumps([_task_summary(node) for node in nodes], indent=2))
            else:
                _print_task_list(nodes, translator=current_translator())
            return 0
        if args.tasks_command == "show":
            if not task_id:
                print(json.dumps({"error": "positional TASK_ID is required"}))
                return 2
            try:
                node = service.get(task_id)
            except CLIDownloaderError as error:
                print(json.dumps({"error": str(error)}, indent=2))
                return 1
            if getattr(args, "json", False):
                print(json.dumps(_task_details(service, node), indent=2))
            else:
                _print_task_detail(service, node, translator=current_translator())
            return 0
        if args.tasks_command == "delete":
            return _delete_task_records(
                service,
                task_ids=tuple(args.task_ids),
                status=args.status,
                older_than_days=args.older_than,
                dry_run=args.dry_run,
                confirmed=args.yes,
                delete_all=args.all,
            )
        if args.tasks_command == "hard-retry":
            if not task_id:
                print(json.dumps({"error": "positional TASK_ID is required"}))
                return 2
            try:
                with _owned_execution(owner_claimed=_owner_claimed):
                    return _hard_retry_task(
                        task_id,
                        site_registry=get_runtime().site_registry,
                        runtime=get_runtime(),
                        json_output=getattr(args, "json", False),
                    )
            except OwnerError as error:
                _emit_owner_error(error, json_output=getattr(args, "json", False))
                return 1
        if args.tasks_command == "cancel":
            return _cancel_tasks(
                service,
                task_id=task_id,
                cancel_all=args.all,
                confirmed=args.yes,
                json_output=getattr(args, "json", False),
            )
        if args.tasks_command == "pause":
            if not task_id:
                print(json.dumps({"error": "positional TASK_ID is required"}))
                return 2
            try:
                result = service.pause(task_id)
            except CLIDownloaderError as error:
                print(json.dumps({"error": str(error)}, indent=2))
                return 1
            print(
                json.dumps(
                    result.to_dict()
                    if hasattr(result, "to_dict")
                    else {"task_id": task_id, "action": "pause"},
                    indent=2,
                )
            )
            return 0
        if not task_id:
            print(json.dumps({"error": "positional TASK_ID is required"}))
            return 2
        try:
            owner_context = (
                _owned_execution(owner_claimed=_owner_claimed)
                if args.tasks_command == "resume"
                else nullcontext()
            )
            with owner_context:
                task_store = _build_task_store()
                lifecycle = TaskLifecycleService(task_store=task_store)
                try:
                    if args.tasks_command == "retry":
                        result = lifecycle.retry(task_id)
                    elif args.tasks_command == "resume":
                        if service.is_running(task_id):
                            result = service.resume(task_id)
                            print(
                                json.dumps(
                                    result.to_dict()
                                    if hasattr(result, "to_dict")
                                    else {"task_id": task_id, "action": "resume"},
                                    indent=2,
                                )
                            )
                            return 0
                        result = lifecycle.resume(task_id)
                    else:
                        parser.print_help()
                        return 0
                except CLIDownloaderError as error:
                    print(json.dumps({"error": str(error)}, indent=2))
                    return 1
                if args.tasks_command == "resume":
                    record = task_store.get(task_id)
                    if record is not None and record.kind.value == "item" and record.source_url:
                        return _run_generic_download(
                            record.source_url,
                            runtime=get_runtime(),
                            render_mode=RenderMode.JSON
                            if getattr(args, "json", False)
                            else RenderMode.PLAIN,
                            cancellation=cancellation,
                            progress=progress,
                            download_limiter=download_limiter,
                            task_service=task_service,
                            task_id=task_id,
                        )
                print(json.dumps(result.to_dict(), indent=2))
                return 0
        except OwnerError as error:
            _emit_owner_error(error, json_output=getattr(args, "json", False))
            return 1

    if args.command == "history":
        env = diagnose_environment()
        layout = StorageLayout.from_app_paths(env.paths)
        history_command = getattr(args, "history_command", None)
        history_task_id = getattr(args, "task_id", None)
        if history_command == "delete":
            history_store = StateHistoryStore(StateDatabase(layout.state_db))
            if not any([args.all, history_task_id, getattr(args, "older_than", None) is not None]):
                translator = current_translator()
                print(f"{translator('cli.error_prefix')}: {translator('cli.history_delete_usage')}")
                return 1
            if not getattr(args, "yes", False):
                if not env.terminal.interactive:
                    translator = current_translator()
                    print(
                        f"{translator('cli.error_prefix')}: "
                        f"{translator('cli.interactive_prompt_unavailable')}"
                    )
                    return 1
                try:
                    response = input(current_translator()("cli.confirm_history_delete"))
                    if response.lower() not in ("y", "yes"):
                        print(current_translator()("cli.operation_cancelled"))
                        return 0
                except EOFError:
                    print(current_translator()("cli.operation_cancelled"))
                    return 0

            if args.all:
                history_store.clear()
                print(current_translator()("cli.history_deleted_all"))
            elif history_task_id:
                history_store.delete_task(history_task_id)
                print(current_translator()("cli.history_deleted_task", task_id=history_task_id))
            elif getattr(args, "older_than", None) is not None:
                history_store.delete_older_than(args.older_than)
                print(
                    current_translator()(
                        "cli.history_deleted_older",
                        days=args.older_than,
                    )
                )
            return 0

        history_store = StateHistoryStore(StateDatabase(layout.state_db))
        show_child = getattr(args, "child", False)
        if history_command not in {None, "show"}:
            print(json.dumps({"error": f"unknown history action: {history_command}"}, indent=2))
            return 2
        if history_task_id:
            if show_child:
                records = history_store.list_records(parent_record_id=history_task_id)
                if getattr(args, "json", False):
                    print(json.dumps(history_records_to_dicts(records), indent=2))
                else:
                    print_history_record_summary(
                        records,
                        page=getattr(args, "page", 1),
                        page_size=20,
                        query=getattr(args, "query", ""),
                        show_child=True,
                        translator=current_translator(),
                    )
                return 0
            record = history_store.get_record(history_task_id) or history_store.get_latest_record(
                history_task_id
            )
            if record is None:
                print(
                    current_translator()(
                        "cli.task_not_found_history",
                        task_id=history_task_id,
                    )
                )
                return 1
            records = (record,)
            if getattr(args, "json", False):
                print(json.dumps(history_records_to_dicts(records), indent=2))
            else:
                print_history_record_detail(record, translator=current_translator())
        else:
            records = history_store.list_records()
            if getattr(args, "json", False):
                print(json.dumps(history_records_to_dicts(records), indent=2))
            else:
                print_history_record_summary(
                    records,
                    page=getattr(args, "page", 1),
                    page_size=20,
                    query=getattr(args, "query", ""),
                    show_child=show_child,
                    translator=current_translator(),
                )
        return 0

    if args.command == "config":
        settings_registry = get_runtime().settings_registry
        if args.config_command == "defaults":
            print(json.dumps(settings_registry.schema.defaults(), indent=2))
            return 0
    if args.command == "check-update":
        service = ReleaseUpdateService(
            repository_url="https://github.com/RyuuMeow/CLI-Downloader.git",
            current_version=__version__,
        )
        res = service.check()
        payload = {
            "status": res.status.value,
            "current_version": res.current_version,
            "latest_version": res.latest_version,
            "release_url": res.release_url,
            "error_code": res.error_code,
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            if res.status.value == "update_available":
                print(f"Update available: {res.latest_version} (Current: {res.current_version})")
                print(f"Release URL: {res.release_url}")
            elif res.status.value == "up_to_date":
                print(f"CLI-Downloader is up to date (version {res.current_version}).")
            else:
                print(f"Release check status: {res.status.value}")
        return 0

    parser.print_help()
    return 0


def _load_task_records(layout: StorageLayout | None = None) -> tuple[TaskRecord, ...]:
    if layout is None:
        environment = diagnose_environment()
        layout = StorageLayout.from_app_paths(environment.paths)
    if not layout.state_db.exists():
        return ()
    return StateTaskStore(StateDatabase(layout.state_db)).list()


def _build_task_store() -> StateTaskStore:
    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    return StateTaskStore(StateDatabase(layout.state_db))


def _build_task_hierarchy_service() -> TaskHierarchyService:
    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    batch_store = StateBatchStore(StateDatabase(layout.state_db))
    return TaskHierarchyService(
        task_store=StateTaskStore(StateDatabase(layout.state_db)),
        batch_store=batch_store,
        history_store=StateHistoryStore(StateDatabase(layout.state_db)),
    )


def _task_storage_exists() -> bool:
    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    return layout.state_db.exists()


def _delete_task_records(
    service: TaskHierarchyService,
    *,
    task_ids: tuple[str, ...],
    status: str | None,
    older_than_days: float | None,
    dry_run: bool,
    confirmed: bool,
    delete_all: bool = False,
) -> int:
    if older_than_days is not None and older_than_days < 0:
        print(json.dumps({"error": "--older-than must not be negative"}))
        return 2
    if not delete_all and not task_ids and status is None and older_than_days is None:
        print(json.dumps({"error": "provide task ids, --status, --older-than, or --all"}))
        return 2

    candidates = (
        tuple(service.get(task_id) for task_id in dict.fromkeys(task_ids))
        if task_ids
        else service.list_roots()
    )
    if status is not None:
        candidates = tuple(node for node in candidates if node.state.value == status)
    if older_than_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        candidates = tuple(
            node
            for node in candidates
            if ((created_at := _task_created_at(node)) is not None and created_at <= cutoff)
        )

    blocked = tuple(
        node.task_id
        for node in candidates
        if node.state
        in {
            TaskState.PREPARING,
            TaskState.QUEUED,
            TaskState.RUNNING,
            TaskState.PAUSING,
        }
    )
    payload = {
        "matched": len(candidates),
        "task_ids": [node.task_id for node in candidates],
        "blocked_active": list(blocked),
        "dry_run": dry_run,
        "deleted": 0,
        "downloaded_files_preserved": True,
    }
    if dry_run:
        print(json.dumps(payload, indent=2))
        return 0
    if not confirmed:
        payload["error"] = "rerun with --yes after reviewing --dry-run"
        print(json.dumps(payload, indent=2))
        return 2
    if blocked:
        payload["error"] = "active tasks cannot be deleted"
        print(json.dumps(payload, indent=2))
        return 2
    for node in candidates:
        service.delete(node.task_id)
    payload["deleted"] = len(candidates)
    print(json.dumps(payload, indent=2))
    return 0


def _hard_retry_task(
    task_id: str,
    *,
    site_registry: SiteRegistry,
    runtime: ApplicationRuntime,
    json_output: bool = False,
    _task_store: StateTaskStore | None = None,
    _batch_store: StateBatchStore | None = None,
    _history_store: StateHistoryStore | None = None,
) -> int:
    """Rebuild a task from its source URL without replacing its public identity.

    Rebuild the execution plan in place while preserving the public task identity.
    """
    env = diagnose_environment()
    layout = StorageLayout.from_app_paths(env.paths)
    task_store = _task_store or StateTaskStore(StateDatabase(layout.state_db))
    batch_store = _batch_store or StateBatchStore(StateDatabase(layout.state_db))
    history_store = _history_store or StateHistoryStore(StateDatabase(layout.state_db))

    try:
        is_batch_parent = task_id in batch_store.list_parent_ids()

        if is_batch_parent:
            return _hard_retry_batch(
                task_id,
                task_store=task_store,
                batch_store=batch_store,
                history_store=history_store,
                site_registry=site_registry,
                runtime=runtime,
                env=env,
                json_output=json_output,
            )

        # --- Standalone task or batch entry ---
        source_url: str | None = None
        profile: str | None = None

        record = task_store.get(task_id)
        if record is not None and record.source_url:
            source_url = record.source_url
            raw_profile = record.metadata.get("profile")
            if isinstance(raw_profile, str):
                profile = raw_profile

        entry = batch_store.get_entry(task_id)
        if entry is not None and entry.source_url:
            source_url = entry.source_url
            profile = None

        if not source_url:
            _emit_hard_retry_error(
                f"task {task_id} has no source URL to rebuild from",
                json_output=json_output,
            )
            return 1

        if record is None:
            _emit_hard_retry_error(
                f"task {task_id} is not a standalone Task; retry its Batch parent instead",
                json_output=json_output,
            )
            return 1

        history_store.append(
            TaskEvent(
                task_id=task_id,
                name="hard_retry",
                reason="hard_retry_requested",
                payload={"source_url": source_url, "previous_status": record.status.value},
            )
        )

        # Rebuild from scratch.
        download_service = DownloadService(
            registry=site_registry,
            task_store=task_store,
            history_store=history_store,
            environment=env,
        )
        result = asyncio.run(
            download_service.plan_task(
                source_url,
                profile=profile,
                task_id=task_id,
                force=True,
            )
        )
        result_dict = result.to_dict()

        if json_output:
            print(json.dumps(result_dict, indent=2))
        else:
            _print_hard_retry_summary([result_dict])
        return 0
    except Exception as exc:
        _emit_hard_retry_error(str(exc), json_output=json_output)
        return 1


def _hard_retry_batch(
    parent_id: str,
    *,
    task_store: StateTaskStore,
    batch_store: StateBatchStore,
    history_store: StateHistoryStore,
    site_registry: SiteRegistry,
    runtime: ApplicationRuntime,
    env: object,
    json_output: bool,
) -> int:
    """Recreate a batch parent from its entry source URLs."""
    # Gather entries BEFORE deletion.
    entries = batch_store.list_entries(parent_id)
    if not entries:
        _emit_hard_retry_error(
            f"batch {parent_id} has no entries to rebuild from",
            json_output=json_output,
        )
        return 1

    first_entry = entries[0]
    runtime_entry = runtime.entry_for_url(first_entry.source_url)
    if runtime_entry is None:
        _emit_hard_retry_error(
            f"no plugin found for URL: {first_entry.source_url}",
            json_output=json_output,
        )
        return 1

    route = runtime_entry.instance.classify_url(first_entry.source_url)
    if route is None:
        _emit_hard_retry_error(
            f"URL is not routable: {first_entry.source_url}",
            json_output=json_output,
        )
        return 1

    if any(not entry.source_url for entry in entries):
        _emit_hard_retry_error(
            f"batch {parent_id} has no entries with source URLs",
            json_output=json_output,
        )
        return 1

    task_record = task_store.get(parent_id)
    if task_record is None:
        _emit_hard_retry_error(
            f"batch {parent_id} has no Task row to rebuild safely",
            json_output=json_output,
        )
        return 1
    current_snapshot = batch_store.get_snapshot(parent_id)
    rebuilt_entries = tuple(
        replace(
            entry,
            entry_id=uuid4().hex,
            parent_id=parent_id,
            status=BatchEntryStatus.PENDING_RESOLUTION,
            child_id=None,
            failure=None,
        )
        for entry in entries
    )
    rebuilt_parent = BatchParent(
        parent_id=parent_id,
        entry_ids=tuple(entry.entry_id for entry in rebuilt_entries),
        title=current_snapshot.parent.title or task_record.title or parent_id[:8],
    )
    rebuilt_task = task_record.transition(
        status=TaskStatus.PLANNED,
        active_run_id=None,
        desired_control=TaskDesiredControl.RUN,
        metadata={
            **task_record.metadata,
            "last_run_reason": "hard_retry",
            "previous_status": task_record.status.value,
        },
    )
    current_history = history_store.get_record(parent_id)
    rebuilt_history = (
        replace(current_history, status="pending", finished_at=None)
        if isinstance(current_history, DownloadHistoryRecord)
        else None
    )
    batch_store.rebuild_task_and_batch(
        rebuilt_task,
        rebuilt_parent,
        rebuilt_entries,
        history_events=(
            TaskEvent(
                task_id=parent_id,
                name="hard_retry",
                reason="hard_retry_requested",
                payload={"previous_status": task_record.status.value},
            ),
        ),
        history_record=rebuilt_history,
    )

    result = {
        "parent_id": parent_id,
        "type": "batch",
        "title": rebuilt_parent.title,
        "entries": len(rebuilt_parent.entry_ids),
        "skipped": 0,
    }

    if json_output:
        print(json.dumps(result, indent=2))
    else:
        _print_hard_retry_summary([result])
    return 0


def _emit_hard_retry_error(message: str, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"error": message}, indent=2))
    else:
        translator = current_translator()
        print(f"{translator('cli.error_prefix')}: {message}")


def _print_hard_retry_summary(results: list[dict[str, object]]) -> None:
    translator = current_translator()
    rows: list[tuple[str, ...]] = []
    for r in results:
        task_id = str(r.get("task_id") or r.get("parent_id", ""))
        task_type = str(r.get("type") or "item")
        title = str(r.get("title") or r.get("name") or "")
        entries = str(r.get("entries") or "-")
        rows.append((task_id, task_type, title, entries))
    print_rich_table(
        TableSpec(
            title=translator("cli.hard_retry_result"),
            columns=(
                TableColumn(translator("table.task_id"), style="cyan"),
                TableColumn(translator("table.type"), style="magenta"),
                TableColumn(translator("table.title"), style="green"),
                TableColumn(translator("table.entries"), justify="right"),
            ),
            rows=tuple(rows),
        )
    )


def _cancel_tasks(
    service: TaskHierarchyService,
    *,
    task_id: str | None,
    cancel_all: bool,
    confirmed: bool,
    json_output: bool = False,
) -> int:
    """Cancel tasks via CLI.

    With a positional task id: cancel a single task (standalone or batch).
    With ``--all``: cancel all cancellable tasks (requires ``--yes``).
    """
    from cli_downloader.application.task_lifecycle_service import CANCELLABLE_STATUSES
    from cli_downloader.application.task_hierarchy_service import _STANDALONE_STATES

    cancellable_task_states = {_STANDALONE_STATES[s].value for s in CANCELLABLE_STATUSES}

    if not cancel_all and not task_id:
        _emit_hard_retry_error("provide TASK_ID or --all", json_output=json_output)
        return 2

    if cancel_all:
        if not confirmed:
            _emit_hard_retry_error(
                "rerun with --yes to confirm bulk cancellation",
                json_output=json_output,
            )
            return 2

        nodes = service.list_roots()
        cancellable = [node for node in nodes if node.state.value in cancellable_task_states]
        if not cancellable:
            payload = {"cancelled": 0, "task_ids": []}
            if json_output:
                print(json.dumps(payload, indent=2))
            else:
                print(current_translator()("cli.no_cancellable_tasks"))
            return 0

        cancelled: list[str] = []
        errors: list[dict[str, str]] = []
        for node in cancellable:
            try:
                service.cancel(node.task_id)
                cancelled.append(node.task_id)
            except Exception as exc:
                errors.append({"task_id": node.task_id, "error": str(exc)})

        payload: dict[str, object] = {
            "cancelled": len(cancelled),
            "task_ids": cancelled,
        }
        if errors:
            payload["errors"] = errors
        if json_output:
            print(json.dumps(payload, indent=2))
        else:
            from rich.console import Console

            console = Console()
            translator = current_translator()
            console.print(translator("cli.cancelled_tasks", count=len(cancelled)))
            if errors:
                console.print(f"[red]{translator('cli.error_count', count=len(errors))}[/red]")
        return 0 if not errors else 1

    # Single task cancel.
    try:
        service.cancel(task_id)  # type: ignore[arg-type]
    except CLIDownloaderError as error:
        _emit_hard_retry_error(str(error), json_output=json_output)
        return 1

    if json_output:
        print(json.dumps({"cancelled": [task_id]}))
    else:
        print(current_translator()("cli.cancelled_task", task_id=task_id))
    return 0


def _build_completion_store(layout: StorageLayout) -> CompletionStore:
    return SQLiteCompletionStore(StateDatabase(layout.state_db))
