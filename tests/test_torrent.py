import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase

from cli_downloader.integrations.torrent import (
    DisabledTorrentClient,
    QbittorrentClient,
    TorrentAsset,
    TorrentClientError,
    TorrentHandle,
    TorrentOutcome,
    _bencode_extract_info,
    _compute_info_hash,
    _encode_multipart,
    reconcile_torrent_checkpoint,
)


# ------------------------------------------------------------------
# Minimal bencoded .torrent data for info_hash tests
# ------------------------------------------------------------------


def _make_torrent_data(info_content: bytes = b"test-info-value") -> bytes:
    """Build a minimal bencoded .torrent file."""
    info_len = len(info_content)
    result = b"d8:announce12:http://t.com4:info"
    result += str(info_len).encode() + b":" + info_content
    result += b"e"
    return result


# ------------------------------------------------------------------
# Fake qBittorrent HTTP server
# ------------------------------------------------------------------


class _FakeQbittorrentHandler(BaseHTTPRequestHandler):
    """Minimal handler for qBittorrent Web API endpoints."""

    server_config: dict = {
        "accept_auth": True,
        "accept_torrents": True,
        "torrent_state": "downloading",
        "torrent_progress": 0.5,
        "torrent_downloaded": 512,
        "torrent_completed": 512,
        "torrent_size": 1024,
        "torrent_download_speed": 128,
        "torrent_found": True,
        "torrent_save_path": "/downloads/test",
        "last_add_content_type": "",
        "control_requests": [],
    }

    def log_message(self, format, *args):
        pass  # suppress logs

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/v2/auth/login":
            if self.server_config["accept_auth"]:
                self.send_response(200)
                self.send_header("Set-Cookie", "SID=fake-session-id; Path=/")
                self.end_headers()
                self.wfile.write(b"Ok.")
            else:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")

        elif path == "/api/v2/torrents/add":
            self.server_config["last_add_content_type"] = self.headers.get("Content-Type", "")
            cookie = self.headers.get("Cookie", "")
            if "SID=" not in cookie:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return
            if not self.server_config["accept_torrents"]:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Internal Server Error")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Ok.")

        elif path in {
            "/api/v2/torrents/pause",
            "/api/v2/torrents/resume",
            "/api/v2/torrents/stop",
        }:
            cookie = self.headers.get("Cookie", "")
            if "SID=" not in cookie:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return
            self.server_config["control_requests"].append(
                (path, self._read_body(), self.headers.get("Content-Type", ""))
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Ok.")

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/v2/torrents/info":
            cookie = self.headers.get("Cookie", "")
            if "SID=" not in cookie:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return
            if not self.server_config["torrent_found"]:
                self._send_json([])
                return
            info = {
                "hash": "abc123",
                "name": "Test Torrent",
                "state": self.server_config["torrent_state"],
                "progress": self.server_config["torrent_progress"],
                "downloaded": self.server_config["torrent_downloaded"],
                "completed": self.server_config["torrent_completed"],
                "size": self.server_config["torrent_size"],
                "dlspeed": self.server_config["torrent_download_speed"],
                "save_path": self.server_config["torrent_save_path"],
            }
            self._send_json([info])
        elif path == "/api/v2/torrents/files":
            self._send_json([{"name": "file1.mkv"}, {"name": "file2.mkv"}])
        else:
            self.send_response(404)
            self.end_headers()


def _start_fake_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeQbittorrentHandler)
    server.timeout = 1
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ------------------------------------------------------------------
# Test _encode_multipart
# ------------------------------------------------------------------


