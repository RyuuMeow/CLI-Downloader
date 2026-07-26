"""Best-effort durable reporting for otherwise unhandled application errors."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
import traceback

from cli_downloader.core.environment import AppPaths


_WRITE_LOCK = Lock()


def write_crash_log(
    error: BaseException,
    *,
    context: str,
    logs_dir: Path | None = None,
) -> Path | None:
    """Append one traceback without allowing logging failure to mask the error."""
    target_dir = logs_dir or AppPaths.discover().logs_dir
    target = target_dir / "crash.log"
    record = "".join(
        (
            f"[{datetime.now(UTC).isoformat()}] {context}\n",
            "".join(traceback.format_exception(error)),
            "\n",
        )
    )
    try:
        with _WRITE_LOCK:
            target_dir.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as stream:
                stream.write(record)
    except OSError:
        return None
    return target
