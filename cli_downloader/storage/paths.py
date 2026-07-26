from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cli_downloader.core.environment import AppPaths


@dataclass(frozen=True)
class StorageLayout:
    state_db: Path
    downloads_dir: Path
    cache_dir: Path
    logs_dir: Path

    @classmethod
    def from_app_paths(cls, paths: AppPaths) -> "StorageLayout":
        return cls(
            state_db=paths.state_dir / "state.sqlite3",
            downloads_dir=paths.downloads_dir,
            cache_dir=paths.cache_dir,
            logs_dir=paths.logs_dir,
        )
