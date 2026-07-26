"""Shared progress normalization and presentation primitives."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite


def normalise_byte_progress(
    completed: object,
    total: object,
) -> tuple[int, int | None]:
    """Return safe payload byte values from client, checkpoint, or event data.

    ``downloaded`` counters from torrent clients can include retransfers and
    discarded pieces, so the value shown to users must never exceed the
    selected payload size. Unknown or malformed totals remain unknown instead
    of being treated as zero-byte completed work.
    """

    completed_bytes = _non_negative_int(completed, default=0)
    total_bytes = _non_negative_int(total, default=None)
    if total_bytes is not None:
        completed_bytes = min(completed_bytes, total_bytes)
    return completed_bytes, total_bytes


def normalise_progress(value: object, *, default: float = 0.0) -> float:
    """Return a finite fractional progress value in the inclusive 0..1 range."""

    try:
        progress = float(value)
    except (TypeError, ValueError, OverflowError):
        progress = default
    if not isfinite(progress):
        progress = default
    return min(1.0, max(0.0, progress))


def byte_percentage(completed: object, total: object) -> int:
    """Calculate a clamped integer percentage for a known byte total."""

    completed_bytes, total_bytes = normalise_byte_progress(completed, total)
    if total_bytes is None or total_bytes <= 0:
        return 0
    return min(100, max(0, int(completed_bytes / total_bytes * 100)))


def checkpoint_byte_progress(checkpoint: Mapping[str, object]) -> tuple[int, int | None]:
    """Extract safe byte progress from a persisted checkpoint mapping."""

    return normalise_byte_progress(
        checkpoint.get("completed_bytes", checkpoint.get("bytes_received")),
        checkpoint.get("total_bytes"),
    )


def _non_negative_int(value: object, *, default: int | None) -> int | None:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, result)
