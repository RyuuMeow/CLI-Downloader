from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.storage.sqlite_completion_store import SQLiteCompletionStore
from cli_downloader.storage.state_database import StateDatabase


class SQLiteCompletionStoreTest(TestCase):
    def test_records_persist_idempotently_and_can_be_forgotten(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "state.sqlite3"
            store = SQLiteCompletionStore(StateDatabase(db_path))

            store.record_completed("example", "gallery-1")
            store.record_completed("example", "gallery-1")

            reopened = SQLiteCompletionStore(StateDatabase(db_path))
            self.assertTrue(reopened.is_completed("example", "gallery-1"))
            self.assertTrue(reopened.forget("example", "gallery-1"))
            self.assertFalse(reopened.is_completed("example", "gallery-1"))
            self.assertFalse(reopened.forget("example", "gallery-1"))
