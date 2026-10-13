from __future__ import annotations

import os

from cli_downloader.core.owner_scope import OwnerScope


def hold_owner_lock(root: str, ready: object, release: object, crash: bool) -> None:
    scope = OwnerScope.from_data_root(root, user_identity="S-1-5-21-test")
    with scope.lock().acquire():
        ready.set()  # type: ignore[attr-defined]
        if crash:
            os._exit(23)
        release.wait(10)  # type: ignore[attr-defined]