class EncodeMultipartTest(TestCase):
    def test_file_field(self):
        body = _encode_multipart(
            {
                "torrents": ("test.torrent", b"torrent content", "application/x-bittorrent"),
            }
        )
        self.assertIn(b"torrents", body)
        self.assertIn(b"test.torrent", body)
        self.assertIn(b"torrent content", body)
        self.assertIn(b"application/x-bittorrent", body)

    def test_plain_field(self):
        body = _encode_multipart(
            {
                "savepath": ("/downloads", None, None),
            }
        )
        self.assertIn(b"savepath", body)
        self.assertIn(b"/downloads", body)

    def test_multiple_fields(self):
        body = _encode_multipart(
            {
                "torrents": ("x.torrent", b"data", "application/x-bittorrent"),
                "savepath": ("/tmp", None, None),
                "category": ("movies", None, None),
            }
        )
        self.assertIn(b"x.torrent", body)
        self.assertIn(b"/tmp", body)
        self.assertIn(b"movies", body)


class _CheckpointClient:
    def __init__(self, outcome: TorrentOutcome) -> None:
        self.outcome = outcome
        self.handles: list[TorrentHandle] = []

    async def get_status(self, handle: TorrentHandle) -> TorrentOutcome:
        self.handles.append(handle)
        return self.outcome


class _FailingCheckpointClient:
    async def get_status(self, handle: TorrentHandle) -> TorrentOutcome:
        raise OSError("client offline")


class TorrentReconciliationTest(TestCase):
    def _checkpoint(self) -> dict[str, object]:
        return {
            "client_id": "abc123",
            "name": "test.torrent",
            "submission": "accepted",
        }

    def test_downloading_checkpoint_reuses_handle(self) -> None:
        handle = TorrentHandle("abc123", "test.torrent")
        client = _CheckpointClient(TorrentOutcome(handle, "downloading", "/downloads", found=True))

        result = asyncio.run(reconcile_torrent_checkpoint(client, self._checkpoint()))

        self.assertEqual(result.action, "resume")
        self.assertEqual(result.handle, handle)
        self.assertEqual(client.handles, [handle])

    def test_accepted_missing_checkpoint_is_external_completion(self) -> None:
        handle = TorrentHandle("abc123", "test.torrent")
        client = _CheckpointClient(
            TorrentOutcome(
                handle,
                "unknown",
                "",
                found=False,
                error="Torrent not found in qBittorrent",
            )
        )

        result = asyncio.run(reconcile_torrent_checkpoint(client, self._checkpoint()))

        self.assertEqual(result.action, "externally_completed")
        self.assertIsNotNone(result.outcome)
        self.assertEqual(result.outcome.status, "completed")
        self.assertFalse(result.outcome.found)

    def test_client_error_is_not_treated_as_completion(self) -> None:
        handle = TorrentHandle("abc123", "test.torrent")
        client = _CheckpointClient(
            TorrentOutcome(handle, "error", "", found=None, error="connection failed")
        )

        result = asyncio.run(reconcile_torrent_checkpoint(client, self._checkpoint()))

        self.assertEqual(result.action, "error")
        self.assertEqual(result.outcome.error, "connection failed")

    def test_status_exception_is_recoverable_error(self) -> None:
        result = asyncio.run(
            reconcile_torrent_checkpoint(_FailingCheckpointClient(), self._checkpoint())
        )

        self.assertEqual(result.action, "error")
        self.assertEqual(result.outcome.found, None)
        self.assertEqual(result.outcome.error, "client offline")


# ------------------------------------------------------------------
# Test _bencode_extract_info
# ------------------------------------------------------------------


class BencodeExtractInfoTest(TestCase):
    def test_extracts_info_value(self):
        data = _make_torrent_data(b"hello-info-dict")
        extracted = _bencode_extract_info(data)
        # The bencoded form of a string is "len:content"
        self.assertEqual(extracted, b"15:hello-info-dict")

    def test_info_hash_matches_expected(self):
        info_inner = b"4:name6:myfile12:piece lengthi256e6:pieces20:" + b"\x00" * 20
        info_dict = b"d" + info_inner + b"e"
        data = b"d8:announce15:http://ann.test4:info" + info_dict + b"e"
        extracted = _bencode_extract_info(data)
        self.assertEqual(extracted, info_dict)
        expected_hash = hashlib.sha1(info_dict).hexdigest()
        self.assertEqual(_compute_info_hash(data), expected_hash)

    def test_compute_info_hash(self):
        data = _make_torrent_data(b"test-info")
        digest = _compute_info_hash(data)
        # SHA1 of the bencoded representation "9:test-info"
        self.assertEqual(digest, hashlib.sha1(b"9:test-info").hexdigest())

    def test_raises_on_non_dict(self):
        with self.assertRaises(ValueError):
            _bencode_extract_info(b"l1:ae")

    def test_raises_on_no_info_key(self):
        data = b"d4:name5:valuee"
        with self.assertRaises(ValueError):
            _bencode_extract_info(data)


