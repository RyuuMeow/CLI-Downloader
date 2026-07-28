import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlparse


from cli_downloader.plugins.metadata import (
    metadata_compatibility_error,
    plugin_metadata_from_dict,
    plugin_metadata_from_json_file,
)
from cli_downloader.plugins.registry import (
    register_plugin,
    unregister_plugin,
)


def _read_active_marker(plugin_dir: Path) -> str | None:
    marker = plugin_dir / "active"
    if marker.is_file():
        version = marker.read_text(encoding="utf-8").strip()
        if (plugin_dir / version).is_dir():
            return version
    return None


def _write_active_marker(plugin_dir: Path, version: str) -> None:
    marker = plugin_dir / "active"
    marker.write_text(version, encoding="utf-8")


def _installed_versions(plugin_dir: Path) -> list[str]:
    if not plugin_dir.is_dir():
        return []
    return sorted(
        (child.name for child in plugin_dir.iterdir() if child.is_dir()),
        key=_version_key,
    )


def _version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.findall(r"\d+|[^\d]+", version)
    )


def _plugin_version_from_folder(plugin_dir: Path) -> str | None:
    meta_path = plugin_dir / "plugin.json"
    if not meta_path.exists():
        return None
    try:
        return plugin_metadata_from_json_file(meta_path).version
    except Exception:
        return None


_GITHUB_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")


def _github_raw_url(git_url: str) -> str | None:
    m = _GITHUB_URL_RE.match(git_url)
    if m is None:
        return None
    return f"https://raw.githubusercontent.com/{m[1]}/{m[2]}/HEAD/plugin.json"


