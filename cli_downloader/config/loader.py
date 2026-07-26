from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli_downloader.config.schema import ConfigSchema, SettingsRegistry, default_schema
from cli_downloader.core.environment import AppPaths
from cli_downloader.core.errors import ConfigValidationError


@dataclass(frozen=True)
class LoadedConfig:
    values: dict[str, Any]
    secrets: dict[str, Any]
    sources: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": self.values,
            "secrets": {key: "<redacted>" for key in self.secrets},
            "sources": [str(source) for source in self.sources],
        }


class ConfigLoader:
    def __init__(
        self,
        *,
        paths: AppPaths | None = None,
        schema: ConfigSchema | None = None,
        registry: SettingsRegistry | None = None,
    ) -> None:
        self._paths = paths or AppPaths.discover()
        self._registry = registry
        self._schema = registry.schema if registry is not None else schema or default_schema()

    def load(self) -> LoadedConfig:
        config_path = self._paths.config_dir / "config.toml"
        secrets_path = self._paths.config_dir / "secrets.toml"

        values = self._schema.defaults()
        sources: list[Path] = []

        if config_path.exists():
            values.update(_flatten_toml(_read_toml(config_path)))
            sources.append(config_path)

        secrets: dict[str, Any] = {}
        if secrets_path.exists():
            secrets = _flatten_toml(_read_toml(secrets_path))
            sources.append(secrets_path)

        if self._registry is not None:
            self._registry.validate(values, secrets)
        else:
            self._schema.validate(values)

        return LoadedConfig(
            values=values,
            secrets=secrets,
            sources=tuple(sources),
        )

    def save(self, values: dict[str, Any], secrets: dict[str, Any]) -> LoadedConfig:
        if self._registry is not None:
            self._registry.validate(values, secrets)
        else:
            self._schema.validate(values)
        self._paths.config_dir.mkdir(parents=True, exist_ok=True)
        _write_toml(self._paths.config_dir / "config.toml", values)
        _write_toml(self._paths.config_dir / "secrets.toml", secrets)
        return self.load()


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _flatten_toml(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in data.items():
        dotted_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flattened.update(_flatten_toml(value, dotted_key))
        else:
            flattened[dotted_key] = value
    return flattened


def _write_toml(path: Path, values: dict[str, Any]) -> None:
    tables: dict[str, list[tuple[str, Any]]] = {}
    for dotted_key, value in sorted(values.items()):
        if value is None:
            continue
        table, _, key = dotted_key.rpartition(".")
        tables.setdefault(table, []).append((key, value))

    lines: list[str] = []
    for table, entries in tables.items():
        if table:
            if lines:
                lines.append("")
            lines.append(f"[{table}]")
        lines.extend(f"{key} = {_toml_value(value)}" for key, value in entries)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    temporary.replace(path)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise ConfigValidationError(f"unsupported config value type: {type(value).__name__}")
