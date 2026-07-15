import json
import multiprocessing
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cli_downloader.core.errors import (
    OwnerAlreadyRunningError,
    OwnerDiscoveryError,
    OwnerScopeMismatchError,
)
from cli_downloader.core.owner_protocol import (
    OWNER_PROTOCOL_VERSION,
    OwnerLifecycleState,
    OwnerMode,
)
from cli_downloader.core.owner_scope import (
    OwnerDiscoveryMetadata,
    OwnerDiscoveryStore,
    OwnerManager,
    OwnerScope,
    canonicalize_data_root,
    current_process_start_identity,
)
from cli_downloader.cli.app import main
from tests.example_site import build_example_runtime
from tests.owner_scope_worker import hold_owner_lock


def make_metadata(scope: OwnerScope, *, session_id: str = "session-1") -> OwnerDiscoveryMetadata:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    return OwnerDiscoveryMetadata(
        scope_fingerprint=scope.scope_fingerprint,
        protocol_version=OWNER_PROTOCOL_VERSION,
        application_version="0.1.5",
        owner_mode=OwnerMode.SHELL,
        lifecycle_state=OwnerLifecycleState.READY,
        pid=os.getpid(),
        process_start_identity=current_process_start_identity(),
        session_id=session_id,
        endpoint_identity=scope.endpoint_identity,
        capabilities=("download", "status"),
        created_at=now,
        updated_at=now,
    )


class OwnerScopeTests(unittest.TestCase):
    def test_path_aliases_share_scope_but_different_roots_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Data"
            root.mkdir()
            alias = root / "." / "nested" / ".."

            self.assertEqual(canonicalize_data_root(root), canonicalize_data_root(alias))
            first = OwnerScope.from_data_root(root, user_identity="S-1-5-21-test")
            same = OwnerScope.from_data_root(alias, user_identity="S-1-5-21-test")
            other_user = OwnerScope.from_data_root(root, user_identity="S-1-5-21-other")
            other_root = OwnerScope.from_data_root(Path(directory) / "Other", user_identity="S-1-5-21-test")

            self.assertEqual(first.scope_fingerprint, same.scope_fingerprint)
            self.assertEqual(first.lock_path, same.lock_path)
            self.assertNotEqual(first.scope_fingerprint, other_user.scope_fingerprint)
            self.assertNotEqual(first.scope_fingerprint, other_root.scope_fingerprint)
            self.assertNotIn(str(root), first.endpoint_identity)

    def test_scope_uses_one_identity_for_lock_discovery_and_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)

            self.assertIn(scope.scope_fingerprint, scope.lock_path.name)
            self.assertIn(scope.scope_fingerprint, scope.discovery_path.name)
            self.assertTrue(scope.endpoint_identity.startswith("cli-downloader-"))
            self.assertIn(scope.scope_fingerprint[:32], scope.endpoint_identity)

    def test_discovery_metadata_round_trips_without_raw_scope_or_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            metadata = make_metadata(scope)
            store = OwnerDiscoveryStore(scope)

            with scope.lock().acquire() as lease:
                lease.publish(metadata)
                payload = json.loads(scope.discovery_path.read_text(encoding="utf-8"))
                self.assertNotIn("canonical_data_root", payload)
                self.assertNotIn("user_identity", payload)
                self.assertEqual(store.read(), metadata)
                self.assertEqual(OwnerDiscoveryMetadata.from_dict(payload), metadata)

            self.assertIsNone(store.read())

    def test_metadata_is_atomic_and_old_lease_cannot_delete_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory, user_identity="S-1-5-21-test")
            store = OwnerDiscoveryStore(scope)
            first = make_metadata(scope, session_id="session-old")
            second = replace(
                first,
                session_id="session-new",
                updated_at=first.updated_at + timedelta(seconds=1),
            )

            with scope.lock().acquire() as lease:
                lease.publish(first)
                store.write(second)
                self.assertFalse(lease.clear_discovery())
                self.assertEqual(store.read(), second)

    def test_corrupt_metadata_is_not_treated_as_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory, user_identity="S-1-5-21-test")
            scope.discovery_path.parent.mkdir(parents=True)
            scope.discovery_path.write_text("{not-json", encoding="utf-8")

            with self.assertRaises(OwnerDiscoveryError):
                scope.discovery().read()

    def test_metadata_scope_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory, user_identity="S-1-5-21-test")
            other = OwnerScope.from_data_root(directory, user_identity="S-1-5-21-other")
            metadata = make_metadata(other)

            with self.assertRaises(OwnerScopeMismatchError):
                scope.discovery().write(metadata)

    def test_manager_releases_lock_when_metadata_publish_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory, user_identity="S-1-5-21-test")
            bad = make_metadata(scope, session_id="bad")
            bad = replace(bad, endpoint_identity="wrong-endpoint")

            with self.assertRaises(OwnerScopeMismatchError):
                OwnerManager(scope).acquire(bad)

            with scope.lock().acquire():
                pass

    def test_two_independent_data_roots_can_be_owned_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = OwnerScope.from_data_root(Path(directory) / "one", user_identity="S-1-5-21-test")
            second = OwnerScope.from_data_root(Path(directory) / "two", user_identity="S-1-5-21-test")

            with first.lock().acquire(), second.lock().acquire():
                self.assertTrue(first.lock_path.exists())
                self.assertTrue(second.lock_path.exists())


class OwnerProcessLockTests(unittest.TestCase):
    def _start_holder(self, root: str, *, crash: bool) -> tuple[object, object, multiprocessing.Process]:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(target=hold_owner_lock, args=(root, ready, release, crash))
        process.start()
        self.assertTrue(ready.wait(15), "owner process did not acquire its lock")
        return ready, release, process

    def test_same_scope_allows_one_process_and_releases_after_normal_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _ready, release, process = self._start_holder(directory, crash=False)
            scope = OwnerScope.from_data_root(directory, user_identity="S-1-5-21-test")
            try:
                with self.assertRaises(OwnerAlreadyRunningError):
                    scope.lock().acquire()
            finally:
                release.set()
                process.join(15)
            self.assertEqual(process.exitcode, 0)

            with scope.lock().acquire():
                pass


    def test_crashed_owner_releases_os_lock_without_manual_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _ready, _release, process = self._start_holder(directory, crash=True)
            process.join(15)
            self.assertNotEqual(process.exitcode, 0)

            scope = OwnerScope.from_data_root(directory, user_identity="S-1-5-21-test")
            with scope.lock().acquire():
                pass


class OwnerExecutionIntegrationTests(unittest.TestCase):
    def test_live_download_is_rejected_before_dispatch_when_owner_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            runtime = build_example_runtime()
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with OwnerManager(scope).acquire(make_metadata(scope)):
                    with patch("cli_downloader.cli.app._run_generic_download", return_value=0) as dispatch:
                        exit_code = main(
                            ["download", "https://example.test/gallery/42", "--json"],
                            runtime=runtime,
                        )

            self.assertEqual(exit_code, 1)
            dispatch.assert_not_called()

    def test_batch_retry_is_rejected_before_pipeline_when_owner_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scope = OwnerScope.from_data_root(directory)
            runtime = build_example_runtime()
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with OwnerManager(scope).acquire(make_metadata(scope)):
                    with patch("cli_downloader.cli.app._run_batch_retry", return_value=0) as retry:
                        exit_code = main(
                            ["batches", "retry", "parent-1", "--json"],
                            runtime=runtime,
                        )

            self.assertEqual(exit_code, 1)
            retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
