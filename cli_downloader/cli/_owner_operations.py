"""Typed plugin-operation forwarding through the live network owner."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from threading import Event, Thread
from uuid import uuid4

from cli_downloader import __version__
from cli_downloader.cli.rendering import RenderMode, serialize_operation_result
from cli_downloader.core.environment import diagnose_environment
from cli_downloader.core.operations import ResultKind
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerEventKind,
    OwnerHandshake,
    OwnerMode,
    OwnerRequest,
    OwnerResponseEvent,
    OwnerResponseMode,
    OwnerSource,
    OwnerTerminalStatus,
)
from cli_downloader.core.owner_scope import OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeClient, OwnerTransportError
from cli_downloader.core.shell_control import ShellController, ShellStatusKind


def forward_operation(args, *, confirmations, emit_error):
    scope = OwnerScope.from_app_paths(diagnose_environment().paths)
    snapshot = ShellController(scope).status()
    if snapshot.metadata is None or snapshot.metadata.owner_mode is not OwnerMode.SHELL:
        return None
    if snapshot.kind not in {ShellStatusKind.READY, ShellStatusKind.RECOVERING}:
        # The caller must acquire the OS lease before any local fallback.
        return None
    params = dict(argument.split("=", 1) for argument in args.args if "=" in argument)
    session_id = f"cli-{uuid4().hex}"
    request = OwnerRequest(
        protocol_version=OWNER_PROTOCOL_VERSION,
        request_id=uuid4().hex,
        source=OwnerSource.CLI,
        command=OwnerCommand.OPERATION,
        response_mode=OwnerResponseMode.WAIT,
        client_session_id=session_id,
        payload={
            "plugin_id": args.plugin_id,
            "operation_id": args.operation_id,
            "params": params,
            "confirmations": confirmations,
            "json": bool(args.json),
        },
    )
    handshake = OwnerHandshake(
        protocol_version=OWNER_PROTOCOL_VERSION,
        session_id=session_id,
        mode=OwnerMode.FOREGROUND,
        application_version=__version__,
        capabilities=("operation",),
        required_capabilities=("operation",),
        scope_fingerprint=scope.scope_fingerprint,
    )
    try:
        if "operation" not in snapshot.metadata.capabilities:
            raise OwnerTransportError(
                "live owner lacks the operation capability; restart the Shell with this version"
            )
        events = OwnerPipeClient(scope).request(request, handshake=handshake)
        terminal = next((e for e in events if e.kind is OwnerEventKind.TERMINAL), None)
        if terminal is None:
            raise OwnerTransportError(
                "owner operation has no terminal result; inspect the request before resubmitting"
            )
        payload = terminal.to_dict()["payload"]
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("output"), str):
            sys.stdout.write(result["output"].rstrip("\n") + "\n")
        else:
            print(json.dumps(payload, ensure_ascii=False))
        return int(payload.get("exit_code", 1))
    except OwnerTransportError as error:
        emit_error(error, json_output=args.json)
        return 1


def handle_operation(request, emit, *, owner_session_id, run_operation):
    if request.response_mode is not OwnerResponseMode.WAIT:
        raise ValueError("plugin operations require wait response mode")
    emit(
        OwnerResponseEvent.accepted(
            request.request_id,
            1,
            owner_session_id=owner_session_id,
            accepted_at=datetime.now(UTC),
            queued=True,
        )
    )
    finished = Event()
    outcome = {}

    def execute():
        try:
            result = run_operation(request)
            outcome["exit_code"] = 1 if result.result_kind is ResultKind.ERROR else 0
            mode = RenderMode.JSON if request.payload.get("json") else RenderMode.PLAIN
            outcome["result"] = {"output": serialize_operation_result(result, mode)}
        except asyncio.CancelledError:
            outcome["exit_code"] = 1
            outcome["error"] = {"message": "Owner operation was cancelled"}
        except Exception as error:
            outcome["exit_code"] = 1
            outcome["error"] = {"message": str(error)}
        finally:
            finished.set()

    Thread(target=execute, name=f"owner-operation-{request.request_id[:8]}", daemon=True).start()
    sequence = 2
    while not finished.wait(5):
        try:
            emit(
                OwnerResponseEvent.heartbeat(
                    request.request_id,
                    sequence,
                    owner_session_id=owner_session_id,
                    emitted_at=datetime.now(UTC),
                )
            )
        except (OSError, EOFError):
            pass
        sequence += 1
    emit(
        OwnerResponseEvent.terminal(
            request.request_id,
            sequence,
            status=OwnerTerminalStatus.COMPLETED
            if outcome["exit_code"] == 0
            else OwnerTerminalStatus.FAILED,
            **outcome,
        )
    )
