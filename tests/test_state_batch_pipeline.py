import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.application.batch_pipeline import BatchPipeline
from cli_downloader.core.batch import (
    AssetStatus,
    BatchEntry,
    BatchParent,
    DownloadAsset,
    DownloadPackage,
)
from cli_downloader.core.history_records import DownloadHistoryRecord, HistoryKind
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.task_tree import TaskKind
from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.state_batch_store import StateBatchStore
from cli_downloader.storage.state_database import StateDatabase
from cli_downloader.storage.state_history_store import StateHistoryStore
from cli_downloader.storage.state_task_store import StateTaskStore
from cli_downloader.storage.task_store import TaskRecord, TaskStatus


class StateBatchPipelineTest(TestCase):
    def test_pipeline_completes_through_unified_state_stores(self) -> None:
        async def scenario(directory: str) -> None:
            database = StateDatabase(Path(directory) / "state.sqlite3")
            task_store = StateTaskStore(database)
            task_store.upsert(TaskRecord("parent-1", TaskStatus.PENDING, kind=TaskKind.BATCH))
            store = StateBatchStore(database)
            entry = _entry()
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))
            completion = SQLiteCompletionStore(database)
            history = StateHistoryStore(database)
            history.upsert_record(
                DownloadHistoryRecord(
                    record_id="parent-1",
                    task_id="parent-1",
                    kind=HistoryKind.BATCH,
                    plugin_id="example_plugin",
                    site_id="example",
                    title="Example batch",
                )
            )
            pipeline = BatchPipeline(
                store=store,
                resolver=ImmediateResolver(),
                downloader=ImmediateDownloader(),
                completion_store=completion,
                history_store=history,
                child_id_factory=lambda _entry, _package: "child-1",
            )

            result = await pipeline.run_batch("parent-1", run_id="run-1")

            self.assertEqual(result.snapshot.execution_status.value, "completed")
            self.assertTrue(completion.is_completed("example", entry.source_url))
            self.assertEqual(store.get_child("child-1").status.value, "completed")
            self.assertEqual(store.list_assets("child-1")[0].status, AssetStatus.COMPLETED)
            self.assertTrue(history.list(task_id="parent-1"))
            child_record = history.get_record("child-1")
            self.assertIsNotNone(child_record)
            self.assertEqual(child_record.kind, HistoryKind.CHILD)
            self.assertEqual(child_record.parent_record_id, "parent-1")
            self.assertEqual(child_record.title, "gallery-1")
            self.assertEqual(child_record.source_url, entry.source_url)
            self.assertEqual(child_record.output_files, (str(Path("output/001.jpg")),))
            parent_record = history.get_record("parent-1")
            self.assertEqual(parent_record.status, "completed")
            self.assertEqual(parent_record.completed_files, 1)

        with TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))


class ImmediateResolver:
    async def resolve(self, entry, cancel_event, progress):
        return DownloadPackage(
            package_id="package-1",
            version=1,
            plugin_id=entry.plugin_id,
            site_id=entry.site_id,
            gallery_id="gallery-1",
            source_operation_id="resolve",
            source_url=entry.source_url,
            strategy=DownloadStrategy.DIRECT,
            assets=(
                DownloadAsset(
                    site_id=entry.site_id,
                    gallery_id="gallery-1",
                    plugin_asset_key="page-1",
                    url="https://cdn.example.test/1.jpg",
                    filename="001.jpg",
                    index=0,
                ),
            ),
        )


class ImmediateDownloader:
    async def execute(self, child, assets, cancel_event, progress):
        return tuple(
            replace(
                asset,
                status=AssetStatus.COMPLETED,
                final_path=f"output/{asset.asset.filename}",
                content_sha256="a" * 64,
            )
            for asset in assets
        )


def _entry() -> BatchEntry:
    return BatchEntry(
        entry_id="entry-1",
        parent_id="parent-1",
        plugin_id="example_plugin",
        site_id="example",
        candidate_id="gallery-1",
        source_operation_id="search",
        source_url="https://example.test/gallery/1",
    )
