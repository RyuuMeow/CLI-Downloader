from __future__ import annotations

from typing import Protocol


class CompletionStore(Protocol):
    """Own duplicate-download decisions independently from History events."""

    def record_completed(self, site_id: str, content_key: str) -> None: ...

    def is_completed(self, site_id: str, content_key: str) -> bool: ...

    def forget(self, site_id: str, content_key: str) -> bool: ...


class InMemoryCompletionStore:
    def __init__(self) -> None:
        self._completed: set[tuple[str, str]] = set()

    def record_completed(self, site_id: str, content_key: str) -> None:
        self._completed.add((site_id, content_key))

    def is_completed(self, site_id: str, content_key: str) -> bool:
        return (site_id, content_key) in self._completed

    def forget(self, site_id: str, content_key: str) -> bool:
        identity = (site_id, content_key)
        if identity not in self._completed:
            return False
        self._completed.remove(identity)
        return True
