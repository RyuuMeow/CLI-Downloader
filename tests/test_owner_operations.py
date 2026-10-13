import asyncio
import json
import multiprocessing
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic, sleep
from unittest import TestCase

from cli_downloader.application.download_limiter import GalleryDownloadLimiter, RequestPolicy
from cli_downloader.cli._owner_operations import handle_operation
from cli_downloader.core.http import HttpRequest
from cli_downloader.core.operations import ListPageData, OperationResult, PageInfo, ResultKind
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerHandshake,
    OwnerLifecycleState,
    OwnerMode,
)
from cli_downloader.core.owner_scope import OwnerDiscoveryMetadata, OwnerManager, OwnerScope
from cli_downloader.core.owner_transport import OwnerPipeServer
from cli_downloader.integrations.http_client import ManagedHttpClient
from tests.owner_transport_worker import operation_cli_process


class OwnerOperationsTest(TestCase):
    def run_clients(self, capabilities):
        with TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            metadata = OwnerDiscoveryMetadata.for_current_process(
                scope,
                application_version="0.1.4",
                owner_mode=OwnerMode.SHELL,
                lifecycle_state=OwnerLifecycleState.READY,
                session_id="owner",
                capabilities=capabilities,
            )
            limiter = GalleryDownloadLimiter(2, default_request_policy=RequestPolicy(1, 0.025))
            starts, active = [], [0]
            lock = Lock()

            class Response(BytesIO):
                status = 200
                headers = {}

                def close(self):
                    if not self.closed:
                        with lock:
                            active[0] -= 1
                    super().close()

            def opener(request):
                with lock:
                    active[0] += 1
                    self.assertEqual(active[0], 1)
                    starts.append(monotonic())
                sleep(0.02)
                return Response(b"ok")

            client = ManagedHttpClient("site", limiter.requests, opener=opener)

            def run_operation(request):
                async def work():
                    for _ in range(3):
                        await client.request(HttpRequest("https://offline.test"))

                asyncio.run(work())
                return OperationResult(
                    request.request_id,
                    ResultKind.LIST_PAGE,
                    ListPageData("offline", (), PageInfo(1, 1, 0, 1)),
                )

            server = OwnerPipeServer(
                scope,
                handshake=OwnerHandshake(
                    OWNER_PROTOCOL_VERSION,
                    "owner",
                    OwnerMode.SHELL,
                    "0.1.4",
                    capabilities,
                    scope_fingerprint=scope.scope_fingerprint,
                ),
                handler=lambda request, emit: handle_operation(
                    request, emit, owner_session_id="owner", run_operation=run_operation
                ),
            )
            lease = OwnerManager(scope).acquire(metadata)
            server.start()
            context = multiprocessing.get_context("spawn")
            queue = context.Queue()
            processes = [
                context.Process(target=operation_cli_process, args=(directory, queue))
                for _ in range(2)
            ]
            try:
                for process in processes:
                    process.start()
                results = [queue.get(timeout=20) for _ in processes]
                for process in processes:
                    process.join(5)
                    self.assertEqual(process.exitcode, 0)
                self.assertFalse((Path(directory) / "state" / "state.sqlite3").exists())
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
                server.stop()
                lease.release()
                queue.close()
            return results, starts

    def test_concurrent_cli_processes_share_one_owner_request_budget(self):
        results, starts = self.run_clients(("operation",))
        self.assertEqual([code for code, _ in results], [0, 0], results)
        for _, output in results:
            self.assertEqual(json.loads(output)["result_kind"], "list_page")
        self.assertEqual(len(starts), 6)
        self.assertTrue(all(b - a >= 0.020 for a, b in zip(starts, starts[1:])), starts)

    def test_old_owner_is_rejected_without_local_plugin_or_http_execution(self):
        results, starts = self.run_clients(("download",))
        self.assertEqual([code for code, _ in results], [1, 1], results)
        self.assertEqual(starts, [])
        self.assertTrue(all("capabilit" in output for _, output in results), results)

    def test_live_network_snapshot_is_json_serializable_and_does_not_run_operation(self):
        import os
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import Mock, patch
        from cli_downloader.cli._network_status import print_network_status
        from cli_downloader.core.owner_protocol import OwnerResponseEvent, OwnerTerminalStatus
        from cli_downloader.core.shell_control import ShellStatusKind
        from types import SimpleNamespace

        event = OwnerResponseEvent.terminal(
            "request",
            1,
            status=OwnerTerminalStatus.COMPLETED,
            exit_code=0,
            result={"owner_available": True, "sites": [{"site_id": "site", "waiting": []}]},
        )
        with (
            TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "CLI_DOWNLOADER_DATA_ROOT": directory,
                    "CLI_DOWNLOADER_PORTABLE_ROOT": "",
                },
            ),
        ):
            with (
                patch("cli_downloader.cli._network_status.ShellController") as controller,
                patch("cli_downloader.cli._network_status.OwnerPipeClient") as client,
            ):
                controller.return_value.status.return_value = SimpleNamespace(
                    kind=ShellStatusKind.READY
                )
                client.return_value.request = Mock(return_value=(event,))
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(print_network_status(), 0)
                self.assertEqual(json.loads(output.getvalue())["sites"][0]["site_id"], "site")