def fetch_remote_plugin_metadata(git_url: str) -> dict[str, object]:
    """Fetch plugin metadata from a remote GitHub repo via raw HTTP.

    Falls back to local file read for ``file://`` URLs (used in tests).
    """
    name = git_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]

    # --- file:// URL (local git repo, used by tests) ---
    if git_url.startswith("file://"):
        try:
            local_path = Path(urlparse(git_url).path)
            # On Windows, file:///C:/...  is parsed as /C:/... by urllib.
            # Convert to a proper platform path by stripping the leading slash.
            if not local_path.exists():
                local_path = Path(local_path.as_posix().lstrip("/"))
            meta_path = local_path / "plugin.json"
            if meta_path.exists():
                metadata = plugin_metadata_from_json_file(meta_path)
                return {
                    "id": metadata.plugin_id,
                    "name": metadata.display_name,
                    "version": metadata.version,
                    "description": metadata.description or "No description provided.",
                    "requires_core_api": metadata.requires_core_api,
                    "compatibility_error": metadata_compatibility_error(metadata),
                    "git_url": git_url,
                }
        except Exception:
            pass
        return _metadata_fallback(name, git_url)

    # --- GitHub URL (official plugins) ---
    raw_url = _github_raw_url(git_url)
    if raw_url is None:
        return _metadata_fallback(name, git_url)

    try:
        req = urllib.request.Request(raw_url, headers={"User-Agent": "cli-downloader"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return _metadata_fallback(name, git_url)

    try:
        metadata = plugin_metadata_from_dict(payload, source=raw_url)
    except Exception:
        return _metadata_fallback(name, git_url)
    return {
        "id": metadata.plugin_id,
        "name": metadata.display_name,
        "version": metadata.version,
        "description": metadata.description or "No description provided.",
        "requires_core_api": metadata.requires_core_api,
        "compatibility_error": metadata_compatibility_error(metadata),
        "git_url": git_url,
    }


def _metadata_fallback(name: str, git_url: str) -> dict[str, object]:
    return {
        "id": name.lower(),
        "name": name,
        "version": "-",
        "description": "Failed to fetch remote metadata.",
        "fetch_error": True,
        "git_url": git_url,
    }


def _install_plugin_dependencies(plugin_dir: Path) -> None:
    """Install runtime requirements into a staging directory inside one plugin version."""
    deps_dir = plugin_dir / "deps"
    if deps_dir.is_dir() and any(deps_dir.iterdir()):
        # Pre-packaged vendor dependencies exist (e.g. built by GitHub Actions into repo or release bundle).
        # Skip running pip install on user machine.
        return

    requirements = plugin_dir / "requirements.txt"
    if not requirements.is_file():
        return

    staging: Path | None = Path(tempfile.mkdtemp(prefix=".deps-", dir=str(plugin_dir)))
    try:
        try:
            pip_command = [sys.executable]
            if getattr(sys, "frozen", False):
                pip_command = [
                    str(Path(sys.executable).with_name("CLI-Downloader.PluginInstaller.exe"))
                ]
            else:
                pip_command.extend(("-m", "pip"))
            subprocess.run(
                [
                    *pip_command,
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--target",
                    str(staging),
                    "-r",
                    str(requirements),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "dependency installation failed").strip()
            raise RuntimeError(f"Plugin dependency installation failed: {detail}") from exc
        except OSError as exc:
            raise RuntimeError(f"Cannot start pip for plugin dependencies: {exc}") from exc

        assert staging is not None
        deps_dir = plugin_dir / "deps"
        if deps_dir.exists():
            shutil.rmtree(deps_dir)
        staging.rename(deps_dir)
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _extract_zip_archive(zip_path: Path, target_dir: Path) -> None:
    """Extract a .zip file and unwrap single top-level directory if present."""
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)

    entries = [
        child for child in target_dir.iterdir() if child.name not in {".DS_Store", "__MACOSX"}
    ]
    if len(entries) == 1 and entries[0].is_dir() and not (target_dir / "plugin.json").exists():
        single_dir = entries[0]
        for child in single_dir.iterdir():
            shutil.move(str(child), str(target_dir / child.name))
        shutil.rmtree(single_dir, ignore_errors=True)


def _download_and_extract_zip(zip_url: str, target_dir: Path) -> None:
    """Download a .zip file via HTTP and extract it into target_dir."""
    req = urllib.request.Request(zip_url, headers={"User-Agent": "cli-downloader"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read()

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
            tmp_file.write(content)
            tmp_path = Path(tmp_file.name)
        _extract_zip_archive(tmp_path, target_dir)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _find_github_latest_release_zip_url(git_url: str) -> str | None:
    """Query GitHub API for latest release assets and return the first .zip download URL."""
    m = _GITHUB_URL_RE.match(git_url)
    if m is None:
        return None
    owner, repo = m[1], m[2]
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    try:
        req = urllib.request.Request(
            api_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "cli-downloader",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if (
            isinstance(payload, dict)
            and "assets" in payload
            and isinstance(payload["assets"], list)
        ):
            for asset in payload["assets"]:
                if isinstance(asset, dict):
                    name = asset.get("name", "")
                    download_url = asset.get("browser_download_url")
                    if (
                        isinstance(name, str)
                        and name.lower().endswith(".zip")
                        and isinstance(download_url, str)
                    ):
                        return download_url
    except Exception:
        pass
    return None


def _prepare_plugin_source(git_url: str, target_dir: Path) -> None:
    """Materialize a plugin checkout or release archive.

    Supports:
    - file:// URLs (directory or .zip archive)
    - Direct .zip URLs or /releases/download/ URLs
    - GitHub repository URLs: queries GitHub API for latest release assets,
      downloads the first .zip asset if present, otherwise falls back to git clone.
    """
    if git_url.startswith("file://"):
        local_path = Path(urlparse(git_url).path)
        # On Windows, file:///C:/... is parsed as /C:/... by urllib.
        if not local_path.exists():
            local_path = Path(local_path.as_posix().lstrip("/"))
        if local_path.is_dir():
            shutil.copytree(local_path, target_dir, dirs_exist_ok=True)
            return
        if local_path.is_file() and local_path.name.lower().endswith(".zip"):
            _extract_zip_archive(local_path, target_dir)
            return

    if git_url.lower().endswith(".zip") or "/releases/download/" in git_url:
        _download_and_extract_zip(git_url, target_dir)
        return

    zip_url = _find_github_latest_release_zip_url(git_url)
    if zip_url is not None:
        try:
            _download_and_extract_zip(zip_url, target_dir)
            return
        except Exception:
            pass

    subprocess.run(
        ["git", "clone", git_url, str(target_dir)],
        capture_output=True,
        text=True,
        check=True,
    )


def install_plugin(
    plugins_dir: Path,
    git_url: str,
    *,
    source: str = "custom",
    catalog_id: str | None = None,
) -> str:
    """Clone plugin into a versioned directory and set it as active.

    Returns the installed version string.
    """
    name = git_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]

    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp())
        _prepare_plugin_source(git_url, temp_dir)
        meta_path = temp_dir / "plugin.json"
        if not meta_path.exists():
            raise RuntimeError("Plugin does not contain plugin.json")
        meta = plugin_metadata_from_json_file(meta_path)
        compatibility_error = metadata_compatibility_error(meta)
        if compatibility_error is not None:
            raise RuntimeError(f"Plugin is incompatible: {compatibility_error}")
        version = meta.version
        plugin_id = meta.plugin_id

        target_base = plugins_dir / plugin_id
        version_dir = target_base / version
        if version_dir.exists():
            raise RuntimeError(f"Version {version} of plugin {plugin_id} is already installed.")

        _install_plugin_dependencies(temp_dir)
        target_base.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(version_dir)

        _write_active_marker(target_base, version)
        register_plugin(
            plugins_dir,
            plugin_id,
            git_url,
            source=source,
            catalog_id=catalog_id,
        )
        return version
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git clone failed: {e.stderr}")
    finally:
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def update_plugin(plugins_dir: Path, plugin_id: str) -> str | None:
    """Clone the latest version and atomically switch the active marker.

    Returns the new version string, or None if already up to date.
    """
    from cli_downloader.plugins.registry import get_plugin_git_url

    git_url = get_plugin_git_url(plugins_dir, plugin_id)
    if git_url is None:
        raise RuntimeError(f"No git URL registered for plugin {plugin_id}")

    current_version = _read_active_marker(plugins_dir / plugin_id)

    temp_dir = None
    try:
        temp_dir = Path(tempfile.mkdtemp())
        _prepare_plugin_source(git_url, temp_dir)
        meta_path = temp_dir / "plugin.json"
        if not meta_path.exists():
            raise RuntimeError("Plugin does not contain plugin.json")
        meta = plugin_metadata_from_json_file(meta_path)
        compatibility_error = metadata_compatibility_error(meta)
        if compatibility_error is not None:
            raise RuntimeError(f"Plugin is incompatible: {compatibility_error}")
        new_version = meta.version

        if current_version == new_version:
            return None

        target_base = plugins_dir / plugin_id
        version_dir = target_base / new_version
        if version_dir.exists():
            if _read_active_marker(target_base) == new_version:
                return None
            return None

        _install_plugin_dependencies(temp_dir)
        target_base.mkdir(parents=True, exist_ok=True)
        temp_dir.rename(version_dir)
        _write_active_marker(target_base, new_version)
        return new_version
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Git clone failed: {e.stderr}")
    finally:
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def check_plugin_update(plugins_dir: Path, plugin_id: str) -> bool:
    """Check if a newer version is available by inspecting the git remote."""
    from cli_downloader.plugins.registry import get_plugin_git_url

    git_url = get_plugin_git_url(plugins_dir, plugin_id)
    if git_url is None:
        return False

    active_version = _read_active_marker(plugins_dir / plugin_id)
    remote = fetch_remote_plugin_metadata(git_url)
    if remote.get("version", "-") == "-":
        return False
    if active_version is not None:
        try:
            return _version_key(remote["version"]) > _version_key(active_version)  # type: ignore[index]
        except (TypeError, ValueError):
            return False
    return True


def remove_plugin(plugins_dir: Path, plugin_id: str) -> None:
    target_dir = plugins_dir / plugin_id
    if not target_dir.exists():
        raise RuntimeError(f"Plugin directory {plugin_id} does not exist.")

    def on_rm_error(func, path, exc_info):
        import stat

        os.chmod(path, stat.S_IWRITE)
        os.unlink(path)

    shutil.rmtree(target_dir, onerror=on_rm_error)
    unregister_plugin(plugins_dir, plugin_id)


def rollback_plugin(plugins_dir: Path, plugin_id: str) -> str | None:
    """Switch the active version to the previous installed version.

    Returns the new active version, or None if there is only one version.
    """
    target_base = plugins_dir / plugin_id
    if not target_base.is_dir():
        raise RuntimeError(f"Plugin {plugin_id} is not installed.")

    versions = _installed_versions(target_base)
    if len(versions) < 2:
        return None

    current = _read_active_marker(target_base)
    if current is None:
        return None

    try:
        idx = versions.index(current)
    except ValueError:
        return None

    prev = versions[idx - 1]
    _write_active_marker(target_base, prev)
    return prev


def list_plugin_versions(plugins_dir: Path, plugin_id: str) -> list[dict[str, str]]:
    """List all installed versions of a plugin with their status."""
    target_base = plugins_dir / plugin_id
    if not target_base.is_dir():
        return []

    active = _read_active_marker(target_base)
    result = []
    for version in _installed_versions(target_base):
        metadata = {}
        version_dir = target_base / version
        meta_path = version_dir / "plugin.json"
        if meta_path.exists():
            try:
                meta = plugin_metadata_from_json_file(meta_path)
                metadata = {
                    "name": meta.display_name,
                    "description": meta.description or "",
                }
            except Exception:
                metadata = {"name": version, "description": ""}
        else:
            metadata = {"name": version, "description": ""}

        result.append(
            {
                "version": version,
                "active": version == active,
                **metadata,
            }
        )
    return result
