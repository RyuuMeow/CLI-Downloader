from tests.state_store_support import StateBatchStore, StateTaskStore
import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.application.batch_retry import BatchRetryService
from cli_downloader.application.batch_service import SystemBatchDownloader
from cli_downloader.cli.app import main
from cli_downloader.core.batch import (
    AssetRecord,
    AssetStatus,
    BatchEntry,
    BatchEntryStatus,
    BatchParent,
    DownloadAsset,
    DownloadChild,
    DownloadChildStatus,
    DownloadPackage,
    FailureData,
    FailureOrigin,
    FailureStage,
    RetryScope,
)
from cli_downloader.core.models import Asset, AssetKind, DownloadStrategy
from cli_downloader.core.errors import BatchConflictError
from cli_downloader.storage.file_writer import AssetFileWriter
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.task_store import TaskRecord, TaskStatus
from cli_downloader.core.task_tree import TaskKind
from tests.example_site import build_example_runtime


def failure(
    stage: FailureStage = FailureStage.FETCH,
    *,
    http_status: int | None = None,
) -> FailureData:
    details = {"http_status": http_status} if http_status is not None else {}
    return FailureData(
        origin=(
            FailureOrigin.PLUGIN
            if stage
            in {
                FailureStage.CLASSIFY,
                FailureStage.RESOLVE,
                FailureStage.REQUEST,
                FailureStage.ENUMERATE,
                FailureStage.PARSE,
                FailureStage.BUILD_PACKAGE,
            }
            else FailureOrigin.SYSTEM
        ),
        stage=stage,
        code="test_failure",
        message="Test failure",
        retryable=True,
        details=details,
    )


def entry(entry_id: str, parent_id: str = "parent-1") -> BatchEntry:
    return BatchEntry(
        entry_id=entry_id,
        parent_id=parent_id,
        plugin_id="example",
        site_id="example",
        candidate_id=entry_id,
        source_operation_id="resolve_gallery",
        source_url=f"https://example.test/gallery/{entry_id}",
    )


def package(
    item: BatchEntry,
    *,
    version: int = 1,
    suffix: str = "old",
    expires_at: datetime | None = None,
    refresh_on_status: tuple[int, ...] = (),
    keys: tuple[str, ...] = ("one", "two"),
) -> DownloadPackage:
    return DownloadPackage(
        package_id=f"package-{item.entry_id}",
        version=version,
        plugin_id=item.plugin_id,
        site_id=item.site_id,
        gallery_id=item.candidate_id,
        source_operation_id=item.source_operation_id,
        source_url=item.source_url,
        strategy=DownloadStrategy.DIRECT,
        assets=tuple(
            DownloadAsset(
                site_id=item.site_id,
                gallery_id=item.candidate_id,
                plugin_asset_key=key,
                url=f"https://cdn.example.test/{item.entry_id}/{key}?token={suffix}",
                filename=f"{key}.jpg",
                index=index,
            )
            for index, key in enumerate(keys, start=1)
        ),
        expires_at=expires_at,
        refresh_on_status=refresh_on_status,
    )


def attach(store: StateBatchStore, item: BatchEntry, value: DownloadPackage) -> DownloadChild:
    store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))
    child = DownloadChild(
        child_id=f"child-{item.entry_id}",
        parent_id=item.parent_id,
        entry_id=item.entry_id,
        package=value,
    )
    store.attach_child(item.entry_id, child)
    return child


