import json
import unittest
from datetime import UTC, datetime

from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    AttemptData,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    Checkpoint,
    CheckpointData,
    DownloadAsset,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureOrigin,
    FailureStage,
    RetryPlan,
    RetryScope,
    derive_asset_id,
    validate_asset_transition,
    validate_child_transition,
    validate_entry_transition,
)
from cli_downloader.core.errors import ContractValidationError
from cli_downloader.core.models import DownloadStrategy


def make_asset(**changes: object) -> DownloadAsset:
    values: dict[str, object] = {
        "site_id": "example",
        "gallery_id": "gallery-42",
        "plugin_asset_key": "page-1",
        "url": "https://cdn.example.test/images/1.jpg?token=first",
        "filename": "001.jpg",
        "index": 0,
        "headers": {"User-Agent": "CLI-Downloader"},
        "referrer": "https://example.test/gallery/42",
        "metadata": {"width": 1200},
    }
    values.update(changes)
    return DownloadAsset(**values)


def make_package(**changes: object) -> DownloadPackage:
    values: dict[str, object] = {
        "package_id": "package-1",
        "version": 1,
        "plugin_id": "example_plugin",
        "site_id": "example",
        "gallery_id": "gallery-42",
        "source_operation_id": "resolve_gallery",
        "source_url": "https://example.test/gallery/42",
        "strategy": DownloadStrategy.DIRECT,
        "assets": (make_asset(),),
        "expires_at": datetime(2026, 7, 3, 12, tzinfo=UTC),
        "refresh_on_status": (401, 403),
        "secret_refs": {"session": "secrets.example.session"},
    }
    values.update(changes)
    return DownloadPackage(**values)


def plugin_failure(**changes: object) -> FailureData:
    values: dict[str, object] = {
        "origin": FailureOrigin.PLUGIN,
        "stage": FailureStage.RESOLVE,
        "code": "gallery_unavailable",
        "message": "Gallery could not be resolved",
        "retryable": True,
    }
    values.update(changes)
    return FailureData(**values)


def system_failure(**changes: object) -> FailureData:
    values: dict[str, object] = {
        "origin": FailureOrigin.SYSTEM,
        "stage": FailureStage.FETCH,
        "code": "http_error",
        "message": "Asset request failed",
        "retryable": True,
        "details": {"status": 503},
    }
    values.update(changes)
    return FailureData(**values)


class AssetIdentityTests(unittest.TestCase):
    def test_asset_id_uses_logical_identity_not_url(self) -> None:
        first = make_asset(url="https://cdn.example.test/1.jpg?token=first")
        refreshed = make_asset(url="https://cdn.example.test/1.jpg?token=second")

        self.assertEqual(first.asset_id, refreshed.asset_id)
        self.assertEqual(
            first.asset_id,
            derive_asset_id("example", "gallery-42", "page-1"),
        )

    def test_logical_key_changes_asset_id(self) -> None:
        self.assertNotEqual(make_asset().asset_id, make_asset(plugin_asset_key="page-2").asset_id)

    def test_asset_serializes_without_secret_values(self) -> None:
        payload = make_asset().to_dict()

        self.assertEqual(payload["plugin_asset_key"], "page-1")
        self.assertEqual(len(payload["asset_id"]), 64)
        json.dumps(payload)


