"""Shared presentation resources owned by the Textual front ends."""

from importlib.resources import files

from textual.theme import Theme


CLI_DOWNLOADER_THEME = Theme(
    name="cli-downloader",
    primary="#e58a32",
    secondary="#999999",
    warning="#ffaa55",
    error="#d96c6c",
    success="#67b279",
    accent="#ffaa55",
    foreground="#e8e8e8",
    background="#171717",
    surface="#222222",
    panel="#222222",
    dark=True,
)


def load_stylesheet(name: str) -> str:
    """Load a packaged Textual stylesheet by filename."""

    return files("cli_downloader.cli.styles").joinpath(name).read_text(encoding="utf-8")


# Host styles load first; shared Core page rules follow so host-specific layout
# overrides remain explicit and deterministic.
CORE_PAGE_CSS = load_stylesheet("core_page.tcss")
SHELL_CSS = load_stylesheet("shell.tcss") + "\n" + CORE_PAGE_CSS
CORE_APP_CSS = "Screen { padding: 1 2; }\n" + CORE_PAGE_CSS
