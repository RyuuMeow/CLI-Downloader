from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


def application_root() -> Path:
    """Return the read-only application payload root.

    PyInstaller stores imported modules below its private runtime directory, so
    ``__file__`` is not the directory users extracted or installed.  External
    assets and development plugins must be resolved beside the executable.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def installed_plugins_root(*, local_app_data: str | Path | None = None) -> Path:
    """Return the mutable, per-user plugin installation root."""
    local_root = (
        Path(local_app_data)
        if local_app_data is not None
        else Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    )
    return local_root / "CLI-Downloader" / "plugins"


@dataclass(frozen=True)
class AppPaths:
    mode: str
    data_root: Path
    config_dir: Path
    cache_dir: Path
    state_dir: Path
    downloads_dir: Path
    logs_dir: Path

    @classmethod
    def discover(cls) -> "AppPaths":
        portable_root = os.environ.get("CLI_DOWNLOADER_PORTABLE_ROOT")
        if portable_root:
            root = Path(portable_root).expanduser()
            mode = "portable"
        else:
            root = Path(
                os.environ.get("CLI_DOWNLOADER_DATA_ROOT", "~/.cli-downloader")
            ).expanduser()
            mode = "user"

        return cls(
            mode=mode,
            data_root=root,
            config_dir=root / "config",
            cache_dir=root / "cache",
            state_dir=root / "state",
            downloads_dir=root / "downloads",
            logs_dir=root / "logs",
        )


@dataclass(frozen=True)
class TerminalCapabilities:
    is_tty: bool
    color: bool
    hyperlink: bool
    interactive: bool

    @classmethod
    def detect(cls) -> "TerminalCapabilities":
        term = os.environ.get("TERM", "")
        wt_session = bool(os.environ.get("WT_SESSION"))
        color_term = bool(os.environ.get("COLORTERM"))
        is_tty = sys.stdout.isatty()

        return cls(
            is_tty=is_tty,
            color=is_tty and (color_term or term not in {"", "dumb"}),
            hyperlink=is_tty and (wt_session or "xterm" in term.lower()),
            interactive=sys.stdin.isatty() and is_tty,
        )


@dataclass(frozen=True)
class EnvironmentContext:
    paths: AppPaths
    terminal: TerminalCapabilities
    python: str
    platform: str

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["paths"] = {key: str(value) for key, value in payload["paths"].items()}
        return payload


def diagnose_environment() -> EnvironmentContext:
    return EnvironmentContext(
        paths=AppPaths.discover(),
        terminal=TerminalCapabilities.detect(),
        python=sys.version.split()[0],
        platform=sys.platform,
    )