# ------------------------------------------------------------------
# Test QbittorrentClient
# ------------------------------------------------------------------


class QbittorrentClientTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _start_fake_server()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _FakeQbittorrentHandler.server_config.update(
            {
                "accept_auth": True,
                "accept_torrents": True,
                "torrent_state": "downloading",
                "torrent_progress": 0.5,
                "torrent_downloaded": 512,
                "torrent_completed": 512,
                "torrent_size": 1024,
                "torrent_download_speed": 128,
                "torrent_found": True,
                "torrent_save_path": "/downloads/test",
                "last_add_content_type": "",
                "control_requests": [],
            }
        )

    def _make_client(self, **kwargs) -> QbittorrentClient:
        return QbittorrentClient(
            url=self.base_url,
            username="admin",
            password="password",
            poll_interval_seconds=1,
            **kwargs,
        )

    def test_add_torrent_success(self):
        async def run():
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"test-info-data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))
            self.assertIsInstance(handle, TorrentHandle)
            self.assertNotEqual(handle.client_id, "")
            self.assertEqual(handle.name, "test.torrent")
            self.assertIn(
                "multipart/form-data; boundary=",
                _FakeQbittorrentHandler.server_config["last_add_content_type"],
            )

        asyncio.run(run())

    def test_add_torrent_no_data_raises(self):
        async def run():
            client = self._make_client()
            torrent = TorrentAsset(name="test.torrent")
            with self.assertRaises(TorrentClientError):
                await client.add_torrent(torrent, Path("/tmp/dl"))

        asyncio.run(run())

    def test_add_torrent_auth_failure(self):
        async def run():
            _FakeQbittorrentHandler.server_config["accept_auth"] = False
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            with self.assertRaises(TorrentClientError):
                await client.add_torrent(torrent, Path("/tmp/dl"))

        asyncio.run(run())

    def test_add_torrent_server_error(self):
        async def run():
            _FakeQbittorrentHandler.server_config["accept_torrents"] = False
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            with self.assertRaises(TorrentClientError):
                await client.add_torrent(torrent, Path("/tmp/dl"))

        asyncio.run(run())

    def test_get_status_downloading(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_state"] = "downloading"
            _FakeQbittorrentHandler.server_config["torrent_progress"] = 0.5
            client = self._make_client()
            # Must authenticate first
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            outcome = await client.get_status(handle)
            self.assertEqual(outcome.status, "downloading")
            self.assertEqual(outcome.output_dir, "/downloads/test")
            self.assertTrue(outcome.found)
            self.assertEqual(outcome.progress, 0.5)
            self.assertEqual(outcome.completed_bytes, 512)
            self.assertEqual(outcome.total_bytes, 1024)
            self.assertEqual(outcome.download_speed, 128)
            self.assertEqual(outcome.client_state, "downloading")

        asyncio.run(run())

    def test_get_status_prefers_completed_payload_over_cumulative_downloaded(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_downloaded"] = 4096
            _FakeQbittorrentHandler.server_config["torrent_completed"] = 900
            _FakeQbittorrentHandler.server_config["torrent_size"] = 1024
            client = self._make_client()
            handle = TorrentHandle(client_id="abc123", name="test.torrent")

            outcome = await client.get_status(handle)

            self.assertEqual(outcome.completed_bytes, 900)
            self.assertEqual(outcome.total_bytes, 1024)
            self.assertEqual(outcome.progress, 0.5)

        asyncio.run(run())

    def test_get_status_clamps_completed_payload_to_size(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_downloaded"] = 4096
            _FakeQbittorrentHandler.server_config["torrent_completed"] = 4096
            _FakeQbittorrentHandler.server_config["torrent_size"] = 1024
            client = self._make_client()
            handle = TorrentHandle(client_id="abc123", name="test.torrent")

            outcome = await client.get_status(handle)

            self.assertEqual(outcome.completed_bytes, 1024)
            self.assertEqual(outcome.total_bytes, 1024)

        asyncio.run(run())

    def test_get_status_completed(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_state"] = "uploading"
            _FakeQbittorrentHandler.server_config["torrent_progress"] = 1.0
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            outcome = await client.get_status(handle)
            self.assertEqual(outcome.status, "completed")
            self.assertTrue(outcome.found)
            self.assertEqual(outcome.files, ("file1.mkv", "file2.mkv"))

        asyncio.run(run())

    def test_get_status_completed_by_progress(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_state"] = "pausedUP"
            _FakeQbittorrentHandler.server_config["torrent_progress"] = 1.0
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            outcome = await client.get_status(handle)
            self.assertEqual(outcome.status, "completed")

        asyncio.run(run())

    def test_get_status_error(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_state"] = "error"
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            outcome = await client.get_status(handle)
            self.assertEqual(outcome.status, "error")

        asyncio.run(run())

    def test_get_status_not_found(self):
        async def run():
            client = self._make_client()
            _FakeQbittorrentHandler.server_config["torrent_found"] = False

            handle = TorrentHandle(client_id="nonexistent", name="ghost")

            outcome = await client.get_status(handle)
            self.assertEqual(outcome.status, "unknown")
            self.assertFalse(outcome.found)
            self.assertIn("not found", outcome.error or "")

        asyncio.run(run())

    def test_get_status_paused_download(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_state"] = "pausedDL"
            _FakeQbittorrentHandler.server_config["torrent_progress"] = 0.5
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            outcome = await client.get_status(handle)

            self.assertEqual(outcome.status, "paused")
            self.assertEqual(outcome.client_state, "pausedDL")
            self.assertEqual(outcome.progress, 0.5)

        asyncio.run(run())

    def test_control_commands_send_hash_to_qbittorrent(self):
        async def run():
            client = self._make_client()
            handle = TorrentHandle(client_id="abc123", name="test")

            await client.pause(handle)
            await client.resume(handle)
            await client.cancel(handle)

            requests = _FakeQbittorrentHandler.server_config["control_requests"]
            self.assertEqual(
                [path for path, _body, _content_type in requests],
                [
                    "/api/v2/torrents/pause",
                    "/api/v2/torrents/resume",
                    "/api/v2/torrents/stop",
                ],
            )
            self.assertTrue(all(b"hashes=abc123" in body for _path, body, _type in requests))
            self.assertTrue(
                all(
                    content_type == "application/x-www-form-urlencoded"
                    for _path, _body, content_type in requests
                )
            )

        asyncio.run(run())

    def test_poll_until_complete(self):
        async def run():
            _FakeQbittorrentHandler.server_config["torrent_state"] = "uploading"
            _FakeQbittorrentHandler.server_config["torrent_progress"] = 1.0
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            statuses = []
            outcome = await client.poll_until_complete(handle, status_callback=statuses.append)
            self.assertEqual(outcome.status, "completed")
            self.assertEqual([status.status for status in statuses], ["completed"])

        asyncio.run(run())

    def test_poll_until_complete_eventually_completes(self):
        call_count = [0]

        async def run():
            client = self._make_client()

            # Override get_status to simulate transition

            async def progressing_status(h):
                call_count[0] += 1
                if call_count[0] < 3:
                    return TorrentOutcome(handle=h, status="downloading", output_dir="/tmp")
                return TorrentOutcome(
                    handle=h, status="completed", output_dir="/tmp", files=("a.mkv",)
                )

            client.get_status = progressing_status

            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            outcome = await client.poll_until_complete(handle)
            self.assertEqual(outcome.status, "completed")
            self.assertGreaterEqual(call_count[0], 3)

        asyncio.run(run())

    def test_poll_cancelled(self):
        async def run():
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))

            outcome = await client.poll_until_complete(
                handle,
                cancel_requested=lambda: True,
            )
            self.assertEqual(outcome.status, "cancelled")
            self.assertEqual(
                [
                    path
                    for path, _body, _type in _FakeQbittorrentHandler.server_config[
                        "control_requests"
                    ]
                ],
                ["/api/v2/torrents/stop"],
            )

        asyncio.run(run())

    def test_poll_mirrors_pause_and_resume_requests(self):
        async def run():
            client = self._make_client()
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))
            paused = True

            async def release_pause():
                nonlocal paused
                paused = False
                _FakeQbittorrentHandler.server_config["torrent_state"] = "uploading"
                _FakeQbittorrentHandler.server_config["torrent_progress"] = 1.0

            outcome = await client.poll_until_complete(
                handle,
                pause_requested=lambda: paused,
                pause_waiter=release_pause,
            )
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(
                [
                    path
                    for path, _body, _type in _FakeQbittorrentHandler.server_config[
                        "control_requests"
                    ]
                ],
                ["/api/v2/torrents/pause", "/api/v2/torrents/resume"],
            )

        asyncio.run(run())

    def test_poll_releases_client_pause_after_core_resume(self):
        async def run():
            client = self._make_client()
            client._poll_interval = 0
            handle = TorrentHandle(client_id="abc123", name="test")
            statuses = iter(
                (
                    TorrentOutcome(
                        handle,
                        "paused",
                        "/downloads",
                        found=True,
                        client_state="pausedDL",
                    ),
                    TorrentOutcome(
                        handle,
                        "completed",
                        "/downloads",
                        found=True,
                        progress=1.0,
                    ),
                )
            )
            resume_calls = []

            async def get_status(_handle):
                return next(statuses)

            async def resume(_handle):
                resume_calls.append(_handle.client_id)

            client.get_status = get_status
            client.resume = resume

            outcome = await client.poll_until_complete(handle)

            self.assertEqual(outcome.status, "completed")
            self.assertEqual(resume_calls, ["abc123"])

        asyncio.run(run())

    def test_poll_cancel_while_paused_stops_client(self):
        async def run():
            client = self._make_client()
            client._poll_interval = 0
            handle = TorrentHandle(client_id="abc123", name="test")
            paused = True
            cancel = False
            controls = []

            async def pause(_handle):
                controls.append("pause")

            async def cancel_client(_handle):
                controls.append("cancel")

            async def wait_if_paused():
                nonlocal paused, cancel
                paused = False
                cancel = True

            client.pause = pause
            client.cancel = cancel_client

            outcome = await client.poll_until_complete(
                handle,
                cancel_requested=lambda: cancel,
                pause_requested=lambda: paused,
                pause_waiter=wait_if_paused,
            )

            self.assertEqual(outcome.status, "cancelled")
            self.assertEqual(controls, ["pause", "cancel"])

        asyncio.run(run())

    def test_connection_refused(self):
        async def run():
            client = QbittorrentClient(
                url="http://127.0.0.1:1",  # unlikely to be in use
                username="admin",
                password="pw",
                poll_interval_seconds=1,
            )
            torrent = TorrentAsset(
                name="test.torrent",
                torrent_data=_make_torrent_data(b"data"),
            )
            with self.assertRaises(TorrentClientError):
                await client.add_torrent(torrent, Path("/tmp/dl"))

        asyncio.run(run())

    def test_password_in_secrets(self):
        """Verify password is read from secrets, not config."""

        async def run():
            client = QbittorrentClient(
                url=self.base_url,
                username="admin",
                password="secret-password",
                poll_interval_seconds=1,
            )
            # Verify the password is set correctly
            self.assertEqual(client._password, "secret-password")

        asyncio.run(run())

    def test_magnet_uri_preserved(self):
        async def run():
            client = self._make_client()
            torrent = TorrentAsset(
                name="magnet.torrent",
                magnet_uri="magnet:?xt=urn:btih:abc",
                torrent_data=_make_torrent_data(b"data"),
            )
            handle = await client.add_torrent(torrent, Path("/tmp/dl"))
            self.assertEqual(handle.magnet_uri, "magnet:?xt=urn:btih:abc")

        asyncio.run(run())


# ------------------------------------------------------------------
# DisabledTorrentClient — keep existing tests updated
# ------------------------------------------------------------------


class DisabledTorrentClientTest(TestCase):
    def test_add_torrent_raises(self):
        async def run():
            client = DisabledTorrentClient()
            with self.assertRaises(TorrentClientError):
                await client.add_torrent(
                    TorrentAsset(name="x", magnet_uri="magnet:x"),
                    Path("/tmp"),
                )

        asyncio.run(run())

    def test_get_status_raises(self):
        async def run():
            client = DisabledTorrentClient()
            with self.assertRaises(TorrentClientError):
                await client.get_status(TorrentHandle("id", "x"))

        asyncio.run(run())

    def test_control_commands_raise(self):
        async def run():
            client = DisabledTorrentClient()
            handle = TorrentHandle("id", "x")
            for operation in (client.pause, client.resume, client.cancel):
                with self.assertRaises(TorrentClientError):
                    await operation(handle)

        asyncio.run(run())


# ------------------------------------------------------------------
# Existing TorrentAsset tests
# ------------------------------------------------------------------


class TorrentAssetTest(TestCase):
    def test_construction_with_magnet(self):
        asset = TorrentAsset(
            name="test.torrent",
            magnet_uri="magnet:?xt=urn:btih:abc123",
            info_hash="abc123",
            index=3,
        )
        self.assertEqual(asset.name, "test.torrent")
        self.assertEqual(asset.magnet_uri, "magnet:?xt=urn:btih:abc123")
        self.assertEqual(asset.info_hash, "abc123")
        self.assertEqual(asset.index, 3)
        self.assertIsNone(asset.torrent_url)

    def test_construction_with_url(self):
        asset = TorrentAsset(
            name="download.torrent",
            torrent_url="https://example.test/file.torrent",
            index=1,
        )
        self.assertEqual(asset.torrent_url, "https://example.test/file.torrent")
        self.assertIsNone(asset.magnet_uri)

    def test_to_dict(self):
        asset = TorrentAsset(name="x", magnet_uri="magnet:x", info_hash="h", index=2)
        d = asset.to_dict()
        self.assertEqual(d["name"], "x")
        self.assertEqual(d["magnet_uri"], "magnet:x")
        self.assertEqual(d["info_hash"], "h")
        self.assertEqual(d["index"], 2)


class TorrentHandleTest(TestCase):
    def test_construction(self):
        handle = TorrentHandle(client_id="id123", name="test", magnet_uri="magnet:x")
        self.assertEqual(handle.client_id, "id123")
        self.assertEqual(handle.name, "test")


class TorrentOutcomeTest(TestCase):
    def test_success(self):
        handle = TorrentHandle(client_id="id", name="test")
        outcome = TorrentOutcome(
            handle=handle,
            status="completed",
            output_dir="/tmp/dl",
            files=("a.mkv", "b.mkv"),
        )
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.files, ("a.mkv", "b.mkv"))
        self.assertIsNone(outcome.found)
        self.assertEqual(outcome.progress, 0.0)

    def test_error(self):
        handle = TorrentHandle(client_id="id", name="test")
        outcome = TorrentOutcome(
            handle=handle,
            status="failed",
            output_dir="/tmp",
            error="Connection refused",
        )
        self.assertEqual(outcome.error, "Connection refused")

    def test_to_dict(self):
        handle = TorrentHandle(client_id="id", name="test")
        outcome = TorrentOutcome(handle=handle, status="done", output_dir="/tmp")
        d = outcome.to_dict()
        self.assertEqual(d["status"], "done")
        self.assertIsNone(d["found"])
        self.assertEqual(d["files"], [])
