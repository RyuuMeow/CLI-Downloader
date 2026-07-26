from __future__ import annotations

import ctypes
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePath, PureWindowsPath
import os
import re
import asyncio
from typing import Mapping, AsyncIterable

from cli_downloader.core.errors import ConfigValidationError
from cli_downloader.core.models import Asset
from cli_downloader.core.output import COMMON_OUTPUT_FIELDS, validate_folder_template

_DEFAULT_FOLDER_TEMPLATE = "{task_id}"
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_INVALID_COMPONENT_CHARS = '<>:"/\\|?*'


@dataclass(frozen=True)
class WrittenAsset:
    asset_url: str
    path: Path
    bytes_written: int


class AssetFileWriter:
    def __init__(
        self,
        output_root: Path,
        *,
        folder_template: str = _DEFAULT_FOLDER_TEMPLATE,
    ) -> None:
        self._output_root = output_root
        self._folder_template = folder_template
        self._task_folders: dict[str, Path] = {}
        self._ownership_root = output_root / ".cli-downloader" / "owners"

    def write(
        self,
        asset: Asset,
        content: bytes,
        *,
        task_id: str,
        folder_fields: Mapping[str, object] | None = None,
        folder_template: str | None = None,
    ) -> WrittenAsset:
        target, partial = self.paths(
            asset,
            task_id=task_id,
            folder_fields=folder_fields,
            folder_template=folder_template,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(content)
        partial.replace(target)

        return WrittenAsset(
            asset_url=asset.url,
            path=target,
            bytes_written=len(content),
        )

    async def write_stream(
        self,
        asset: Asset,
        chunks: AsyncIterable[bytes],
        *,
        task_id: str,
        folder_fields: Mapping[str, object] | None = None,
        folder_template: str | None = None,
    ) -> WrittenAsset:
        target, partial = self.paths(
            asset,
            task_id=task_id,
            folder_fields=folder_fields,
            folder_template=folder_template,
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()

        def _open_partial():
            return partial.open("wb")

        stream = await loop.run_in_executor(None, _open_partial)
        bytes_written = 0
        try:
            async for chunk in chunks:
                await loop.run_in_executor(None, stream.write, chunk)
                bytes_written += len(chunk)
        finally:
            await loop.run_in_executor(None, stream.close)

        await loop.run_in_executor(None, partial.replace, target)

        return WrittenAsset(
            asset_url=asset.url,
            path=target,
            bytes_written=bytes_written,
        )

    def paths(
        self,
        asset: Asset,
        *,
        task_id: str,
        folder_fields: Mapping[str, object] | None = None,
        folder_template: str | None = None,
    ) -> tuple[Path, Path]:
        filename = _safe_filename(asset.filename or PurePath(asset.url).name or "asset.bin")
        folder = self._task_folders.get(task_id)
        if folder is None:
            folder = _resolve_output_folder(
                self._output_root,
                folder_template or self._folder_template,
                {"task_id": task_id, **(folder_fields or {})},
            )
            self._task_folders[task_id] = folder
        target = _select_target(
            folder / filename,
            owner_id=_asset_owner_id(task_id, asset),
            ownership_root=self._ownership_root,
        )
        return target, target.with_name(f"{target.name}.part")

    def resume(
        self,
        asset: Asset,
        *,
        task_id: str,
        partial_path: str,
        content_sha256: str,
        folder_fields: Mapping[str, object] | None = None,
        folder_template: str | None = None,
    ) -> WrittenAsset | None:
        target, expected_partial = self.paths(
            asset,
            task_id=task_id,
            folder_fields=folder_fields,
            folder_template=folder_template,
        )
        supplied = Path(partial_path)
        if supplied.resolve() != expected_partial.resolve() or not supplied.is_file():
            return None
        content = supplied.read_bytes()
        if sha256(content).hexdigest() != content_sha256:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        supplied.replace(target)
        return WrittenAsset(asset_url=asset.url, path=target, bytes_written=len(content))


def _safe_filename(filename: str) -> str:
    name = _safe_component(PurePath(filename).name)
    if name in {"", ".", ".."}:
        return "asset.bin"
    return name


def _resolve_output_folder(
    output_root: Path,
    template: str,
    fields: Mapping[str, object],
) -> Path:
    extra_fields = tuple(name for name in fields if name not in COMMON_OUTPUT_FIELDS)
    validate_folder_template(template, extra_fields)
    safe_fields = {
        name: _safe_component(str(fields.get(name) or "unknown"))
        for name in COMMON_OUTPUT_FIELDS | set(extra_fields)
    }
    try:
        rendered = template.format_map(safe_fields)
    except (KeyError, ValueError):  # validated above
        raise AssertionError("validated folder template failed to render") from None
    parts = PureWindowsPath(rendered.replace("/", "\\")).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ConfigValidationError("download.folder_template cannot escape the download root")
    relative = Path(*(_safe_component(part) for part in parts))
    return output_root / relative


def _asset_owner_id(task_id: str, asset: Asset) -> str:
    identity = next(
        (
            str(asset.metadata[key])
            for key in ("asset_id", "plugin_asset_key", "image_id", "page_id", "index")
            if isinstance(asset.metadata.get(key), (str, int))
            and not isinstance(asset.metadata.get(key), bool)
        ),
        asset.url,
    )
    return sha256(f"{task_id}\0{identity}".encode()).hexdigest()


def _select_target(target: Path, *, owner_id: str, ownership_root: Path) -> Path:
    if _claim_target(target, owner_id=owner_id, ownership_root=ownership_root):
        return target
    suffix = owner_id[:8]
    candidate = target.with_name(f"{target.stem}-{suffix}{target.suffix}")
    counter = 2
    while not _claim_target(candidate, owner_id=owner_id, ownership_root=ownership_root):
        candidate = target.with_name(f"{target.stem}-{suffix}-{counter}{target.suffix}")
        counter += 1
    return candidate


def _claim_target(target: Path, *, owner_id: str, ownership_root: Path) -> bool:
    marker_name = sha256(str(target.absolute()).casefold().encode()).hexdigest()
    marker = ownership_root / marker_name
    if target.exists() and not marker.exists():
        return False
    ownership_root.mkdir(parents=True, exist_ok=True)
    _hide_metadata_directory(ownership_root.parent)
    try:
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(owner_id)
        return True
    except FileExistsError:
        return marker.read_text(encoding="utf-8") == owner_id


def _hide_metadata_directory(path: Path) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.windll.kernel32
    attributes = kernel32.GetFileAttributesW(str(path))
    if attributes != -1:
        kernel32.SetFileAttributesW(str(path), attributes | 0x2)


def _safe_component(value: str) -> str:
    sanitized = "".join(
        "_" if character in _INVALID_COMPONENT_CHARS else character for character in value
    )
    sanitized = "".join(character for character in sanitized if ord(character) >= 32)
    sanitized = re.sub(r"_+", "_", sanitized)
    sanitized = sanitized.strip().rstrip(". ")
    if not sanitized:
        return "unknown"
    stem = sanitized.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        sanitized = f"_{sanitized}"
    if len(sanitized) > 120:
        digest = sha256(sanitized.encode("utf-8")).hexdigest()[:8]
        sanitized = f"{sanitized[:111]}-{digest}"
    return sanitized
