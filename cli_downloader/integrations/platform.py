from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformIntegration:
    platform: str

    def can_open_path(self, path: Path) -> bool:
        return path.exists()

    def describe_open_path(self, path: Path) -> str:
        return f"open path: {path}"
