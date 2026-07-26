from tests.state_store_support import StateBatchStore, StateTaskStore
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cli_downloader.application.batch_service import BatchService, SystemBatchDownloader
from cli_downloader.application.task_hierarchy_service import TaskHierarchyService
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
    DuplicateKind,
)
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.errors import BatchConflictError
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.core.operations import ListItemData, ListPageData, PageInfo
from tests.example_site import ExampleAssetFetcher
from cli_downloader.integrations.torrent import TorrentHandle, TorrentOutcome
from cli_downloader.storage.completion_store import InMemoryCompletionStore
from cli_downloader.storage.file_writer import AssetFileWriter
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.task_store import InMemoryTaskStore, TaskRecord, TaskStatus


class BatchTorrentFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, _asset):
        self.calls += 1
        return b"descriptor"


class BatchTorrentClient:
    def __init__(self, status: TorrentOutcome, poll: TorrentOutcome | None = None) -> None:
        self.status = status
        self.poll = poll or status
        self.status_calls = 0
        self.poll_calls = 0
        self.add_calls = 0

    async def get_status(self, _handle):
        self.status_calls += 1
        return self.status

    async def poll_until_complete(self, _handle, **_kwargs):
        self.poll_calls += 1
        return self.poll

    async def add_torrent(self, _torrent, _download_dir):
        self.add_calls += 1
        return TorrentHandle("new", "new.torrent")


def _batch_torrent_fixture(root: Path, outcome: TorrentOutcome):
    database = StateDatabase(root / "state.sqlite3")
    store = StateBatchStore(database)
    asset = DownloadAsset(
        site_id="example",
        gallery_id="torrent-gallery",
        plugin_asset_key="torrent-1",
        url="https://example.test/torrent/item",
        filename="item.torrent",
        index=1,
    )
    package = DownloadPackage(
        package_id="torrent-package",
        version=1,
        plugin_id="example",
        site_id="example",
        gallery_id="torrent-gallery",
        source_operation_id="resolve_gallery",
        source_url="https://example.test/gallery/torrent",
        strategy=DownloadStrategy.TORRENT,
        assets=(asset,),
        title="Torrent Gallery",
        folder_template="{gallery_id}",
    )
    entry = BatchEntry(
        entry_id="torrent-entry",
        parent_id="torrent-parent",
        plugin_id="example",
        site_id="example",
        candidate_id="torrent-candidate",
        source_operation_id="resolve_gallery",
        source_url="https://example.test/gallery/torrent",
        status=BatchEntryStatus.RESOLVING,
    )
    store.create_batch(BatchParent("torrent-parent", (entry.entry_id,)), (entry,))
    child = DownloadChild(
        child_id="torrent-child",
        parent_id="torrent-parent",
        entry_id=entry.entry_id,
        package=package,
    )
    store.attach_child(entry.entry_id, child)
    record = store.list_assets(child.child_id)[0]
    handle = outcome.handle
    store.update_asset(
        child.child_id,
        replace(
            record,
            metadata={
                "torrent_checkpoint": {
                    "client_id": handle.client_id,
                    "name": handle.name,
                    "submission": "accepted",
                    "descriptor_path": str(root / "item.torrent"),
                }
            },
        ),
    )
    return store, child


def _items() -> tuple[ListItemData, ...]:
    return (
        ListItemData(
            item_id="one",
            source_url="https://example.test/gallery/one",
            title="One",
        ),
        ListItemData(
            item_id="two",
            source_url="https://example.test/gallery/two",
            title="Two",
        ),
    )


