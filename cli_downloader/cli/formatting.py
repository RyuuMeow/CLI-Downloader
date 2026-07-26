from __future__ import annotations

from rich.cells import cell_len

SUMMARY_TITLE_LIMIT = 20


def abbreviate_summary_title(title: str, *, limit: int = SUMMARY_TITLE_LIMIT) -> str:
    """Keep summary-table titles bounded without changing the stored value."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(title) <= limit:
        return title
    return f"{title[:limit]}..."


def display_width(value: str) -> int:
    """Return terminal cell width for CJK, emoji, and combining characters."""

    return cell_len(value)


def truncate_display(value: str, limit: int, *, suffix: str = "…") -> str:
    """Truncate a cell at a terminal-width boundary without splitting wide glyphs."""

    if limit < 1:
        raise ValueError("limit must be positive")
    if cell_len(value) <= limit:
        return value
    suffix_width = cell_len(suffix)
    available = max(0, limit - suffix_width)
    result: list[str] = []
    width = 0
    for character in value:
        character_width = cell_len(character)
        if width + character_width > available:
            break
        result.append(character)
        width += character_width
    return "".join(result) + suffix
