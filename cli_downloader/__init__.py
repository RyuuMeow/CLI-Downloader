"""CLI-Downloader package metadata."""

from importlib.metadata import PackageNotFoundError, metadata, version
from pathlib import Path
from typing import Any

import tomllib

__all__ = ["__version__"]


def _source_project() -> dict[str, Any]:
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        with project_file.open("rb") as stream:
            return tomllib.load(stream).get("project", {})
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return {}


_SOURCE_PROJECT = _source_project()
_SOURCE_VERSION = _SOURCE_PROJECT.get("version")
if isinstance(_SOURCE_VERSION, str) and _SOURCE_VERSION:
    # A source checkout is authoritative even when an editable install left
    # older dist-info behind. Packaged builds include this same pyproject file.
    __version__ = _SOURCE_VERSION
else:
    try:
        __version__ = version("cli-downloader")
    except PackageNotFoundError:
        __version__ = "0+unknown"


def project_url(name: str) -> str | None:
    """Return a declared project URL without duplicating release metadata."""

    try:
        entries = metadata("cli-downloader").get_all("Project-URL") or ()
    except PackageNotFoundError:
        entries = ()
    for entry in entries:
        label, separator, url = entry.partition(",")
        if separator and label.strip().lower() == name.lower():
            return url.strip() or None
    urls = _SOURCE_PROJECT.get("urls", {})
    value = urls.get(name) if isinstance(urls, dict) else None
    return str(value) if value else None
