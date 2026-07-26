from pathlib import Path

from cli_downloader.core.environment import AppPaths, EnvironmentContext, TerminalCapabilities


def make_environment(
    root: Path, *, mode: str = "test", interactive: bool = False
) -> EnvironmentContext:
    return EnvironmentContext(
        paths=AppPaths(
            mode=mode,
            data_root=root,
            config_dir=root / "config",
            cache_dir=root / "cache",
            state_dir=root / "state",
            downloads_dir=root / "downloads",
            logs_dir=root / "logs",
        ),
        terminal=TerminalCapabilities(
            is_tty=interactive,
            color=interactive,
            hyperlink=interactive,
            interactive=interactive,
        ),
        python="test",
        platform="test-platform",
    )
