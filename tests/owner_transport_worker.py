"""Importable process workers for owner transport lifecycle tests."""

from __future__ import annotations

from datetime import UTC, datetime

from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerHandshake,
    OwnerMode,
    OwnerResponseEvent,
)
from cli_downloader.core.owner_scope import OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeServer


def orphan_owner_process(directory: str, ready, accepted, hold) -> None:
    """Keep an accepted WAIT request alive until the parent simulates a crash."""

    scope = OwnerScope.from_data_root(directory)
    owner_session = "orphan-owner"

    def handler(request, emit):
        emit(
            OwnerResponseEvent.accepted(
                request.request_id,
                1,
                owner_session_id=owner_session,
                accepted_at=datetime.now(UTC),
                task_id="orphan-task",
                task_kind="item",
            )
        )
        accepted.set()
        hold.wait(30)

    server = OwnerPipeServer(
        scope,
        handshake=OwnerHandshake(
            OWNER_PROTOCOL_VERSION,
            owner_session,
            OwnerMode.SHELL,
            "0.1.5",
            ("download",),
            scope_fingerprint=scope.scope_fingerprint,
        ),
        handler=handler,
    )
    server.start()
    ready.set()
    try:
        hold.wait(30)
    finally:
        server.stop()