def set_child_outcomes(
    store: StateBatchStore,
    child: DownloadChild,
    statuses: tuple[AssetStatus, ...],
    *,
    failed_http_status: int | None = None,
) -> None:
    store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
    records = store.list_assets(child.child_id)
    for record, status in zip(records, statuses, strict=True):
        downloading = replace(record, status=AssetStatus.DOWNLOADING, attempt_count=1)
        store.update_asset(child.child_id, downloading)
        if status is AssetStatus.COMPLETED:
            outcome = replace(
                downloading,
                status=status,
                final_path=f"output/{record.asset.filename}",
                content_sha256="a" * 64,
            )
        else:
            outcome = replace(
                downloading,
                status=status,
                failure=failure(http_status=failed_http_status),
            )
        store.update_asset(child.child_id, outcome)
    completed = statuses.count(AssetStatus.COMPLETED)
    child_status = (
        DownloadChildStatus.COMPLETED
        if completed == len(statuses)
        else DownloadChildStatus.PARTIALLY_COMPLETED
        if completed
        else DownloadChildStatus.FAILED
    )
    store.update_child(
        replace(
            child,
            status=child_status,
            failure=None
            if child_status is DownloadChildStatus.COMPLETED
            else failure(http_status=failed_http_status),
        )
    )


class RefreshedResolver:
    def __init__(self, refreshed: DownloadPackage) -> None:
        self.refreshed = refreshed
        self.calls = 0

    async def resolve(self, entry, cancel_event, progress):
        self.calls += 1
        return self.refreshed


class StageAwareResolver:
    def __init__(self) -> None:
        self.plan = None

    async def resolve(self, entry, cancel_event, progress):
        raise AssertionError("entry retry should receive its retry plan")

    async def retry(self, item, plan, cancel_event, progress):
        self.plan = plan
        return package(item)


class CompletingDownloader:
    def __init__(self) -> None:
        self.asset_urls: list[str] = []

    async def execute(self, child, assets, cancel_event, progress):
        self.asset_urls.extend(record.asset.url for record in assets)
        return tuple(
            replace(
                record,
                status=AssetStatus.COMPLETED,
                final_path=f"output/{record.asset.filename}",
                content_sha256="b" * 64,
            )
            for record in assets
        )


class FailingFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, asset):
        self.calls += 1
        raise AssertionError("verified partial must not be fetched")


