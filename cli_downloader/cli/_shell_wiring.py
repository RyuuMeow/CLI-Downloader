"""Interactive shell construction and startup recovery wiring.

The shell UI remains in ``cli.shell``.  This module only assembles its runtime
services and callbacks so the command parser does not own application startup
state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
import os
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from time import monotonic
from uuid import uuid4

from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
from cli_downloader.application.execution_control import ExecutionStopSignal
from cli_downloader.application.download_limiter import GalleryDownloadLimiter
from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.cli._download_ops import _build_download_limiter
from cli_downloader.cli._download_ops import _record_list_browse
from cli_downloader.cli._download_ops import _run_selected_list_batch
from cli_downloader.cli._inspect_ops import (
    _dispatch_operation,
    _dispatch_plugin_operation,
    _with_default_strategy,
)
from cli_downloader.cli._output import _EXPLICIT_DOWNLOAD_CONFIRMATIONS
from cli_downloader.cli._output import ProgressSink
from cli_downloader.cli.rendering import RenderMode
from cli_downloader.cli.shell import InteractiveShell
from cli_downloader.config.loader import ConfigLoader
from cli_downloader.core.environment import diagnose_environment
from cli_downloader.core.owner_protocol import OwnerLifecycleState, OwnerMode
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerDisconnectOutcome,
    OwnerHandshake,
    OwnerResponseEvent,
    OwnerResponseMode,
    OwnerRequest,
    OwnerTerminalStatus,
    OwnerWaitPolicy,
    OwnerWaitPhase,
)
from cli_downloader.core.owner_scope import (
    OwnerDiscoveryMetadata,
    OwnerLease,
    OwnerManager,
    OwnerScope,
    new_owner_session_id,
)
from cli_downloader.core.errors import OwnerError, OwnerScopeMismatchError
from cli_downloader.core.owner_transport import OwnerPipeServer
from cli_downloader.core.operations import OperationResult
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.integrations.browser_login import BrowserLoginService
from cli_downloader.plugins.runtime import ApplicationRuntime
from cli_downloader.storage.paths import StorageLayout
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import TaskDesiredControl, TaskStatus
from cli_downloader.about import __version__


def build_interactive_shell(
    *,
    runtime: ApplicationRuntime,
    plugins_dir: Path,
    runner: Callable[[Sequence[str]], int],
    background_runner: Callable[
        [Sequence[str], object, ProgressSink, GalleryDownloadLimiter, object], int
    ],
    operation_runner: Callable[[str, Mapping[str, object]], object] | None = None,
    owner_lease: OwnerLease | None = None,
) -> int:
    """Claim the Scheduler owner before recovery and run the visible Shell."""

    environment = diagnose_environment()
    scope = OwnerScope.from_app_paths(environment.paths)
    session_id = new_owner_session_id()
    owns_lease = owner_lease is None
    lease = owner_lease
    if lease is None:
        metadata = OwnerDiscoveryMetadata.for_current_process(
            scope,
            application_version=__version__,
            owner_mode=OwnerMode.SHELL,
            lifecycle_state=OwnerLifecycleState.RECOVERING,
            session_id=session_id,
            capabilities=("download", "task_control", "task_query", "history_query", "shell_status"),
        )
        lease = OwnerManager(scope).acquire(metadata)
    elif lease.scope != scope:
        raise OwnerScopeMismatchError("owner lease belongs to a different data-root scope")

    assert lease is not None
    try:
        return _build_interactive_shell_impl(
            runtime=runtime,
            plugins_dir=plugins_dir,
            runner=runner,
            background_runner=background_runner,
            operation_runner=operation_runner,
            owner_lease=lease,
            owner_session_id=session_id,
        )
    finally:
        if owns_lease:
            stopping = OwnerDiscoveryMetadata.for_current_process(
                scope,
                application_version=__version__,
                owner_mode=OwnerMode.SHELL,
                lifecycle_state=OwnerLifecycleState.STOPPING,
                session_id=session_id,
                capabilities=("download", "task_control", "task_query", "history_query", "shell_status"),
            )
            try:
                lease.publish(stopping)
            finally:
                lease.release()


def _build_interactive_shell_impl(
    *,
    runtime: ApplicationRuntime,
    plugins_dir: Path,
    runner: Callable[[Sequence[str]], int],
    background_runner: Callable[
        [Sequence[str], object, ProgressSink, GalleryDownloadLimiter, object], int
    ],
    operation_runner: Callable[[str, Mapping[str, object]], object] | None = None,
    owner_lease: OwnerLease | None = None,
    owner_session_id: str | None = None,
) -> int:
    """Build and run the interactive shell for an already-loaded runtime."""

    environment = diagnose_environment()
    layout = StorageLayout.from_app_paths(environment.paths)
    shell_config = ConfigLoader(
        paths=environment.paths,
        registry=runtime.settings_registry,
    ).load()
    runtime.configure_plugins(shell_config.values, shell_config.secrets)
    shell_download_limiter = _build_download_limiter(
        runtime,
        shell_config.values,
    )
    task_service: TaskHierarchyService | None = None
    state_database = StateDatabase(layout.state_db)
    task_store = StateTaskStore(state_database)
    batch_store = StateBatchStore(state_database)
    history_store = StateHistoryStore(state_database)
    startup_commands = _recover_interrupted_downloads(task_store, batch_store, history_store)
    shutdown_requested = Event()
    startup_ready = Event()
    execution_lock = Lock()
    active_cancellations: set[object] = set()
    active_workers: set[Thread] = set()
    browser_login = BrowserLoginService(
        profile_root=environment.paths.cache_dir / "browser-auth"
    )

    def validate_browser_session(plugin_id, session):
        plugin = runtime.plugin_for_id(plugin_id)
        validator = getattr(plugin, "validate_auth_session", None)
        return not callable(validator) or bool(validator(session))

    def capture_browser_session(plugin_id, auth):
        return browser_login.capture(
            auth,
            validator=lambda session: validate_browser_session(plugin_id, session),
        )

    def load_task_service() -> TaskHierarchyService:
        nonlocal task_service
        if task_service is None:
            task_service = TaskHierarchyService(
                task_store=task_store,
                batch_store=batch_store,
                history_store=history_store,
                scheduler=shell_download_limiter,
            )
        return task_service

    def submit_selected_batch(items, source_url, title, cancellation, progress):
        return _run_selected_list_batch(
            items,
            batch_source_url=source_url,
            batch_title=title,
            runtime=runtime,
            render_mode=RenderMode.JSON,
            profile=None,
            force=False,
            cancellation=cancellation,
            progress=progress,
            task_service=load_task_service(),
            download_limiter=shell_download_limiter,
        )

    def run_background(shell_argv, cancellation, progress, on_accept=None):
        worker = current_thread()
        with execution_lock:
            active_cancellations.add(cancellation)
            active_workers.add(worker)
            if shutdown_requested.is_set():
                request = getattr(cancellation, "request_shutdown", None)
                if callable(request):
                    request()
                else:
                    cancellation.set()
        try:
            return background_runner(
                shell_argv,
                cancellation,
                progress,
                shell_download_limiter,
                on_accept,
            )
        finally:
            with execution_lock:
                active_cancellations.discard(cancellation)
                active_workers.discard(worker)

    def persist_interruption_intent(*, normalize_running: bool = True) -> int:
        unfinished = 0
        for record in task_store.list():
            if record.status in {
                TaskStatus.PENDING,
                TaskStatus.PLANNED,
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
            }:
                unfinished += 1
            if normalize_running and record.status is TaskStatus.RUNNING:
                task_store.upsert(
                    record.transition(
                        status=TaskStatus.PLANNED,
                        desired_control=TaskDesiredControl.RUN,
                        active_run_id=None,
                        # The bounded drain has expired.  Rotate the fence
                        # before releasing the owner lease so a
                        # non-cooperative worker from the old process cannot
                        # publish a late terminal/checkpoint write.
                        metadata={**record.metadata, "execution_fence": uuid4().hex},
                    )
                )
        return unfinished

    owner_server: OwnerPipeServer | None = None

    def request_shutdown() -> str | None:
        first_request = not shutdown_requested.is_set()
        shutdown_requested.set()
        if first_request and owner_lease is not None:
            owner_lease.publish(
                OwnerDiscoveryMetadata.for_current_process(
                    owner_lease.scope,
                    application_version=__version__,
                    owner_mode=OwnerMode.SHELL,
                    lifecycle_state=OwnerLifecycleState.STOPPING,
                    session_id=owner_session_id or new_owner_session_id(),
                    capabilities=("download", "task_control", "task_query", "history_query", "shell_status"),
                )
            )
        if first_request and owner_server is not None:
            owner_server.stop()
        with execution_lock:
            for cancellation in tuple(active_cancellations):
                request = getattr(cancellation, "request_shutdown", None)
                if callable(request):
                    request()
                else:
                    # Third-party/background runners may still receive a raw
                    # Event; preserve their existing cancellation behavior
                    # until they adopt the typed signal.
                    cancellation.set()
        # Active workers own their run revisions and must checkpoint first;
        # normalising RUNNING here would invalidate their coordinator token.
        unfinished = persist_interruption_intent(normalize_running=False)
        if unfinished == 0:
            return None
        noun = "task" if unfinished == 1 else "tasks"
        return (
            f"{unfinished} unfinished {noun} are persisted; they will be recovered "
            "when the Shell starts again."
        )

    def wait_for_safe_boundaries(timeout: float = 2.0) -> None:
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            with execution_lock:
                workers = tuple(worker for worker in active_workers if worker is not current_thread())
            if not workers:
                break
            for worker in workers:
                worker.join(timeout=max(0.0, min(0.1, deadline - monotonic())))
        # A worker may have written a RUNNING checkpoint just before it noticed
        # cancellation; normalize it once more before the owner lock is freed.
        persist_interruption_intent()

    operation_callback = operation_runner or (
        lambda url, params: _load_shell_operation(runtime, url, params)
    )

    def publish_ready() -> None:
        if startup_ready.is_set() or owner_lease is None:
            return
        startup_ready.set()
        owner_lease.publish(
            OwnerDiscoveryMetadata.for_current_process(
                owner_lease.scope,
                application_version=__version__,
                owner_mode=OwnerMode.SHELL,
                lifecycle_state=OwnerLifecycleState.READY,
                session_id=owner_session_id or new_owner_session_id(),
                capabilities=("download", "task_control", "task_query", "history_query", "shell_status"),
            )
        )

    shell = InteractiveShell(
        runner=runner,
        background_runner=run_background,
        task_service_loader=load_task_service,
        operation_runner=operation_callback,
        batch_selection_runner=submit_selected_batch,
        settings_registry=runtime.settings_registry,
        config_loader=ConfigLoader(
            paths=environment.paths,
            registry=runtime.settings_registry,
        ),
        browser_auth_runner=capture_browser_session,
        browser_auth_validator=validate_browser_session,
        startup_commands=startup_commands,
        runtime=runtime,
        plugins_dir=plugins_dir,
        cache_dir=environment.paths.cache_dir,
        shutdown_warning=request_shutdown,
        startup_ready_callback=publish_ready,
    )

    if owner_lease is not None and owner_session_id is not None:
        owner_server = OwnerPipeServer(
            owner_lease.scope,
            handshake=OwnerHandshake(
                protocol_version=OWNER_PROTOCOL_VERSION,
                session_id=owner_session_id,
                mode=OwnerMode.SHELL,
                application_version=__version__,
                capabilities=("download", "task_query", "task_control", "history_query", "shell_status"),
                scope_fingerprint=owner_lease.scope.scope_fingerprint,
            ),
            handler=lambda request, emit: _handle_owner_request(
                request,
                emit,
                run_background=run_background,
                owner_session_id=owner_session_id,
                task_service_loader=load_task_service,
                notify_tasks=shell.notify_external_tasks,
                is_shutting_down=shutdown_requested.is_set,
                is_ready=startup_ready.is_set,
            ),
        )
        owner_server.start()
    try:
        return shell.run()
    finally:
        request_shutdown()
        wait_for_safe_boundaries()
        if owner_server is not None:
            owner_server.stop()


def _load_shell_operation(
    runtime: ApplicationRuntime,
    url: str,
    params: Mapping[str, object],
) -> tuple[str, OperationResult]:
    environment = diagnose_environment()
    config = ConfigLoader(paths=environment.paths, registry=runtime.settings_registry).load()
    runtime.configure_plugins(config.values, config.secrets)

    if url.startswith("plugin://"):
        from urllib.parse import parse_qsl, urlsplit

        parsed = urlsplit(url)
        plugin_id = parsed.hostname
        operation_id = parsed.path.lstrip("/")
        query_params = dict(parse_qsl(parsed.query))

        merged_params = dict(params)
        merged_params.update(query_params)
        merged_params = _with_default_strategy(runtime, url, merged_params, config)

        result = _dispatch_plugin_operation(
            OperationDispatcher(runtime, config=config),
            plugin_id,
            operation_id,
            params=merged_params,
            confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
        )
    if url.startswith("plugin://"):
        return (plugin_id, result)
    entry = runtime.entry_for_url(url)
    result = _dispatch_operation(
        OperationDispatcher(runtime, config=config),
        url,
        params=_with_default_strategy(runtime, url, params, config),
        confirmations=_EXPLICIT_DOWNLOAD_CONFIRMATIONS,
    )
    _record_list_browse(result, url=url, runtime=runtime)
    return (entry.plugin_id if entry is not None else "unknown", result)


def _handle_owner_request(
    request: OwnerRequest,
    emit: Callable[[OwnerResponseEvent], None],
    *,
    run_background: Callable[..., int],
    owner_session_id: str,
    task_service_loader: Callable[[], TaskHierarchyService],
    notify_tasks: Callable[[], None] | None = None,
    is_shutting_down: Callable[[], bool] | None = None,
    is_ready: Callable[[], bool] | None = None,
) -> None:
    """Run one externally submitted download inside the Shell owner.

    The worker is deliberately independent from the requesting process.  A
    detached client can close its pipe after the acceptance event and the Shell
    continues the same background execution and durable task lifecycle.
    """

    if is_shutting_down is not None and is_shutting_down():
        raise OwnerError("Shell is stopping and is not accepting new commands")
    if is_ready is not None and not is_ready():
        raise OwnerError("Shell is recovering and is not accepting commands yet")
    if request.command is OwnerCommand.TASK_CONTROL:
        _handle_owner_control(
            request,
            emit,
            run_background=run_background,
            owner_session_id=owner_session_id,
            task_service=task_service_loader(),
            notify_tasks=notify_tasks,
        )
        return
    if request.command is not OwnerCommand.DOWNLOAD:
        raise ValueError(f"unsupported owner command: {request.command.value}")
    _handle_owner_download(
        request,
        emit,
        run_background=run_background,
        owner_session_id=owner_session_id,
        notify_tasks=notify_tasks,
    )


def _handle_owner_download(
    request: OwnerRequest,
    emit: Callable[[OwnerResponseEvent], None],
    *,
    run_background: Callable[..., int],
    owner_session_id: str,
    notify_tasks: Callable[[], None] | None = None,
) -> None:
    payload = dict(request.payload)
    url = payload.get("url")
    if not isinstance(url, str):
        raise ValueError("download request is missing its URL")
    argv: list[str] = ["download", url, "--json"]
    for name in ("profile", "strategy"):
        value = payload.get(name)
        if isinstance(value, str):
            argv.extend((f"--{name}", value))
    selections = payload.get("select", ())
    if isinstance(selections, tuple):
        for item_id in selections:
            if isinstance(item_id, str):
                argv.extend(("--select", item_id))
    selection_policy = payload.get("selection_policy", "all")
    if selection_policy not in {"all", "explicit", "interactive"}:
        raise ValueError("download request has an invalid selection policy")
    argv.extend(("--selection-policy", selection_policy))
    if payload.get("force") is True:
        argv.append("--force")

    sequence = 0
    accepted = Event()
    finished = Event()
    heartbeat_stop = Event()
    sequence_lock = Lock()
    cancellation = ExecutionStopSignal()
    accepted_task: dict[str, str] = {}
    pipe_closed = Event()

    def push(event_factory: Callable[[int], OwnerResponseEvent]) -> bool:
        nonlocal sequence
        if pipe_closed.is_set():
            return False
        with sequence_lock:
            sequence += 1
            current_sequence = sequence
        try:
            emit(event_factory(current_sequence))
        except (OSError, EOFError, OwnerError):
            pipe_closed.set()
            return False
        return True

    def on_accept(task_id: str, task_kind: str) -> None:
        if accepted.is_set():
            return
        accepted_task.update({"task_id": task_id, "task_kind": task_kind})
        if notify_tasks is not None:
            try:
                notify_tasks()
            except Exception:
                # The durable Task remains authoritative if the UI is already
                # shutting down or cannot accept a cross-thread refresh.
                pass
        if push(
            lambda current: OwnerResponseEvent.accepted(
                request.request_id,
                current,
                owner_session_id=owner_session_id,
                accepted_at=datetime.now(UTC),
                task_id=task_id,
                task_kind=task_kind,
                queued=True,
                owner_pid=os.getpid(),
            )
        ):
            accepted.set()
            Thread(target=heartbeat, name=f"owner-heartbeat-{request.request_id[:8]}", daemon=True).start()

    def heartbeat() -> None:
        while not heartbeat_stop.wait(5.0):
            if finished.is_set() or not push(
                lambda current: OwnerResponseEvent.heartbeat(
                    request.request_id,
                    current,
                    owner_session_id=owner_session_id,
                    emitted_at=datetime.now(UTC),
                )
            ):
                return

    def progress(reason: str, progress_payload: Mapping[str, object]) -> None:
        if not accepted.is_set() or not accepted_task:
            return
        current = progress_payload.get("current")
        total = progress_payload.get("total")
        ratio = None
        if isinstance(current, int) and isinstance(total, int) and total > 0:
            ratio = min(1.0, max(0.0, current / total))
        push(
            lambda sequence_number: OwnerResponseEvent.progress(
                request.request_id,
                sequence_number,
                task_id=accepted_task["task_id"],
                status=reason,
                progress=ratio,
                file_count=current if isinstance(current, int) and current >= 0 else None,
                message=str(progress_payload.get("message")) if progress_payload.get("message") else None,
            )
        )

    def worker() -> None:
        output = StringIO()
        exit_code = 1
        try:
            with redirect_stdout(output):
                exit_code = run_background(
                    argv,
                    cancellation,
                    progress,
                    on_accept,
                )
        except Exception as error:
            output.write(json.dumps({"error": str(error)}))
            exit_code = 1
        if not accepted.is_set():
            push(
                lambda current: OwnerResponseEvent.owner_disconnected(
                    request.request_id,
                    current,
                    phase=OwnerWaitPhase.ACCEPTANCE,
                    outcome=OwnerDisconnectOutcome.NOT_ACCEPTED,
                    last_sequence=current - 1,
                    message="download did not create a durable Task; select a list item explicitly",
                )
            )
            finished.set()
            heartbeat_stop.set()
            return
        status = (
            OwnerTerminalStatus.COMPLETED
            if exit_code == 0
            else OwnerTerminalStatus.CANCELLED
            if exit_code == 130
            else OwnerTerminalStatus.FAILED
        )
        push(
            lambda current: OwnerResponseEvent.terminal(
                request.request_id,
                current,
                status=status,
                task_id=accepted_task.get("task_id"),
                exit_code=exit_code,
                result={"output": output.getvalue()},
            )
        )
        finished.set()
        heartbeat_stop.set()

    Thread(target=worker, name=f"owner-download-{request.request_id[:8]}", daemon=True).start()
    accepted_before_deadline = False
    acceptance_deadline = monotonic() + OwnerWaitPolicy().acceptance_timeout
    while monotonic() < acceptance_deadline:
        if accepted.is_set():
            accepted_before_deadline = True
            break
        if finished.wait(min(0.1, max(0.0, acceptance_deadline - monotonic()))):
            break
    if not accepted_before_deadline and not pipe_closed.is_set() and not finished.is_set():
        push(
            lambda current: OwnerResponseEvent.owner_disconnected(
                request.request_id,
                current,
                phase=OwnerWaitPhase.ACCEPTANCE,
                outcome=OwnerDisconnectOutcome.NOT_ACCEPTED,
                last_sequence=current - 1,
                message="owner did not create a durable Task before the acceptance deadline",
            )
        )
        pipe_closed.set()
    if request.response_mode is OwnerResponseMode.WAIT:
        finished.wait()


def _handle_owner_control(
    request: OwnerRequest,
    emit: Callable[[OwnerResponseEvent], None],
    *,
    run_background: Callable[..., int],
    owner_session_id: str,
    task_service: TaskHierarchyService,
    notify_tasks: Callable[[], None] | None = None,
) -> None:
    payload = dict(request.payload)
    task_id = payload.get("task_id")
    action = payload.get("action")
    if not isinstance(task_id, str) or not isinstance(action, str):
        raise ValueError("task control requires task_id and action")
    node = task_service.get(task_id)
    task_kind = node.kind.value
    sequence = 1
    emit(
        OwnerResponseEvent.accepted(
            request.request_id,
            sequence,
            owner_session_id=owner_session_id,
            owner_pid=os.getpid(),
            accepted_at=datetime.now(UTC),
            task_id=task_id,
            task_kind=task_kind,
            queued=False,
        )
    )
    sequence += 1
    try:
        output = StringIO()
        result: object
        exit_code = 0
        if action in {"pause", "cancel"}:
            result = getattr(task_service, action)(task_id)
        elif action in {"resume", "retry", "hard-retry"} and task_kind == TaskKind.BATCH.value:
            subcommand = "resume" if action == "resume" else "retry"
            with redirect_stdout(output):
                exit_code = run_background(
                    ("batches", subcommand, task_id, "--json"),
                    ExecutionStopSignal(),
                    lambda _reason, _payload: None,
                    None,
                )
            result = {"task_id": task_id, "action": action, "output": output.getvalue()}
        elif action == "resume":
            with redirect_stdout(output):
                exit_code = run_background(
                    ("tasks", "resume", task_id, "--json"),
                    ExecutionStopSignal(),
                    lambda _reason, _payload: None,
                    None,
                )
            result = {"task_id": task_id, "action": action, "output": output.getvalue()}
        elif action == "hard-retry":
            with redirect_stdout(output):
                exit_code = run_background(
                    ("tasks", "hard-retry", task_id, "--json"),
                    ExecutionStopSignal(),
                    lambda _reason, _payload: None,
                    None,
                )
            result = {"task_id": task_id, "action": action, "output": output.getvalue()}
        else:
            result = task_service.retry(task_id)
        revision = None
        record = getattr(result, "record", None)
        if record is None:
            store = getattr(task_service, "_task_store", None)
            record = store.get(task_id) if store is not None else None
        if record is not None:
            revision = getattr(record, "revision", None)
        if hasattr(result, "to_dict"):
            serialised = result.to_dict()
        elif isinstance(result, Mapping):
            serialised = dict(result)
        else:
            serialised = {"task_id": task_id, "action": action, "acknowledged": bool(result)}
        receipt = None
        wait_for_applied = getattr(task_service, "wait_for_control_applied", None)
        if action in {"pause", "resume", "cancel"} and callable(wait_for_applied):
            receipt = wait_for_applied(task_id, action, timeout=2.0)
            if hasattr(receipt, "to_dict"):
                serialised = {
                    **(serialised if isinstance(serialised, dict) else {"value": serialised}),
                    "control_receipt": receipt.to_dict(),
                }
        acknowledged = exit_code == 0
        applied = None
        actual_status = None
        desired_control = (
            "pause"
            if action == "pause"
            else "cancel"
            if action == "cancel"
            else "run"
        )
        quiescent = None
        phase = None
        if receipt is not None:
            acknowledged = bool(getattr(receipt, "requested", acknowledged))
            applied = bool(getattr(receipt, "applied", False))
            actual_status = getattr(getattr(receipt, "actual_status", None), "value", None)
            desired_control = getattr(getattr(receipt, "desired_control", None), "value", desired_control)
            quiescent = bool(getattr(receipt, "quiescent", False))
            phase = getattr(receipt, "phase", None)
            receipt_revision = getattr(receipt, "revision", None)
            if isinstance(receipt_revision, int) and receipt_revision > 0:
                revision = receipt_revision
        emit(
            OwnerResponseEvent.control(
                request.request_id,
                sequence,
                task_id=task_id,
                action=action,
                acknowledged=acknowledged,
                requested=(getattr(receipt, "requested", None) if receipt is not None else None),
                applied=applied,
                actual_status=actual_status,
                desired_control=desired_control,
                revision=revision if isinstance(revision, int) and revision > 0 else None,
                quiescent=quiescent,
                phase=phase,
            )
        )
        sequence += 1
        control_applied = applied is not False
        terminal_status = (
            OwnerTerminalStatus.COMPLETED
            if exit_code == 0 and control_applied
            else OwnerTerminalStatus.FAILED
        )
        terminal_exit_code = exit_code if exit_code != 0 or control_applied else 2
        emit(
            OwnerResponseEvent.terminal(
                request.request_id,
                sequence,
                status=terminal_status,
                task_id=task_id,
                exit_code=terminal_exit_code,
                result={**serialised, "output": output.getvalue()} if isinstance(serialised, dict) else {"value": serialised},
                error=(
                    {"phase": phase, "message": "task control is still pending"}
                    if applied is False and phase == "pending"
                    else None
                ),
            )
        )
        if notify_tasks is not None:
            notify_tasks()
    except Exception as error:
        emit(
            OwnerResponseEvent.terminal(
                request.request_id,
                sequence,
                status=OwnerTerminalStatus.FAILED,
                task_id=task_id,
                exit_code=1,
                error={"message": str(error)},
            )
        )

def _recover_interrupted_downloads(
    task_store: StateTaskStore,
    batch_store: StateBatchStore | None = None,
    history_store: object | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Release stale startup leases and rebuild resumable shell commands."""

    commands = []
    for record in sorted(task_store.list(), key=lambda value: (value.created_at, value.task_id)):
        # Batch roots are recovered together with their execution graph below;
        # handling them here would split the root transition across two
        # transactions and could enqueue both item and batch resumes.
        if record.kind is TaskKind.BATCH:
            continue
        if record.status is TaskStatus.PAUSED:
            record = record.transition(
                active_run_id=None,
                desired_control=TaskDesiredControl.PAUSE,
                metadata={**record.metadata, "execution_fence": uuid4().hex},
            )
            task_store.upsert(record)
        if (
            record.status not in {TaskStatus.PLANNED, TaskStatus.RUNNING}
            or not record.source_url
            or record.metadata.get("scheduler_managed") is not True
        ):
            continue
        if record.status is TaskStatus.PAUSED:
            continue
        if record.status is TaskStatus.RUNNING:
            # The old owner no longer exists after a restart.  Do not leave
            # its run lease attached to the resumable record: the next
            # ``tasks resume`` must be able to claim a fresh generation.
            task_store.upsert(
                record.transition(
                    status=TaskStatus.PLANNED,
                    desired_control=TaskDesiredControl.RUN,
                    active_run_id=None,
                    metadata={**record.metadata, "execution_fence": uuid4().hex},
                )
            )
        elif record.status is TaskStatus.PLANNED and record.active_run_id is not None:
            # A planned task can also contain a stale lease when the previous
            # owner stopped between its checkpoint and durable status write.
            task_store.upsert(
                record.transition(
                    desired_control=TaskDesiredControl.RUN,
                    active_run_id=None,
                    metadata={**record.metadata, "execution_fence": uuid4().hex},
                )
            )
        commands.append(("tasks", "resume", record.task_id, "--json"))

    if batch_store is not None:
        for parent_id in batch_store.list_parent_ids():
            parent_record = task_store.get(parent_id)
            if parent_record is None or parent_record.kind is not TaskKind.BATCH:
                continue
            if parent_record.status in {
                TaskStatus.PENDING,
                TaskStatus.PLANNED,
                TaskStatus.RUNNING,
                TaskStatus.PAUSED,
            }:
                parent_record = batch_store.recover_interrupted_root(
                    parent_id,
                    execution_fence=uuid4().hex,
                )
                if parent_record is not None and parent_record.status in {
                    TaskStatus.PENDING,
                    TaskStatus.PLANNED,
                }:
                    commands.append(("batches", "resume", parent_id))
    return tuple(commands)
