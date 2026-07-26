from __future__ import annotations

from typing import Protocol

from cli_downloader.core.events import TaskEvent


class HistoryStore(Protocol):
    def append(self, event: TaskEvent) -> None: ...

    def append_many(self, events: tuple[TaskEvent, ...]) -> None: ...

    def list(self, *, task_id: str | None = None) -> tuple[TaskEvent, ...]: ...

    def delete_task(self, task_id: str) -> None: ...

    def delete_older_than(self, days: float) -> None: ...

    def clear(self) -> None: ...


class InMemoryHistoryStore:
    def __init__(self) -> None:
        self._events: list[TaskEvent] = []

    def append(self, event: TaskEvent) -> None:
        self._events.append(event)

    def append_many(self, events: tuple[TaskEvent, ...]) -> None:
        self._events.extend(events)

    def list(self, *, task_id: str | None = None) -> tuple[TaskEvent, ...]:
        if task_id is None:
            return tuple(self._events)
        return tuple(event for event in self._events if event.task_id == task_id)

    def delete_task(self, task_id: str) -> None:
        self._events = [event for event in self._events if event.task_id != task_id]

    def delete_older_than(self, days: float) -> None:
        pass  # In-memory store does not need to support date-based deletion for tests

    def clear(self) -> None:
        self._events.clear()