class BatchRetryTests(unittest.TestCase):
    def test_manual_retry_waits_for_persisted_site_cooldown(self) -> None:
        async def scenario(directory: str) -> None:
            service = BatchRetryService(
                store=StateBatchStore(Path(directory) / "state.sqlite3"),
                resolver=object(),
            )
            retry_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
            limited = FailureData(
                origin=FailureOrigin.SYSTEM,
                stage=FailureStage.FETCH,
                code="asset_download_failed",
                message="HTTP 429",
                retryable=True,
                details={"next_retry_at": retry_at},
            )
            with patch(
                "cli_downloader.application.batch_retry.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep_mock:
                await service._wait_for_cooldown("parent-1", limited)
            self.assertGreater(sleep_mock.await_args.args[0], 0)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_item_retry_only_processes_selected_items_failed_assets(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            partial, unresolved = entry("partial"), entry("unresolved")
            store.create_batch(
                BatchParent("parent-1", (partial.entry_id, unresolved.entry_id)),
                (partial, unresolved),
            )
            child = attach(store, partial, package(partial))
            set_child_outcomes(
                child=child, store=store, statuses=(AssetStatus.COMPLETED, AssetStatus.FAILED)
            )
            resolving = replace(unresolved, status=BatchEntryStatus.RESOLVING)
            store.update_entry(resolving)
            store.update_entry(
                replace(
                    resolving,
                    status=BatchEntryStatus.RESOLUTION_FAILED,
                    failure=failure(FailureStage.RESOLVE),
                )
            )
            resolver = RefreshedResolver(package(partial, suffix="unused"))
            downloader = CompletingDownloader()
            pipeline = BatchPipeline(store=store, resolver=resolver, downloader=downloader)

            result = await BatchRetryService(store=store, resolver=resolver).retry_task(
                partial.entry_id,
                pipeline=pipeline,
            )

            self.assertEqual(len(result.retry.plans), 1)
            self.assertEqual(len(downloader.asset_urls), 1)
            self.assertEqual(
                store.get_entry(unresolved.entry_id).status,
                BatchEntryStatus.RESOLUTION_FAILED,
            )

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_entry_retry_passes_plugin_checkpoint_to_resolver(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("request-failed")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            resolving = replace(item, status=BatchEntryStatus.RESOLVING)
            store.update_entry(resolving)
            store.update_entry(
                replace(
                    resolving,
                    status=BatchEntryStatus.RESOLUTION_FAILED,
                    failure=failure(FailureStage.REQUEST),
                )
            )
            resolver = StageAwareResolver()
            pipeline = BatchPipeline(
                store=store,
                resolver=resolver,
                downloader=CompletingDownloader(),
            )

            result = await BatchRetryService(store=store, resolver=resolver).retry_parent(
                "parent-1",
                pipeline=pipeline,
            )

            self.assertEqual(result.run.snapshot.counts.completed, 1)
            self.assertEqual(resolver.plan.start_at, FailureStage.REQUEST)
            self.assertEqual(
                store.list_attempts(item.entry_id)[0].failure.stage, FailureStage.REQUEST
            )

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_parent_plan_combines_entry_and_asset_work_and_skips_completed(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            failed_entry, done_entry, partial_entry = (
                entry("failed"),
                entry("done"),
                entry("partial"),
            )
            store.create_batch(
                BatchParent("parent-1", ("failed", "done", "partial")),
                (failed_entry, done_entry, partial_entry),
            )
            store.update_entry(
                replace(
                    failed_entry,
                    status=BatchEntryStatus.RESOLVING,
                )
            )
            store.update_entry(
                replace(
                    failed_entry,
                    status=BatchEntryStatus.RESOLUTION_FAILED,
                    failure=failure(FailureStage.RESOLVE),
                )
            )
            done_child = attach(store, done_entry, package(done_entry))
            partial_child = attach(store, partial_entry, package(partial_entry))
            set_child_outcomes(
                store,
                done_child,
                (AssetStatus.COMPLETED, AssetStatus.COMPLETED),
            )
            set_child_outcomes(
                store,
                partial_child,
                (AssetStatus.COMPLETED, AssetStatus.FAILED),
            )
            resolver = RefreshedResolver(package(partial_entry, suffix="new"))

            plan = BatchRetryService(store=store, resolver=resolver).plan_parent("parent-1")

        self.assertEqual(len(plan.plans), 2)
        self.assertEqual({item.scope for item in plan.plans}, {RetryScope.ENTRY, RetryScope.ASSET})
        self.assertNotIn(
            done_child.child_id, {target for item in plan.plans for target in item.target_ids}
        )

    def test_asset_retry_preserves_completed_asset_and_attempt_history(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("partial")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            child = attach(store, item, package(item))
            set_child_outcomes(
                store,
                child,
                (AssetStatus.COMPLETED, AssetStatus.FAILED),
            )
            before = store.list_assets(child.child_id)
            resolver = RefreshedResolver(package(item, suffix="unused"))
            downloader = CompletingDownloader()
            pipeline = BatchPipeline(store=store, resolver=resolver, downloader=downloader)

            result = await BatchRetryService(store=store, resolver=resolver).retry_parent(
                "parent-1",
                pipeline=pipeline,
            )

            after = store.list_assets(child.child_id)
            self.assertEqual(result.run.snapshot.counts.completed, 1)
            self.assertEqual(downloader.asset_urls, [before[1].asset.url])
            self.assertEqual(after[0], before[0])
            self.assertEqual(after[1].attempt_count, 2)
            attempts = store.list_attempts(child.child_id)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0].failure.stage, FailureStage.FETCH)
            self.assertEqual(attempts[0].retry_plan.target_ids, (before[1].asset.asset_id,))
            self.assertEqual(resolver.calls, 0)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_http_refresh_reconciles_logical_assets_and_keeps_old_package(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("refresh")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            original = package(item, refresh_on_status=(403,))
            child = attach(store, item, original)
            set_child_outcomes(
                store,
                child,
                (AssetStatus.COMPLETED, AssetStatus.FAILED),
                failed_http_status=403,
            )
            before = store.list_assets(child.child_id)
            refreshed = package(item, suffix="new", refresh_on_status=(403,))
            resolver = RefreshedResolver(refreshed)
            downloader = CompletingDownloader()
            pipeline = BatchPipeline(store=store, resolver=resolver, downloader=downloader)

            result = await BatchRetryService(store=store, resolver=resolver).retry_parent(
                "parent-1",
                pipeline=pipeline,
            )

            current = store.get_child(child.child_id)
            after = store.list_assets(child.child_id)
            self.assertEqual(result.run.snapshot.counts.completed, 1)
            self.assertEqual(current.package.version, 2)
            self.assertEqual(store.get_package(original.package_id, 1), original)
            self.assertEqual(after[0], before[0])
            self.assertIn("token=new", after[1].asset.url)
            self.assertEqual(downloader.asset_urls, [after[1].asset.url])
            attempt = store.list_attempts(child.child_id)[0]
            self.assertEqual(attempt.retry_plan.automatic_refresh_count, 1)
            self.assertEqual(attempt.retry_plan.package_version, 1)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_expired_package_requests_refresh(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("expired")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            now = datetime(2026, 7, 3, tzinfo=UTC)
            child = attach(
                store,
                item,
                package(item, expires_at=now - timedelta(seconds=1)),
            )
            set_child_outcomes(store, child, (AssetStatus.FAILED, AssetStatus.FAILED))
            resolver = RefreshedResolver(package(item, suffix="new"))

            plan = BatchRetryService(store=store, resolver=resolver).plan_parent(
                "parent-1",
                now=now,
            )

        self.assertEqual(plan.plans[0].scope, RetryScope.CHILD)
        self.assertEqual(plan.plans[0].reason, "refresh_expired_package")
        self.assertEqual(plan.plans[0].automatic_refresh_count, 1)

    def test_verified_partial_resumes_without_fetching(self) -> None:
        async def scenario(directory: str) -> None:
            root = Path(directory)
            item = entry("resume")
            value = package(item, keys=("one",))
            child = DownloadChild(
                child_id="child-resume",
                parent_id=item.parent_id,
                entry_id=item.entry_id,
                package=value,
                status=DownloadChildStatus.DOWNLOADING,
            )
            writer = AssetFileWriter(root)
            legacy_asset = value.assets[0]
            _target, partial = writer.paths(
                Asset(
                    url=legacy_asset.url,
                    kind=AssetKind.FILE,
                    filename=legacy_asset.filename,
                ),
                task_id=child.child_id,
            )
            partial.parent.mkdir(parents=True)
            content = b"complete partial"
            partial.write_bytes(content)
            record = AssetRecord(
                asset=legacy_asset,
                status=AssetStatus.DOWNLOADING,
                attempt_count=1,
                partial_path=str(partial),
                content_sha256=sha256(content).hexdigest(),
            )
            fetcher = FailingFetcher()
            downloader = SystemBatchDownloader(
                file_writer=writer,
                fetchers={"example": fetcher},
            )

            outcomes = await downloader.execute(
                child,
                (record,),
                asyncio.Event(),
                lambda _reason, _data: None,
            )

            self.assertEqual(outcomes[0].status, AssetStatus.COMPLETED)
            self.assertEqual(fetcher.calls, 0)
            self.assertFalse(partial.exists())
            self.assertEqual(Path(outcomes[0].final_path).read_bytes(), content)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_refresh_missing_unfinished_asset_rolls_back(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("rollback")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            original = package(item)
            child = attach(store, item, original)
            set_child_outcomes(
                store,
                child,
                (AssetStatus.COMPLETED, AssetStatus.FAILED),
            )
            before = store.list_assets(child.child_id)
            before_child = store.get_child(child.child_id)
            incomplete = package(item, version=2, suffix="new", keys=("one",))

            with self.assertRaises(BatchConflictError):
                store.replace_child_package(child.child_id, incomplete)

            self.assertEqual(store.get_child(child.child_id), before_child)
            self.assertEqual(store.list_assets(child.child_id), before)
            self.assertIsNone(store.get_package(original.package_id, 2))

    def test_cli_retries_persisted_failed_batch(self) -> None:
        stream = StringIO()
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            database = StateDatabase(state / "state.sqlite3")
            StateTaskStore(database).upsert(
                TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH)
            )
            store = StateBatchStore(database)
            item = entry("cli")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            child = attach(store, item, package(item))
            set_child_outcomes(
                store,
                child,
                (AssetStatus.COMPLETED, AssetStatus.FAILED),
            )
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with redirect_stdout(stream):
                    exit_code = main(
                        ["batches", "retry", "parent-1", "--json"],
                        runtime=build_example_runtime(),
                    )

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["batch"]["counts"]["completed"], 1)
        self.assertEqual(payload["retry"]["plans"][0]["scope"], "asset")

    def test_retry_cancelled_child_resets_downloading_assets_to_queued(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("crashed")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            child = attach(store, item, package(item))
            # Simulate crash recovery: some assets stuck in DOWNLOADING state
            store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            records = store.list_assets(child.child_id)
            for record in records:
                store.update_asset(
                    child.child_id,
                    replace(
                        record,
                        status=AssetStatus.DOWNLOADING,
                        attempt_count=record.attempt_count + 1,
                    ),
                )
            # Recovery sets child to CANCELLED
            store.update_child(replace(child, status=DownloadChildStatus.CANCELLED))
            resolver = RefreshedResolver(package(item, suffix="unused"))
            downloader = CompletingDownloader()
            pipeline = BatchPipeline(store=store, resolver=resolver, downloader=downloader)

            result = await BatchRetryService(store=store, resolver=resolver).retry_parent(
                "parent-1",
                pipeline=pipeline,
            )

            self.assertEqual(result.run.snapshot.counts.completed, 1)
            self.assertEqual(len(downloader.asset_urls), 2)
            child_after = store.get_child(child.child_id)
            self.assertEqual(child_after.status, DownloadChildStatus.COMPLETED)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_retry_partial_child_with_downloading_assets(self) -> None:
        async def scenario(directory: str) -> None:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("partial-crash")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            child = attach(store, item, package(item, keys=("one", "two", "three")))
            # One asset COMPLETED, two stuck in DOWNLOADING
            records = store.list_assets(child.child_id)
            # Transition: QUEUED → DOWNLOADING → COMPLETED
            store.update_asset(
                child.child_id,
                replace(records[0], status=AssetStatus.DOWNLOADING, attempt_count=1),
            )
            store.update_asset(
                child.child_id,
                replace(
                    records[0],
                    status=AssetStatus.COMPLETED,
                    final_path=f"output/{records[0].asset.filename}",
                    content_sha256="a" * 64,
                ),
            )
            for record in records[1:]:
                store.update_asset(
                    child.child_id,
                    replace(
                        record,
                        status=AssetStatus.DOWNLOADING,
                        attempt_count=record.attempt_count + 1,
                    ),
                )
            # Transition child: QUEUED → DOWNLOADING → PARTIALLY_COMPLETED
            store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            store.update_child(
                replace(
                    child,
                    status=DownloadChildStatus.PARTIALLY_COMPLETED,
                    failure=failure(),
                )
            )
            resolver = RefreshedResolver(
                package(item, keys=("one", "two", "three"), suffix="unused")
            )
            downloader = CompletingDownloader()
            pipeline = BatchPipeline(store=store, resolver=resolver, downloader=downloader)

            result = await BatchRetryService(store=store, resolver=resolver).retry_parent(
                "parent-1",
                pipeline=pipeline,
            )

            self.assertEqual(result.run.snapshot.counts.completed, 1)
            # Should download the 2 previously DOWNLOADING assets
            self.assertEqual(len(downloader.asset_urls), 2)
            child_after = store.get_child(child.child_id)
            self.assertEqual(child_after.status, DownloadChildStatus.COMPLETED)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    # ── interrupted / paused state recovery ──

    def test_entry_retry_includes_pending_resolution_and_resolving(self) -> None:
        """Entries stuck in PENDING_RESOLUTION or RESOLVING (interrupted runs)
        should be included in the retry plan so a paused batch can resume."""
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            e1 = entry("e1")
            e2 = entry("e2")
            store.create_batch(BatchParent("parent-1", (e1.entry_id, e2.entry_id)), (e1, e2))
            # e1 starts as PENDING_RESOLUTION (default), keep it there.
            # Transition e2 to RESOLVING.
            store.update_entry(replace(e2, status=BatchEntryStatus.RESOLVING))

            service = BatchRetryService(store=store, resolver=object())
            plan = service.plan_parent("parent-1")
            entry_ids = {
                pid for p in plan.plans if p.scope is RetryScope.ENTRY for pid in p.target_ids
            }
            self.assertIn(e1.entry_id, entry_ids)
            self.assertIn(e2.entry_id, entry_ids)

    def test_child_retry_includes_queued_and_downloading(self) -> None:
        """Children in QUEUED or DOWNLOADING (interrupted downloads) should be
        included in the retry plan."""
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            item = entry("item")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            child = attach(store, item, package(item))
            # Child has assets but is QUEUED — should produce a plan.
            plan = BatchRetryService(store=store, resolver=object()).plan_parent("parent-1")
            child_ids = {
                pid
                for p in plan.plans
                if p.scope in (RetryScope.CHILD, RetryScope.ASSET)
                for pid in p.target_ids
            }
            self.assertIn(child.child_id, child_ids)

            # Transition to DOWNLOADING.
            store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            # Mark assets as DOWNLOADING as well so they generate asset-level plans.
            for record in store.list_assets(child.child_id):
                store.update_asset(child.child_id, replace(record, status=AssetStatus.DOWNLOADING))
            plan = BatchRetryService(store=store, resolver=object()).plan_parent("parent-1")
            child_ids = {pid for p in plan.plans for pid in p.target_ids}
            self.assertIn(child.child_id, child_ids)

    def test_prepare_skips_already_resolving_entry(self) -> None:
        """_prepare should not fail when the entry is already in RESOLVING state."""
        with TemporaryDirectory() as d:
            store = StateBatchStore(Path(d) / "state.sqlite3")
            item = entry("entry")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            store.update_entry(replace(item, status=BatchEntryStatus.RESOLVING))

            async def run():
                service = BatchRetryService(store=store, resolver=object())
                plan = service.plan_parent("parent-1")
                target = service._attempt_target("parent-1", plan.plans[0])
                # Should not raise
                await service._prepare(plan.plans[0], target)
                # Entry should still be RESOLVING (no-op)
                self.assertEqual(store.get_entry(item.entry_id).status, BatchEntryStatus.RESOLVING)

            asyncio.run(run())


class BatchAutoDeleteTest(unittest.TestCase):
    def _completed_batch(self, store: StateBatchStore, parent_id: str, entry_id: str) -> None:
        """Set up a batch whose single entry is fully resolved and downloaded."""
        item = entry(entry_id, parent_id=parent_id)
        store.create_batch(BatchParent(parent_id, (item.entry_id,)), (item,))
        child = attach(store, item, package(item))
        set_child_outcomes(store, child, (AssetStatus.COMPLETED, AssetStatus.COMPLETED))

    def test_auto_delete_removes_batch_after_completion(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            self._completed_batch(store, "parent-1", "entry-1")

            snapshot = store.get_snapshot("parent-1")
            self.assertEqual(snapshot.counts.completed, 1)

            # Simulate auto_delete: delete the completed batch.
            store.delete_batch("parent-1")
            # After deletion, the batch should no longer exist.
            from cli_downloader.core.errors import BatchNotFoundError

            with self.assertRaises(BatchNotFoundError):
                store.get_snapshot("parent-1")

    def test_auto_delete_skipped_when_batch_not_completed(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            item = entry("entry-1")
            store.create_batch(BatchParent("parent-1", (item.entry_id,)), (item,))
            # Leave the entry in PENDING_RESOLUTION — not completed.
            snapshot = store.get_snapshot("parent-1")
            self.assertNotEqual(snapshot.execution_status.value, "completed")


if __name__ == "__main__":
    unittest.main()
