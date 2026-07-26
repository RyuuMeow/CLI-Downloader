from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.core.models import Asset, AssetKind
from cli_downloader.core.errors import ConfigValidationError
from cli_downloader.storage.file_writer import AssetFileWriter


class AssetFileWriterTest(TestCase):
    def test_write_places_asset_under_task_directory(self) -> None:
        with TemporaryDirectory() as directory:
            writer = AssetFileWriter(Path(directory))
            asset = Asset(
                url="example-asset://item/cover.txt",
                kind=AssetKind.FILE,
                filename="cover.txt",
            )

            written = writer.write(asset, b"content", task_id="task-1")

            self.assertEqual(written.path, Path(directory) / "task-1" / "cover.txt")
            self.assertEqual(written.path.read_bytes(), b"content")
            self.assertEqual(written.bytes_written, 7)
            self.assertFalse(written.path.with_name("cover.txt.part").exists())

    def test_write_strips_path_from_filename(self) -> None:
        with TemporaryDirectory() as directory:
            writer = AssetFileWriter(Path(directory))
            asset = Asset(
                url="example-asset://item/cover.txt",
                kind=AssetKind.FILE,
                filename="../cover.txt",
            )

            written = writer.write(asset, b"content", task_id="task-1")

            self.assertEqual(written.path, Path(directory) / "task-1" / "cover.txt")

    def test_template_uses_safe_readable_gallery_fields(self) -> None:
        with TemporaryDirectory() as directory:
            writer = AssetFileWriter(
                Path(directory),
                folder_template="{site_id}/{gallery_id}-{title}",
            )
            asset = Asset(
                url="example-asset://item/cover.txt",
                kind=AssetKind.FILE,
                filename="cover.txt",
            )

            written = writer.write(
                asset,
                b"content",
                task_id="task-1",
                folder_fields={
                    "site_id": "example",
                    "gallery_id": "12345",
                    "title": 'A <Gallery>: "Title"',
                },
            )

            self.assertEqual(
                written.path.relative_to(directory),
                Path("example") / "12345-A _Gallery_ _Title_" / "cover.txt",
            )

    def test_template_rejects_absolute_traversal_and_unknown_fields(self) -> None:
        asset = Asset(
            url="example-asset://item/cover.txt",
            kind=AssetKind.FILE,
            filename="cover.txt",
        )
        with TemporaryDirectory() as directory:
            for template in ("C:/escape", "../escape", "{unknown}"):
                writer = AssetFileWriter(Path(directory), folder_template=template)
                with self.subTest(template=template), self.assertRaises(ConfigValidationError):
                    writer.paths(asset, task_id="task-1")

    def test_template_shares_folder_and_suffixes_only_duplicate_filenames(self) -> None:
        asset = Asset(
            url="example-asset://item/cover.txt",
            kind=AssetKind.FILE,
            filename="cover.txt",
        )
        fields = {"site_id": "example", "gallery_id": "same", "title": "Same"}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = AssetFileWriter(
                root,
                folder_template="{site_id}/{gallery_id}-{title}",
            ).write(asset, b"one", task_id="task-one", folder_fields=fields)
            second = AssetFileWriter(
                root,
                folder_template="{site_id}/{gallery_id}-{title}",
            ).write(asset, b"two", task_id="task-two", folder_fields=fields)

            self.assertEqual(first.path.parent, second.path.parent)
            self.assertEqual(first.path.name, "cover.txt")
            self.assertRegex(second.path.name, r"^cover-[0-9a-f]{8}\.txt$")
            self.assertEqual(first.path.read_bytes(), b"one")
            self.assertEqual(second.path.read_bytes(), b"two")

    def test_retry_reuses_the_same_owned_filename(self) -> None:
        asset = Asset(
            url="https://example.test/cover.txt?token=one",
            kind=AssetKind.FILE,
            filename="cover.txt",
            metadata={"asset_id": "cover"},
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = AssetFileWriter(root).write(asset, b"one", task_id="task-one")
            retried = AssetFileWriter(root).write(asset, b"two", task_id="task-one")

            self.assertEqual(retried.path, first.path)
            self.assertEqual(retried.path.read_bytes(), b"two")

    def test_task_template_accepts_only_supplied_plugin_fields(self) -> None:
        asset = Asset(
            url="example-asset://item/cover.txt",
            kind=AssetKind.FILE,
            filename="cover.txt",
        )
        with TemporaryDirectory() as directory:
            writer = AssetFileWriter(Path(directory))
            written = writer.write(
                asset,
                b"content",
                task_id="task-1",
                folder_template="{author_name}/{artwork_id}-{title}",
                folder_fields={
                    "author_name": "Artist",
                    "artwork_id": "123",
                    "title": "Work",
                },
            )

            self.assertEqual(
                written.path.relative_to(directory),
                Path("Artist") / "123-Work" / "cover.txt",
            )
