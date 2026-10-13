from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cli_downloader.core.errors import PluginValidationError


_PLUGIN_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class PluginEntrypoint:
    file_name: str
    class_name: str

    @property
    def value(self) -> str:
        return f"{self.file_name}:{self.class_name}"


@dataclass(frozen=True)
class PluginPackageMetadata:
    plugin_id: str
    display_name: str
    version: str
    entrypoint: PluginEntrypoint
    manifest: str
    description: str | None = None
    requires_python: str | None = None
    requires_core_api: str | None = None

    def manifest_path(self, plugin_folder: str | Path) -> Path:
        return Path(plugin_folder) / "manifests" / self.manifest

    def entrypoint_path(self, plugin_folder: str | Path) -> Path:
        return Path(plugin_folder) / self.entrypoint.file_name

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name,
            "version": self.version,
            "entrypoint": self.entrypoint.value,
            "manifest": self.manifest,
            "description": self.description,
            "requires_python": self.requires_python,
            "requires_core_api": self.requires_core_api,
        }


def plugin_metadata_from_json_file(path: str | Path) -> PluginPackageMetadata:
    metadata_path = Path(path)
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PluginValidationError(
            f"Cannot read plugin metadata JSON in {metadata_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PluginValidationError(
            f"Invalid plugin metadata JSON in {metadata_path}: {exc.msg}"
        ) from exc
    return plugin_metadata_from_dict(payload, source=str(metadata_path))


def plugin_metadata_from_dict(
    payload: Mapping[str, Any],
    *,
    source: str = "plugin.json",
) -> PluginPackageMetadata:
    data = _require_mapping(payload, source)
    plugin_id = _require_non_empty_string(data, "plugin_id", source)
    if _PLUGIN_ID_PATTERN.fullmatch(plugin_id) is None:
        raise PluginValidationError(
            f"{source}.plugin_id must use lowercase letters, numbers, hyphens, or underscores"
        )

    requires_python = _optional_requirement(data, "requires_python", source)
    requires_core_api = _optional_requirement(data, "requires_core_api", source)
    return PluginPackageMetadata(
        plugin_id=plugin_id,
        display_name=_require_non_empty_string(data, "display_name", source),
        version=_require_non_empty_string(data, "version", source),
        entrypoint=_parse_entrypoint(
            _require_non_empty_string(data, "entrypoint", source),
            f"{source}.entrypoint",
        ),
        manifest=_validate_manifest_filename(
            _require_non_empty_string(data, "manifest", source),
            f"{source}.manifest",
        ),
        description=data.get("description"),
        requires_python=requires_python,
        requires_core_api=requires_core_api,
    )


def metadata_compatibility_error(metadata: PluginPackageMetadata) -> str | None:
    """Return a diagnostic for version constraints without importing plugin code."""
    if metadata.requires_python and not _matches_requirement(
        metadata.requires_python,
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    ):
        return f"requires_python {metadata.requires_python!r} is not satisfied"
    if metadata.requires_core_api:
        from cli_downloader import __version__ as core_version

        if not _matches_requirement(metadata.requires_core_api, core_version):
            return f"requires_core_api {metadata.requires_core_api!r} is not satisfied"
    return None


def _parse_entrypoint(value: str, source: str) -> PluginEntrypoint:
    if value.count(":") != 1:
        raise PluginValidationError(f"{source} must use file.py:ClassName form")
    file_name, class_name = value.split(":", 1)
    if not _is_safe_file_name(file_name) or not file_name.endswith(".py"):
        raise PluginValidationError(f"{source} file must be a local .py filename")
    if not class_name.isidentifier():
        raise PluginValidationError(f"{source} class name must be a Python identifier")
    return PluginEntrypoint(file_name=file_name, class_name=class_name)


def _validate_manifest_filename(value: str, source: str) -> str:
    if not _is_safe_file_name(value) or not value.endswith(".json"):
        raise PluginValidationError(f"{source} must be a local .json filename inside manifests/")
    return value


def _is_safe_file_name(value: str) -> bool:
    if value in {"", ".", ".."}:
        return False
    return "/" not in value and "\\" not in value and Path(value).name == value


def _require_mapping(payload: Any, source: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise PluginValidationError(f"{source} must be an object")
    return payload


def _require_non_empty_string(
    data: Mapping[str, Any],
    key: str,
    source: str,
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or value.strip() == "":
        raise PluginValidationError(f"{source}.{key} must be a non-empty string")
    return value


def _optional_requirement(data: Mapping[str, Any], key: str, source: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PluginValidationError(f"{source}.{key} must be a version requirement string")
    if not re.fullmatch(
        r"\s*(?:~=|==|!=|>=|<=|>|<)?\s*\d+(?:\.\d+)*(?:\s*,\s*(?:~=|==|!=|>=|<=|>|<)?\s*\d+(?:\.\d+)*)*\s*",
        value,
    ):
        raise PluginValidationError(f"{source}.{key} must contain version clauses such as >=3.11")
    return value.strip()


def _matches_requirement(requirement: str, version: str) -> bool:
    current = _version_tuple(version)
    for clause in requirement.split(","):
        match = re.fullmatch(r"\s*(~=|==|!=|>=|<=|>|<)?\s*(\d+(?:\.\d+)*)\s*", clause)
        if match is None:
            return False
        operator = match.group(1) or "=="
        expected = _version_tuple(match.group(2))
        width = max(len(current), len(expected))
        comparable_current = current + (0,) * (width - len(current))
        comparable_expected = expected + (0,) * (width - len(expected))
        if operator == "==" and comparable_current != comparable_expected:
            return False
        if operator == "!=" and comparable_current == comparable_expected:
            return False
        if operator == ">=" and comparable_current < comparable_expected:
            return False
        if operator == "<=" and comparable_current > comparable_expected:
            return False
        if operator == ">" and comparable_current <= comparable_expected:
            return False
        if operator == "<" and comparable_current >= comparable_expected:
            return False
        if operator == "~=" and (
            comparable_current < comparable_expected or current[:1] != expected[:1]
        ):
            return False
    return True


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))
