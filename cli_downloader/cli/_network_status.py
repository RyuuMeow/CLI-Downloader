"""Read the live owner's compact, credential-free HTTP diagnostics."""

import json
from uuid import uuid4

from cli_downloader import __version__
from cli_downloader.core.environment import diagnose_environment
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerCommand,
    OwnerEventKind,
    OwnerHandshake,
    OwnerMode,
    OwnerRequest,
    OwnerResponseMode,
    OwnerSource,
)
from cli_downloader.core.owner_scope import OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeClient, OwnerTransportError
from cli_downloader.core.shell_control import ShellController, ShellStatusKind


def print_network_status() -> int:
    scope = OwnerScope.from_app_paths(diagnose_environment().paths)
    snapshot = ShellController(scope).status()
    if snapshot.kind is not ShellStatusKind.READY:
        print(json.dumps({"owner_available": False, "sites": []}))
        return 0
    session = f"cli-{uuid4().hex}"
    request = OwnerRequest(
        OWNER_PROTOCOL_VERSION,
        uuid4().hex,
        OwnerSource.CLI,
        OwnerCommand.SHELL_STATUS,
        OwnerResponseMode.WAIT,
        {},
        session,
    )
    try:
        events = OwnerPipeClient(scope).request(
            request,
            handshake=OwnerHandshake(
                OWNER_PROTOCOL_VERSION,
                session,
                OwnerMode.FOREGROUND,
                __version__,
                ("network_status",),
                ("network_status",),
                scope.scope_fingerprint,
            ),
        )
        terminal = next(event for event in events if event.kind is OwnerEventKind.TERMINAL)
        print(json.dumps(terminal.to_dict()["payload"]["result"], ensure_ascii=False, indent=2))
        return 0
    except (OwnerTransportError, StopIteration, KeyError) as error:
        print(json.dumps({"owner_available": True, "error": str(error)}, ensure_ascii=False))
        return 1
