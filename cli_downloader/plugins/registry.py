import json
from pathlib import Path
from typing import Dict, Any


def get_registry_path(plugins_dir: Path) -> Path:
    return plugins_dir / "registry.json"


def load_registry(plugins_dir: Path) -> Dict[str, Any]:
    registry_path = get_registry_path(plugins_dir)
    if not registry_path.exists():
        return {}
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_registry(plugins_dir: Path, registry: Dict[str, Any]) -> None:
    registry_path = get_registry_path(plugins_dir)
    plugins_dir.mkdir(parents=True, exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)


def register_plugin(
    plugins_dir: Path,
    plugin_id: str,
    git_url: str,
    *,
    source: str = "custom",
    catalog_id: str | None = None,
) -> None:
    registry = load_registry(plugins_dir)
    registry[plugin_id] = {
        "git_url": git_url,
        "is_remote": True,
        "source": source,
        "catalog_id": catalog_id,
    }
    save_registry(plugins_dir, registry)


def unregister_plugin(plugins_dir: Path, plugin_id: str) -> None:
    registry = load_registry(plugins_dir)
    if plugin_id in registry:
        del registry[plugin_id]
        save_registry(plugins_dir, registry)


def get_plugin_git_url(plugins_dir: Path, plugin_id: str) -> str | None:
    registry = load_registry(plugins_dir)
    entry = registry.get(plugin_id)
    if entry and isinstance(entry, dict):
        return entry.get("git_url")
    return None