class PackageAndChildTests(unittest.TestCase):
    def test_complete_package_and_child_serialize(self) -> None:
        child = DownloadChild(
            child_id="child-1",
            parent_id="parent-1",
            entry_id="entry-1",
            package=make_package(),
        )

        payload = child.to_dict()
        self.assertEqual(payload["package"]["assets"][0]["filename"], "001.jpg")
        self.assertEqual(payload["package"]["expires_at"], "2026-07-03T12:00:00+00:00")
        self.assertEqual(payload["package"]["secret_refs"], {"session": "secrets.example.session"})
        json.dumps(payload)

    def test_package_requires_at_least_one_asset(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_package(assets=())

        self.assertEqual(context.exception.code, "incomplete_package")

    def test_package_requires_selected_strategy(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_package(strategy=DownloadStrategy.AUTO)

        self.assertEqual(context.exception.code, "incomplete_package")
        self.assertEqual(context.exception.path, "package.strategy")

    def test_asset_identity_must_match_package(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_package(assets=(make_asset(gallery_id="other"),))

        self.assertEqual(context.exception.code, "asset_identity_mismatch")

    def test_duplicate_logical_assets_are_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_package(assets=(make_asset(), make_asset(url="https://cdn.example.test/new")))

        self.assertEqual(context.exception.code, "duplicate_asset_id")

    def test_package_expiry_must_be_timezone_aware(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            make_package(expires_at=datetime(2026, 7, 3, 12))

        self.assertEqual(context.exception.code, "invalid_datetime")

    def test_child_rejects_partial_or_arbitrary_package(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            DownloadChild(
                child_id="child-1",
                parent_id="parent-1",
                entry_id="entry-1",
                package={"assets": []},
            )

        self.assertEqual(context.exception.code, "incomplete_package")


class FailureAndEntryTests(unittest.TestCase):
    def test_failure_origin_must_match_stage(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            plugin_failure(stage=FailureStage.FETCH)

        self.assertEqual(context.exception.code, "failure_origin_mismatch")

    def test_resolution_failure_retains_entry_without_child(self) -> None:
        entry = BatchEntry(
            entry_id="entry-1",
            parent_id="parent-1",
            plugin_id="example_plugin",
            site_id="example",
            candidate_id="result-42",
            source_operation_id="search",
            source_url="https://example.test/gallery/42",
            status=BatchEntryStatus.RESOLUTION_FAILED,
            failure=plugin_failure(),
        )

        self.assertIsNone(entry.child_id)
        self.assertEqual(entry.to_dict()["failure"]["origin"], "plugin")

    def test_package_ready_entry_requires_child_id(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            BatchEntry(
                entry_id="entry-1",
                parent_id="parent-1",
                plugin_id="example_plugin",
                site_id="example",
                candidate_id="result-42",
                source_operation_id="search",
                source_url="https://example.test/gallery/42",
                status=BatchEntryStatus.PACKAGE_READY,
            )

        self.assertEqual(context.exception.code, "missing_child")

    def test_package_rejection_requires_pre_child_failure(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            BatchEntry(
                entry_id="entry-1",
                parent_id="parent-1",
                plugin_id="example_plugin",
                site_id="example",
                candidate_id="result-42",
                source_operation_id="search",
                source_url="https://example.test/gallery/42",
                status=BatchEntryStatus.PACKAGE_REJECTED,
                failure=system_failure(),
            )

        self.assertEqual(context.exception.code, "invalid_failure")

    def test_parent_requires_unique_entries(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            BatchParent(parent_id="parent-1", entry_ids=("entry-1", "entry-1"))

        self.assertEqual(context.exception.code, "duplicate_value")


class StateTransitionTests(unittest.TestCase):
    def test_valid_retry_transitions(self) -> None:
        validate_entry_transition(
            BatchEntryStatus.RESOLUTION_FAILED,
            BatchEntryStatus.RESOLVING,
        )
        validate_child_transition(
            DownloadChildStatus.PARTIALLY_COMPLETED,
            DownloadChildStatus.QUEUED,
        )
        validate_asset_transition(AssetStatus.FAILED, AssetStatus.QUEUED)

    def test_completed_child_is_terminal(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            validate_child_transition(
                DownloadChildStatus.COMPLETED,
                DownloadChildStatus.QUEUED,
            )

        self.assertEqual(context.exception.code, "invalid_status_transition")


class RetryAndAttemptTests(unittest.TestCase):
    def test_asset_retry_and_attempt_serialize(self) -> None:
        plan = RetryPlan(
            plan_id="retry-1",
            start_at=FailureStage.FETCH,
            scope=RetryScope.ASSET,
            target_ids=(make_asset().asset_id,),
            reason="Retry failed assets",
            package_version=1,
            automatic_refresh_count=1,
        )
        attempt = AttemptData(
            attempt_id="attempt-2",
            target_id="child-1",
            number=2,
            checkpoints=(
                CheckpointData(
                    checkpoint=Checkpoint.ASSET_FETCHED,
                    target_id=make_asset().asset_id,
                    details={"bytes": 1024},
                ),
            ),
            retry_plan=plan,
            failure=system_failure(),
        )

        payload = attempt.to_dict()
        self.assertEqual(payload["retry_plan"]["scope"], "asset")
        self.assertEqual(payload["checkpoints"][0]["checkpoint"], "asset_fetched")
        json.dumps(payload)

    def test_asset_retry_rejects_plugin_stage(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            RetryPlan(
                plan_id="retry-1",
                start_at=FailureStage.RESOLVE,
                scope=RetryScope.ASSET,
                target_ids=("asset-1",),
                reason="Wrong checkpoint",
            )

        self.assertEqual(context.exception.code, "retry_scope_mismatch")

    def test_automatic_package_refresh_is_limited_to_one(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            RetryPlan(
                plan_id="retry-1",
                start_at=FailureStage.RESOLVE,
                scope=RetryScope.ENTRY,
                target_ids=("entry-1",),
                reason="Refresh expired package",
                automatic_refresh_count=2,
            )

        self.assertEqual(context.exception.code, "refresh_limit_exceeded")

    def test_failed_asset_record_requires_system_failure(self) -> None:
        record = AssetRecord(
            asset=make_asset(),
            status=AssetStatus.FAILED,
            attempt_count=1,
            failure=system_failure(),
            partial_path="001.jpg.part",
        )

        self.assertEqual(record.to_dict()["status"], "failed")


if __name__ == "__main__":
    unittest.main()
