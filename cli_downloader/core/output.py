from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from string import Formatter
from typing import Mapping

from cli_downloader.core.errors import ConfigValidationError


COMMON_OUTPUT_FIELDS = frozenset({"site_id", "gallery_id", "title", "task_id"})


def validate_folder_template(template: object, extra_fields: tuple[str, ...] = ()) -> bool:
    if not isinstance(template, str) or not template.strip():
        raise ConfigValidationError("download.folder_template must be a non-empty string")
    if PureWindowsPath(template).is_absolute() or PurePosixPath(template).is_absolute():
        raise ConfigValidationError("download.folder_template must be relative")
    try:
        names = {
            name
            for _literal, name, _format_spec, _conversion in Formatter().parse(template)
            if name
        }
        rendered = template.format_map({name: "value" for name in names})
    except (KeyError, ValueError) as error:
        raise ConfigValidationError(f"invalid download.folder_template: {error}") from error
    unknown = names - COMMON_OUTPUT_FIELDS - set(extra_fields)
    if unknown:
        raise ConfigValidationError(
            f"download.folder_template has unsupported fields: {', '.join(sorted(unknown))}"
        )
    parts = PureWindowsPath(rendered.replace("/", "\\")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ConfigValidationError("download.folder_template cannot escape the download root")
    return True


def output_folder_fields(
    *,
    site_id: str,
    gallery_id: str,
    title: str,
    metadata: Mapping[str, object],
    declared_fields: tuple[str, ...] = (),
) -> dict[str, str]:
    fields = {"site_id": site_id, "gallery_id": gallery_id, "title": title}
    for name in declared_fields:
        value = metadata.get(name)
        if isinstance(value, (str, int, float, bool)):
            fields[name] = str(value)
    return fields


def resolve_output_template(
    values: Mapping[str, object],
    *,
    plugin_id: str,
    plugin_default: str | None,
    declared_fields: tuple[str, ...] = (),
) -> str:
    override = values.get(f"plugins.{plugin_id}.download.folder_template")
    template = override or plugin_default or values.get("download.folder_template")
    validate_folder_template(template, declared_fields)
    return str(template)
