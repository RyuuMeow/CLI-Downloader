from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tests.state_store_support import StateBatchStore

from cli_downloader.cli._download_ops import _batch_details, _selection_strategy
from cli_downloader.core.models import DownloadStrategy
from cli_downloader.core.operations import ListItemData
from cli_downloader.core.batch import BatchEntry, BatchParent


def _entry() -> BatchEntry:
    return BatchEntry(
        entry_id="entry-1",
        parent_id="parent-1",
        plugin_id="example_plugin",
        site_id="example",
        candidate_id="candidate-1",
        source_operation_id="search",
        source_url="https://example.test/gallery/1",
    )


class DownloadOpsTests(TestCase):
    def test_selection_strategy_reads_operation_metadata(self) -> None:
        items = (
            ListItemData(
                item_id="one",
                source_url="https://example.test/gallery/one",
                title="One",
                metadata={"strategy": "torrent"},
            ),
        )

        self.assertEqual(_selection_strategy(items), DownloadStrategy.TORRENT)

    def test_batch_details_projects_execution_graph(self) -> None:
        with TemporaryDirectory() as directory:
            store = StateBatchStore(Path(directory) / "state.sqlite3")
            entry = _entry()
            store.create_batch(BatchParent("parent-1", (entry.entry_id,)), (entry,))

            details = _batch_details(store, "parent-1")

            self.assertEqual(details["batch"]["execution_status"], "pending")
            self.assertEqual(details["entries"][0]["entry_id"], "entry-1")
            self.assertEqual(details["children"], [])
            self.assertEqual(details["attempts"], {})
