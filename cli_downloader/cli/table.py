"""Shared table specifications for CLI and Textual command output."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import sys
from typing import Literal, TextIO

from rich.cells import cell_len
from rich.console import Console
from rich.table import Table


TableJustify = Literal["left", "center", "right"]


@dataclass(frozen=True)
class TableColumn:
    label: str
    style: str | None = None
    justify: TableJustify = "left"
    no_wrap: bool = True
    overflow: Literal["fold", "crop", "ellipsis"] = "ellipsis"


@dataclass(frozen=True)
class TableSpec:
    columns: tuple[TableColumn, ...]
    rows: tuple[tuple[str, ...], ...] = ()
    title: str | None = None
    show_header: bool = True


def build_rich_table(spec: TableSpec) -> Table:
    """Build a Rich table while keeping column behavior in one place."""

    table = Table(title=spec.title, show_header=spec.show_header)
    for column in spec.columns:
        table.add_column(
            column.label,
            style=column.style,
            justify=column.justify,
            no_wrap=column.no_wrap,
            overflow=column.overflow,
        )
    for row in spec.rows:
        table.add_row(*row)
    return table


def print_rich_table(spec: TableSpec, *, file: TextIO | None = None) -> None:
    """Print a shared table to a CLI stream."""

    target = file or sys.stdout
    width = None if target.isatty() else _natural_width(spec)
    Console(file=file, width=width).print(build_rich_table(spec))


def render_table_text(spec: TableSpec, *, width: int | None = None) -> str:
    """Render a table without terminal control sequences for Shell capture."""

    stream = StringIO()
    console = Console(
        file=stream,
        record=True,
        force_terminal=False,
        color_system=None,
        width=width or _natural_width(spec),
    )
    console.print(build_rich_table(spec))
    return stream.getvalue().rstrip("\n")


def _natural_width(spec: TableSpec) -> int:
    """Keep captured output intact; interactive terminals still use Rich sizing."""

    widths = []
    for index, column in enumerate(spec.columns):
        values = [column.label]
        values.extend(row[index] for row in spec.rows if index < len(row))
        widths.append(max((cell_len(value) for value in values), default=1))
    return max(80, sum(widths) + (3 * max(len(widths) - 1, 0)) + 4)