class BatchServiceTests(unittest.TestCase):
    def test_selection_persists_explicit_download_strategy_on_entries(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            store = StateBatchStore(database)
            result = BatchService(
                store=store,
                task_store=tasks,
            ).confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="batch_download",
                items=(
                    ListItemData(
                        item_id="123-abcdef",
                        source_url="https://example.test/g/123/abcdef/",
                        title="Gallery",
                    ),
                ),
                strategy=DownloadStrategy.TORRENT,
            )

            self.assertEqual(result.entries[0].strategy, DownloadStrategy.TORRENT)
            self.assertEqual(
                store.list_entries(result.snapshot.parent.parent_id)[0].strategy,
                DownloadStrategy.TORRENT,
            )

    def test_state_selection_commits_task_execution_and_history_together(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            store = StateBatchStore(database)
            history = StateHistoryStore(database)
            result = BatchService(
                store=store,
                task_store=tasks,
                history_store=history,
            ).confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items(),
                source_url="https://example.test/list",
            )

            task = tasks.get(result.snapshot.parent.parent_id)
            self.assertEqual(task.kind, TaskKind.BATCH)
            self.assertEqual(task.source_url, "https://example.test/list")
            self.assertEqual(store.get_snapshot(result.snapshot.parent.parent_id), result.snapshot)
            self.assertEqual(len(history.list()), len(result.entries) + 1)
            history_record = history.get_record(result.snapshot.parent.parent_id)
            self.assertEqual(history_record.source_url, "https://example.test/list")

    def test_state_selection_rolls_back_task_and_history_when_execution_conflicts(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            tasks = StateTaskStore(database)
            store = StateBatchStore(database)
            history = StateHistoryStore(database)
            old_task = TaskRecord("parent-id", TaskStatus.PENDING, kind=TaskKind.BATCH, title="old")
            tasks.upsert(old_task)
            old_entry = BatchEntry(
                entry_id="old-entry",
                parent_id="parent-id",
                plugin_id="example",
                site_id="example",
                candidate_id="old",
                source_operation_id="search",
                source_url="https://example.test/old",
            )
            store.create_batch(
                BatchParent("parent-id", (old_entry.entry_id,), title="old"), (old_entry,)
            )
            service = BatchService(store=store, task_store=tasks, history_store=history)

            with patch(
                "cli_downloader.application.batch_service.uuid4",
                side_effect=(SimpleNamespace(hex="parent-id"), SimpleNamespace(hex="new-entry")),
            ):
                with self.assertRaises(BatchConflictError):
                    service.confirm_selection(
                        plugin_id="example",
                        site_id="example",
                        source_operation_id="resolve_gallery",
                        items=_items()[:1],
                    )

            self.assertEqual(tasks.get("parent-id"), old_task)
            self.assertEqual(store.get_snapshot("parent-id").parent.title, "old")
            self.assertEqual(history.list(), ())

    def test_browse_history_does_not_create_batch_storage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "state.sqlite3")
            history = StateHistoryStore(database)
            BatchService(history_store=history).record_browse(
                request_id="request-1",
                plugin_id="example",
                site_id="example",
                source_url="https://example.test/list",
                page=ListPageData(
                    title="Example",
                    items=_items(),
                    page_info=PageInfo(current_page=1, page_size=2),
                ),
            )

            self.assertEqual(StateBatchStore(database).list_parent_ids(), ())
            self.assertEqual(history.list()[0].reason, "list_page_viewed")

    def test_active_duplicates_skip_by_default_and_force_requeues(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            service = BatchService(store=store, task_store=InMemoryTaskStore())
            first = service.confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items(),
                title="Example artist",
            )
            self.assertEqual(first.snapshot.parent.title, "Example artist")
            skipped = service.confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items(),
            )
            forced = service.confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items(),
                force=True,
            )

        self.assertEqual(first.snapshot.counts.unresolved, 2)
        self.assertEqual(skipped.snapshot.counts.skipped, 2)
        self.assertEqual(set(skipped.duplicates.values()), {DuplicateKind.ACTIVE})
        self.assertEqual(forced.snapshot.counts.unresolved, 2)
        self.assertTrue(forced.forced)

    def test_completion_ledger_marks_deleted_batch_content_as_duplicate(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            completion = InMemoryCompletionStore()
            completion.record_completed("example", _items()[0].source_url)
            service = BatchService(
                store=store,
                task_store=InMemoryTaskStore(),
                completion_store=completion,
            )

            result = service.confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items()[:1],
            )

        self.assertEqual(result.duplicates, {"one": DuplicateKind.COMPLETED})
        self.assertEqual(result.snapshot.counts.skipped, 1)

    def test_completed_child_is_a_completed_duplicate(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            service = BatchService(store=store, task_store=InMemoryTaskStore())
            created = service.confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items()[:1],
            )
            entry = created.entries[0]
            store.update_entry(replace(entry, status=BatchEntryStatus.RESOLVING))
            package = DownloadPackage(
                package_id="package-1",
                version=1,
                plugin_id="example",
                site_id="example",
                gallery_id="one",
                source_operation_id="resolve_gallery",
                source_url=entry.source_url,
                strategy=DownloadStrategy.DIRECT,
                assets=(
                    DownloadAsset(
                        site_id="example",
                        gallery_id="one",
                        plugin_asset_key="1",
                        url="https://example.test/assets/one/1.jpg",
                    ),
                ),
            )
            child = DownloadChild(
                child_id="child-1",
                parent_id=entry.parent_id,
                entry_id=entry.entry_id,
                package=package,
            )
            store.attach_child(entry.entry_id, child)
            store.update_child(replace(child, status=DownloadChildStatus.DOWNLOADING))
            store.update_asset(
                child.child_id,
                replace(
                    store.list_assets(child.child_id)[0],
                    status=AssetStatus.DOWNLOADING,
                    attempt_count=1,
                ),
            )
            store.update_asset(
                child.child_id,
                replace(
                    store.list_assets(child.child_id)[0],
                    status=AssetStatus.COMPLETED,
                ),
            )
            store.update_child(replace(child, status=DownloadChildStatus.COMPLETED))
            duplicate = service.confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items()[:1],
            )

        self.assertEqual(duplicate.duplicates, {"one": DuplicateKind.COMPLETED})
        self.assertEqual(duplicate.snapshot.counts.skipped, 1)

    def test_confirm_selection_creates_batch_task_with_immutable_snapshot(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            tasks = InMemoryTaskStore()
            result = BatchService(store=store, task_store=tasks).confirm_selection(
                plugin_id="example",
                site_id="example",
                source_operation_id="resolve_gallery",
                items=_items(),
                title="Example artist",
            )

            record = tasks.get(result.snapshot.parent.parent_id)
            roots = TaskHierarchyService(task_store=tasks, batch_store=store).list_roots()

        self.assertEqual(record.kind, TaskKind.BATCH)
        self.assertEqual(record.status, TaskStatus.PENDING)
        self.assertEqual(record.title, "Example artist")
        self.assertEqual(
            record.metadata["batch_selection"]["items"],
            [
                {
                    "candidate_id": "one",
                    "source_url": "https://example.test/gallery/one",
                    "title": "One",
                },
                {
                    "candidate_id": "two",
                    "source_url": "https://example.test/gallery/two",
                    "title": "Two",
                },
            ],
        )
        self.assertEqual(tuple(node.task_id for node in roots), (record.task_id,))

    def test_batch_create_failure_removes_created_task_record(self) -> None:
        class FailingBatchStore(StateBatchStore):
            def create_batch(self, parent, entries):
                raise OSError("batch database unavailable")

        with TemporaryDirectory() as directory:
            tasks = InMemoryTaskStore()
            service = BatchService(
                store=FailingBatchStore(Path(directory) / "state.sqlite3"),
                task_store=tasks,
            )

            with self.assertRaisesRegex(OSError, "batch database unavailable"):
                service.confirm_selection(
                    plugin_id="example",
                    site_id="example",
                    source_operation_id="resolve_gallery",
                    items=_items(),
                )

        self.assertEqual(tasks.list(), ())

    def test_batch_download_uses_the_same_gallery_folder_template(self) -> None:
        asset = DownloadAsset(
            site_id="example",
            gallery_id="one",
            plugin_asset_key="1",
            url="https://example.test/assets/one/1.jpg",
            filename="1.jpg",
        )
        package = DownloadPackage(
            package_id="package-1",
            version=1,
            plugin_id="example",
            site_id="example",
            gallery_id="one",
            source_operation_id="resolve_gallery",
            source_url="https://example.test/gallery/one",
            strategy=DownloadStrategy.DIRECT,
            assets=(asset,),
            title="Gallery One",
            folder_template="{author_name}/{gallery_id}-{title}",
            folder_fields={"author_name": "Example Artist"},
        )
        child = DownloadChild(
            child_id="child-1",
            parent_id="parent-1",
            entry_id="entry-1",
            package=package,
        )
        with TemporaryDirectory() as directory:
            downloader = SystemBatchDownloader(
                file_writer=AssetFileWriter(
                    Path(directory),
                    folder_template="{site_id}/{gallery_id}-{title}",
                ),
                fetchers={"example": ExampleAssetFetcher()},
            )

            result = asyncio.run(
                downloader.execute(
                    child,
                    (AssetRecord(asset),),
                    Event(),
                    lambda _reason, _payload: None,
                )
            )

            self.assertEqual(
                Path(result[0].final_path).relative_to(directory),
                Path("Example Artist") / "one-Gallery One" / "1.jpg",
            )

    def test_batch_torrent_completed_checkpoint_skips_fetch_and_add(self) -> None:
        class CountingFetcher:
            def __init__(self) -> None:
                self.calls = 0

            async def fetch(self, _asset):
                self.calls += 1
                return b"descriptor"

        class ExistingClient:
            def __init__(self, outcome):
                self.outcome = outcome
                self.status_calls = 0
                self.add_calls = 0

            async def get_status(self, _handle):
                self.status_calls += 1
                return self.outcome

            async def add_torrent(self, _torrent, _download_dir):
                self.add_calls += 1
                return TorrentHandle("new", "new.torrent")

        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            store = StateBatchStore(database)
            asset = DownloadAsset(
                site_id="example",
                gallery_id="torrent-gallery",
                plugin_asset_key="torrent-1",
                url="https://example.test/torrent/item",
                filename="item.torrent",
                index=1,
            )
            package = DownloadPackage(
                package_id="torrent-package",
                version=1,
                plugin_id="example",
                site_id="example",
                gallery_id="torrent-gallery",
                source_operation_id="resolve_gallery",
                source_url="https://example.test/gallery/torrent",
                strategy=DownloadStrategy.TORRENT,
                assets=(asset,),
                title="Torrent Gallery",
                folder_template="{gallery_id}",
            )
            entry = BatchEntry(
                entry_id="torrent-entry",
                parent_id="torrent-parent",
                plugin_id="example",
                site_id="example",
                candidate_id="torrent-candidate",
                source_operation_id="resolve_gallery",
                source_url="https://example.test/gallery/torrent",
                status=BatchEntryStatus.RESOLVING,
            )
            store.create_batch(BatchParent("torrent-parent", (entry.entry_id,)), (entry,))
            child = DownloadChild(
                child_id="torrent-child",
                parent_id="torrent-parent",
                entry_id=entry.entry_id,
                package=package,
            )
            store.attach_child(entry.entry_id, child)
            handle = TorrentHandle("existing", "item.torrent")
            record = store.list_assets(child.child_id)[0]
            store.update_asset(
                child.child_id,
                replace(
                    record,
                    metadata={
                        "torrent_checkpoint": {
                            "client_id": handle.client_id,
                            "name": handle.name,
                            "submission": "accepted",
                            "descriptor_path": str(Path(directory) / "item.torrent"),
                        }
                    },
                ),
            )
            client = ExistingClient(
                TorrentOutcome(
                    handle,
                    "completed",
                    str(Path(directory)),
                    found=True,
                    progress=1.0,
                    completed_bytes=1024,
                    total_bytes=1024,
                )
            )
            fetcher = CountingFetcher()
            downloader = SystemBatchDownloader(
                file_writer=AssetFileWriter(Path(directory)),
                store=store,
                fetchers={"example": fetcher},
                torrent_client=client,
            )

            outcomes = asyncio.run(
                downloader.execute(
                    child,
                    (store.list_assets(child.child_id)[0],),
                    Event(),
                    lambda _reason, _data: None,
                )
            )

            self.assertEqual(outcomes[0].status, AssetStatus.COMPLETED)
            self.assertEqual(fetcher.calls, 0)
            self.assertEqual(client.add_calls, 0)
            self.assertEqual(client.status_calls, 1)
            self.assertEqual(store.list_assets(child.child_id)[0].status, AssetStatus.COMPLETED)

    def test_batch_torrent_downloading_and_paused_checkpoints_poll_existing_handle(self) -> None:
        for client_state in ("downloading", "paused"):
            with self.subTest(client_state=client_state), TemporaryDirectory() as directory:
                root = Path(directory)
                handle = TorrentHandle("existing", "item.torrent")
                initial = TorrentOutcome(
                    handle,
                    client_state,
                    str(root),
                    found=True,
                    client_state="pausedDL" if client_state == "paused" else "downloading",
                )
                completed = TorrentOutcome(
                    handle,
                    "completed",
                    str(root),
                    found=True,
                    progress=1.0,
                    completed_bytes=1024,
                    total_bytes=1024,
                )
                store, child = _batch_torrent_fixture(root, initial)
                client = BatchTorrentClient(initial, completed)
                fetcher = BatchTorrentFetcher()
                downloader = SystemBatchDownloader(
                    file_writer=AssetFileWriter(root),
                    store=store,
                    fetchers={"example": fetcher},
                    torrent_client=client,
                )

                outcomes = asyncio.run(
                    downloader.execute(
                        child,
                        (store.list_assets(child.child_id)[0],),
                        Event(),
                        lambda _reason, _data: None,
                    )
                )

                self.assertEqual(outcomes[0].status, AssetStatus.COMPLETED)
                self.assertEqual(fetcher.calls, 0)
                self.assertEqual(client.add_calls, 0)
                self.assertEqual(client.status_calls, 1)
                self.assertEqual(client.poll_calls, 1)

    def test_batch_torrent_client_error_stays_retryable(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            handle = TorrentHandle("existing", "item.torrent")
            failed = TorrentOutcome(
                handle,
                "error",
                str(root),
                found=None,
                error="client offline",
            )
            store, child = _batch_torrent_fixture(root, failed)
            client = BatchTorrentClient(failed)
            fetcher = BatchTorrentFetcher()
            downloader = SystemBatchDownloader(
                file_writer=AssetFileWriter(root),
                store=store,
                fetchers={"example": fetcher},
                torrent_client=client,
            )

            outcomes = asyncio.run(
                downloader.execute(
                    child,
                    (store.list_assets(child.child_id)[0],),
                    Event(),
                    lambda _reason, _data: None,
                )
            )

            self.assertEqual(outcomes[0].status, AssetStatus.FAILED)
            self.assertTrue(outcomes[0].failure.retryable)
            self.assertEqual(outcomes[0].failure.code, "torrent_reconcile_failed")
            self.assertEqual(fetcher.calls, 0)
            self.assertEqual(client.add_calls, 0)
            self.assertEqual(client.status_calls, 1)
            self.assertEqual(client.poll_calls, 0)


if __name__ == "__main__":
    unittest.main()
