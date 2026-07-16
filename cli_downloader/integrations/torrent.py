"""Torrent client integration.

Plugins return validated torrent data (magnet URI or .torrent file data).
The core invokes a configured external client to handle the actual download.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol, runtime_checkable

from cli_downloader.core.progress import normalise_byte_progress, normalise_progress


@dataclass(frozen=True)
class TorrentAsset:
    """Plugin-provided torrent data for one downloadable item."""

    name: str
    magnet_uri: str | None = None
    torrent_url: str | None = None
    torrent_data: bytes | None = None
    info_hash: str | None = None
    index: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "magnet_uri": self.magnet_uri,
            "torrent_url": self.torrent_url,
            "info_hash": self.info_hash,
            "index": self.index,
        }


@dataclass(frozen=True)
class TorrentHandle:
    """Opaque identifier returned by the client after adding a torrent."""

    client_id: str
    name: str
    magnet_uri: str | None = None


@dataclass(frozen=True)
class TorrentOutcome:
    """Result of a torrent download operation."""

    handle: TorrentHandle
    status: str
    output_dir: str
    found: bool | None = None
    client_state: str = "unknown"
    progress: float = 0.0
    completed_bytes: int = 0
    total_bytes: int | None = None
    download_speed: int = 0
    files: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "progress", normalise_progress(self.progress))
        completed_bytes, total_bytes = normalise_byte_progress(
            self.completed_bytes,
            self.total_bytes,
        )
        object.__setattr__(self, "completed_bytes", completed_bytes)
        object.__setattr__(self, "total_bytes", total_bytes)

    def to_dict(self) -> dict[str, object]:
        return {
            "client_id": self.handle.client_id,
            "name": self.handle.name,
            "status": self.status,
            "output_dir": self.output_dir,
            "found": self.found,
            "client_state": self.client_state,
            "progress": self.progress,
            "completed_bytes": self.completed_bytes,
            "total_bytes": self.total_bytes,
            "download_speed": self.download_speed,
            "files": list(self.files),
            "error": self.error,
        }


@dataclass(frozen=True)
class TorrentReconciliation:
    """Decision for a persisted torrent checkpoint after process restart."""

    action: str
    handle: TorrentHandle | None = None
    outcome: TorrentOutcome | None = None


async def reconcile_torrent_checkpoint(
    client: object,
    checkpoint: Mapping[str, object],
) -> TorrentReconciliation:
    """Resolve whether a persisted torrent should be reused or submitted again.

    A checkpoint is only reusable when it contains the opaque client id and
    name returned by ``add_torrent``.  ``found=False`` is deliberately mapped
    to completion for accepted submissions: qBittorrent may have removed a
    finished torrent from its active list, and restarting it would duplicate
    the payload.  ``found=None`` (client/auth/connection failure) remains an
    error so recovery never silently downloads twice.
    """

    client_id = checkpoint.get("client_id")
    name = checkpoint.get("name")
    if not isinstance(client_id, str) or not client_id.strip() or not isinstance(name, str):
        return TorrentReconciliation(action="new")

    handle = TorrentHandle(
        client_id=client_id,
        name=name,
        magnet_uri=(
            checkpoint.get("magnet_uri")
            if isinstance(checkpoint.get("magnet_uri"), str)
            else None
        ),
    )
    get_status = getattr(client, "get_status", None)
    if not callable(get_status):
        return TorrentReconciliation(action="new", handle=handle)

    try:
        outcome = await get_status(handle)
    except Exception as error:
        outcome = TorrentOutcome(
            handle=handle,
            status="error",
            output_dir="",
            found=None,
            error=str(error).strip() or type(error).__name__,
        )
    if outcome.found is None or outcome.status == "error":
        return TorrentReconciliation(action="error", handle=handle, outcome=outcome)
    if outcome.found is False:
        if checkpoint.get("submission") == "accepted":
            completed = TorrentOutcome(
                handle=handle,
                status="completed",
                output_dir=str(checkpoint.get("output_dir", "")),
                found=False,
                client_state="missing",
                progress=1.0,
                completed_bytes=outcome.completed_bytes,
                total_bytes=outcome.total_bytes,
                download_speed=0,
                files=outcome.files,
                error="Torrent no longer listed by client; treated as externally completed",
            )
            return TorrentReconciliation(
                action="externally_completed",
                handle=handle,
                outcome=completed,
            )
        return TorrentReconciliation(action="new", handle=handle, outcome=outcome)
    if outcome.status == "completed":
        return TorrentReconciliation(action="completed", handle=handle, outcome=outcome)
    if outcome.status in {"downloading", "paused"}:
        return TorrentReconciliation(action="resume", handle=handle, outcome=outcome)
    return TorrentReconciliation(action="error", handle=handle, outcome=outcome)


@runtime_checkable
class TorrentClient(Protocol):
    """Protocol for torrent client integrations."""

    async def add_torrent(
        self,
        torrent: TorrentAsset,
        download_dir: Path,
    ) -> TorrentHandle: ...

    async def get_status(self, handle: TorrentHandle) -> TorrentOutcome: ...

    async def pause(self, handle: TorrentHandle) -> None: ...

    async def resume(self, handle: TorrentHandle) -> None: ...

    async def cancel(self, handle: TorrentHandle) -> None: ...


class TorrentClientError(Exception):
    """Raised when torrent client operations fail."""


def poll_supports_argument(poll_method: object, name: str) -> bool:
    """Return whether an optional polling callback is accepted by a client.

    ``poll_until_complete`` is an integration convenience rather than part of
    the minimal TorrentClient protocol.  This keeps older/custom clients that
    only expose the original cancel/status callbacks usable while qBittorrent
    receives the richer Core control callbacks.
    """
    try:
        parameters = inspect.signature(poll_method).parameters
    except (TypeError, ValueError, OverflowError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class DisabledTorrentClient:
    """No-op client used when no torrent client is configured."""

    async def add_torrent(
        self,
        torrent: TorrentAsset,
        download_dir: Path,
    ) -> TorrentHandle:
        raise TorrentClientError("Torrent download is not available: no torrent client configured.")

    async def get_status(self, handle: TorrentHandle) -> TorrentOutcome:
        raise TorrentClientError("Torrent client is not configured.")

    async def pause(self, handle: TorrentHandle) -> None:
        raise TorrentClientError("Torrent client is not configured.")

    async def resume(self, handle: TorrentHandle) -> None:
        raise TorrentClientError("Torrent client is not configured.")

    async def cancel(self, handle: TorrentHandle) -> None:
        raise TorrentClientError("Torrent client is not configured.")


class QbittorrentClient:
    """qBittorrent Web API client.

    Authenticates via cookie session and communicates with the qBittorrent
    Web UI API to add torrents and poll progress.
    """

    def __init__(
        self,
        *,
        url: str = "http://localhost:8080",
        username: str = "admin",
        password: str = "",
        poll_interval_seconds: int = 10,
    ) -> None:
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._poll_interval = poll_interval_seconds
        self._sid: str | None = None

    # ------------------------------------------------------------------
    # TorrentClient protocol
    # ------------------------------------------------------------------

    async def add_torrent(
        self,
        torrent: TorrentAsset,
        download_dir: Path,
    ) -> TorrentHandle:
        """Add a .torrent file to qBittorrent and return a handle."""
        await self._ensure_authenticated()

        if torrent.torrent_data is None:
            raise TorrentClientError("Torrent data is required for qBittorrent add_torrent")

        info_hash = _compute_info_hash(torrent.torrent_data)
        filename = torrent.name or f"{info_hash}.torrent"

        fields = {
            "torrents": (filename, torrent.torrent_data, "application/x-bittorrent"),
            "savepath": (str(download_dir), None, None),
        }
        body = _encode_multipart(fields)
        content_type = f"multipart/form-data; boundary={_multipart_boundary(fields)}"

        try:
            await self._request(
                "POST",
                "/api/v2/torrents/add",
                body=body,
                content_type=content_type,
            )
        except TorrentClientError as exc:
            raise TorrentClientError(f"Failed to add torrent to qBittorrent: {exc}") from exc

        return TorrentHandle(
            client_id=info_hash,
            name=torrent.name,
            magnet_uri=torrent.magnet_uri,
        )

    async def get_status(self, handle: TorrentHandle) -> TorrentOutcome:
        """Poll torrent status from qBittorrent."""
        await self._ensure_authenticated()

        try:
            data = await self._request_json(
                "GET",
                f"/api/v2/torrents/info?hashes={handle.client_id}",
            )
        except TorrentClientError as exc:
            return TorrentOutcome(
                handle=handle,
                status="error",
                output_dir="",
                found=None,
                error=str(exc),
            )

        if not isinstance(data, list) or len(data) == 0:
            return TorrentOutcome(
                handle=handle,
                status="unknown",
                output_dir="",
                found=False,
                error="Torrent not found in qBittorrent",
            )

        info = data[0]
        qb_state = str(info.get("state", "unknown"))
        progress = normalise_progress(info.get("progress"))
        save_path = str(info.get("save_path", ""))

        state = _normalise_state(qb_state, progress)

        files: tuple[str, ...] = ()
        if state == "completed":
            try:
                files_data = await self._request_json(
                    "GET",
                    f"/api/v2/torrents/files?hash={handle.client_id}",
                )
                if isinstance(files_data, list):
                    files = tuple(str(f.get("name", "")) for f in files_data)
            except TorrentClientError:
                pass

        return TorrentOutcome(
            handle=handle,
            status=state,
            output_dir=save_path,
            found=True,
            client_state=qb_state,
            progress=progress,
            completed_bytes=_int_value(
                info.get("completed", info.get("downloaded")),
                default=0,
            ),
            total_bytes=_optional_int_value(info.get("size")),
            download_speed=_int_value(info.get("dlspeed"), default=0),
            files=files,
        )

    async def pause(self, handle: TorrentHandle) -> None:
        """Ask qBittorrent to pause the matching torrent."""
        await self._control("pause", handle)

    async def resume(self, handle: TorrentHandle) -> None:
        """Ask qBittorrent to resume the matching torrent."""
        await self._control("resume", handle)

    async def cancel(self, handle: TorrentHandle) -> None:
        """Stop the matching torrent without deleting its payload files."""
        await self._control("stop", handle)

    # ------------------------------------------------------------------
    # Polling helper
    # ------------------------------------------------------------------

    async def poll_until_complete(
        self,
        handle: TorrentHandle,
        *,
        cancel_requested: callable | None = None,
        shutdown_requested: callable | None = None,
        pause_requested: Callable[[], bool] | None = None,
        pause_waiter: Callable[[], Awaitable[None]] | None = None,
        status_callback: Callable[[TorrentOutcome], None] | None = None,
    ) -> TorrentOutcome:
        """Poll until done while mirroring Core control requests to qBittorrent.

        ``pause_waiter`` is supplied by the owning execution boundary.  It
        blocks while Core is paused and also wakes when cancellation releases a
        paused worker, so the client never keeps downloading behind Core's
        back.  The local flags make repeated control observations idempotent.
        """
        client_paused = False
        while True:
            if shutdown_requested is not None and shutdown_requested():
                # Preserve the client task and return the last observed state;
                # the owning pipeline persists it as a resumable checkpoint.
                return await self.get_status(handle)
            if cancel_requested is not None and cancel_requested():
                await self.cancel(handle)
                return TorrentOutcome(
                    handle=handle,
                    status="cancelled",
                    output_dir="",
                    found=None,
                    error="Cancelled by user",
                )

            requested_pause = pause_requested is not None and pause_requested()
            if requested_pause and not client_paused:
                await self.pause(handle)
                client_paused = True
            elif not requested_pause and client_paused:
                await self.resume(handle)
                client_paused = False

            if client_paused:
                if pause_waiter is not None:
                    await pause_waiter()
                else:
                    await asyncio.sleep(self._poll_interval)
                continue

            outcome = await self.get_status(handle)
            if status_callback is not None:
                status_callback(outcome)
            if outcome.status in {"completed", "error", "cancelled"}:
                return outcome
            if outcome.status == "paused" and not requested_pause:
                # The client may have retained a paused state while Core was
                # offline.  A resumed Core task must explicitly release that
                # client-side pause before polling can make progress again.
                await self.resume(handle)
            await asyncio.sleep(self._poll_interval)

    async def _control(self, action: str, handle: TorrentHandle) -> None:
        await self._ensure_authenticated()
        body = urllib.parse.urlencode({"hashes": handle.client_id}).encode("ascii")
        await self._request(
            "POST",
            f"/api/v2/torrents/{action}",
            body=body,
            content_type="application/x-www-form-urlencoded",
        )

    # ------------------------------------------------------------------
    # Internal: HTTP
    # ------------------------------------------------------------------

    async def _ensure_authenticated(self) -> None:
        if self._sid is not None:
            return
        body = urllib.parse.urlencode(
            {"username": self._username, "password": self._password}
        ).encode("ascii")
        try:
            await self._request(
                "POST",
                "/api/v2/auth/login",
                body=body,
                content_type="application/x-www-form-urlencoded",
                authenticate=False,
            )
        except TorrentClientError as exc:
            raise TorrentClientError(f"qBittorrent authentication failed: {exc}") from exc

        # Extract SID cookie — the request itself returns it via Set-Cookie.
        # _request captures it on the first successful auth call.

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "multipart/form-data",
        authenticate: bool = True,
    ) -> str:
        if authenticate and self._sid is None:
            await self._ensure_authenticated()
        url = f"{self._url}{path}"
        headers: dict[str, str] = {"Content-Type": content_type}
        if self._sid is not None:
            headers["Cookie"] = f"SID={self._sid}"

        def _send() -> str:
            data = body
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    response_body = resp.read().decode("utf-8", errors="replace")

                    # Capture SID from Set-Cookie header
                    set_cookie = resp.headers.get("Set-Cookie", "")
                    if set_cookie:
                        for part in set_cookie.split(";"):
                            part = part.strip()
                            if part.startswith("SID="):
                                self._sid = part[4:]
                                break

                    return response_body
            except urllib.error.HTTPError as exc:
                error_body = ""
                try:
                    error_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise TorrentClientError(
                    f"HTTP {exc.code} {method} {path}: {error_body.strip() or exc.reason}"
                ) from exc
            except urllib.error.URLError as exc:
                raise TorrentClientError(f"Connection error {method} {path}: {exc.reason}") from exc
            except OSError as exc:
                raise TorrentClientError(f"OS error {method} {path}: {exc}") from exc

        return await asyncio.to_thread(_send)

    async def _request_json(
        self,
        method: str,
        path: str,
    ) -> object:
        text = await self._request(method, path, content_type="application/json")
        if not text.strip():
            return None
        return json.loads(text)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

# Terminal states for qBittorrent downloads.
_COMPLETED_STATES = frozenset(
    {
        "uploading",
        "stalledUP",
        "pausedUP",
        "queuedUP",
        "checkingUP",
    }
)
_ERROR_STATES = frozenset({"error", "missingFiles"})


def _normalise_state(qb_state: str, progress: float) -> str:
    if qb_state in _ERROR_STATES:
        return "error"
    if qb_state in _COMPLETED_STATES or progress >= 1.0:
        return "completed"
    if qb_state in _PAUSED_STATES:
        return "paused"
    return "downloading"


_PAUSED_STATES = frozenset({"pausedDL", "pausedUP"})


def _float_value(value: object, *, default: float) -> float:
    return normalise_progress(value, default=default)


def _int_value(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, result)


def _optional_int_value(value: object) -> int | None:
    if value is None:
        return None
    return _int_value(value, default=0)


def _encode_multipart(fields: dict[str, tuple[str, bytes | None, str | None]]) -> bytes:
    """Build a multipart/form-data body for qBittorrent API.

    Each value is (filename, data, mime_type). When data is None the field
    is a plain text form value (filename treated as the text value).
    """
    boundary = _multipart_boundary(fields)
    parts: list[bytes] = []
    for name, (filename_or_text, data, mime_type) in fields.items():
        parts.append(f"--{boundary}\r\n".encode("ascii"))
        if data is None:
            # Plain form field
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
            parts.append(f"{filename_or_text}\r\n".encode("utf-8"))
        else:
            # File upload
            parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename_or_text}"\r\n'.encode(
                    "ascii"
                )
            )
            if mime_type:
                parts.append(f"Content-Type: {mime_type}\r\n".encode("ascii"))
            parts.append(b"\r\n")
            parts.append(data)
            parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    body = b"".join(parts)
    return body


def _multipart_boundary(fields: dict[str, tuple[str, bytes | None, str | None]]) -> str:
    return hashlib.sha256(str(fields).encode()).hexdigest()[:32]


def _compute_info_hash(torrent_data: bytes) -> str:
    """Compute the BTIH (BitTorrent Info Hash) from raw .torrent file data."""
    try:
        info_encoded = _bencode_extract_info(torrent_data)
    except ValueError:
        return hashlib.sha1(torrent_data).hexdigest()
    return hashlib.sha1(info_encoded).hexdigest()


def _bencode_extract_info(data: bytes) -> bytes:
    """Extract the raw bencoded value of the 'info' key from a .torrent file.

    Raises ValueError if the data is not a valid bencoded dictionary or
    the 'info' key is missing.
    """
    idx = 0

    def _peek() -> int:
        if idx >= len(data):
            raise ValueError("Unexpected end of bencoded data")
        return data[idx]

    def _read_byte() -> int:
        nonlocal idx
        b = _peek()
        idx += 1
        return b

    def _read_bytes(n: int) -> bytes:
        nonlocal idx
        if idx + n > len(data):
            raise ValueError("Unexpected end of bencoded data")
        chunk = data[idx : idx + n]
        idx += n
        return chunk

    def _read_int() -> int:
        if _read_byte() != ord("i"):
            raise ValueError("Expected 'i'")
        num_str = b""
        while _peek() != ord("e"):
            num_str += bytes([_read_byte()])
        _read_byte()  # consume 'e'
        return int(num_str)

    def _read_str() -> bytes:
        length_str = b""
        while 48 <= _peek() <= 57:  # digits
            length_str += bytes([_read_byte()])
        if _read_byte() != ord(":"):
            raise ValueError("Expected ':' in string")
        length = int(length_str)
        return _read_bytes(length)

    def _skip_value() -> None:
        c = _peek()
        if c == ord("i"):
            _read_int()
        elif 48 <= c <= 57:  # string
            _read_str()
        elif c == ord("l"):  # list
            _read_byte()
            while _peek() != ord("e"):
                _skip_value()
            _read_byte()
        elif c == ord("d"):  # dict
            _read_byte()
            while _peek() != ord("e"):
                _read_str()  # key
                _skip_value()
            _read_byte()
        else:
            raise ValueError(f"Unexpected bencode token: {chr(c)}")

    if _peek() != ord("d"):
        raise ValueError("Expected dictionary start")
    _read_byte()  # consume 'd'

    info_start: int | None = None
    info_end: int | None = None

    while _peek() != ord("e"):
        key = _read_str()
        if key == b"info":
            info_start = idx
            _skip_value()
            info_end = idx
        else:
            _skip_value()

    if info_start is None or info_end is None:
        raise ValueError("info key not found in torrent dictionary")

    return data[info_start:info_end]
