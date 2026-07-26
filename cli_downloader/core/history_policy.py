from __future__ import annotations

from cli_downloader.core.events import TaskEvent


_CONTROL_REASONS = frozenset(
    {
        "task_pause_requested",
        "task_pause_acknowledged",
        "task_resume_requested",
        "task_cancel_requested",
    }
)
_TRANSIENT_REASONS = frozenset(
    {
        "byte_progress",
        "entry_queued",
        "child_queued",
        "entry_resolving",
        "child_downloading",
        "gallery_resolved",
        "package_built",
        "request_rate_limited",
        "asset_resumed",
        "torrent_added",
        "torrent_progress",
    }
)
_SEMANTIC_BATCH_REASONS = frozenset(
    {
        "batch_started",
        "batch_finished",
        "entry_resolution_failed",
        "package_handoff_failed",
        "child_completed",
        "child_partially_completed",
        "child_failed",
        "child_cancelled",
    }
)


def should_persist_history_event(event: TaskEvent) -> bool:
    """Return whether an event is a durable audit milestone.

    Unknown event types remain persisted so plugins can add semantic audit events. Only
    known live-progress streams and control chatter are dropped at the durable boundary.
    """
    if event.name in {"stage_started", "stage_finished"}:
        return False
    if event.reason in _CONTROL_REASONS or event.reason in _TRANSIENT_REASONS:
        return False
    if event.name == "batch_progress" and event.reason not in _SEMANTIC_BATCH_REASONS:
        return False
    if event.name == "task_transitioned" and event.reason in _CONTROL_REASONS:
        return False
    return True
