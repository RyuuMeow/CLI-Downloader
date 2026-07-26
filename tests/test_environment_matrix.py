import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cli_downloader.application.download_service import DownloadService
from cli_downloader.core.environment import application_root, installed_plugins_root
from cli_downloader.sites.registry import SiteRegistry
from tests.example_site import ExampleAdapter, ExampleAssetFetcher
from cli_downloader.storage.file_writer import AssetFileWriter
from cli_downloader.storage.paths import StorageLayout
from cli_downloader.storage.task_store import InMemoryTaskStore, TaskStatus
from tests.support import make_environment


class EnvironmentMatrixTest(TestCase):
    def test_source_application_root_is_repository_root(self) -> None:
        self.assertEqual(application_root(), Path(__file__).resolve().parents[1])

    def test_frozen_application_root_is_executable_directory(self) -> None:
        executable = Path("C:/Program Files/CLI-Downloader/clid.exe")
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(executable)),
        ):
            self.assertEqual(application_root(), executable.parent)

    def test_installed_plugins_are_owned_by_local_app_data(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}):
            self.assertEqual(
                installed_plugins_root(),
                Path("C:/Users/test/AppData/Local/CLI-Downloader/plugins"),
            )

    def test_example_download_uses_injected_environment_matrix(self) -> None:
        scenarios = (
            ("user", False, "default"),
            ("portable", True, "manga"),
        )

        with TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (mode, interactive, profile) in enumerate(scenarios):
                with self.subTest(mode=mode, interactive=interactive, profile=profile):
                    environment = make_environment(
                        root / mode,
                        mode=mode,
                        interactive=interactive,
                    )
                    layout = StorageLayout.from_app_paths(environment.paths)
                    store = InMemoryTaskStore()
                    service = DownloadService(
                        registry=_registry(),
                        task_store=store,
                        environment=environment,
                        task_id_factory=lambda index=index: f"task-{index}",
                    )

                    result = asyncio.run(
                        service.download_task(
                            "example://gallery/welcome",
                            fetcher=ExampleAssetFetcher(),
                            file_writer=AssetFileWriter(layout.downloads_dir),
                            profile=profile,
                        )
                    )

                    self.assertEqual(result.record.status, TaskStatus.COMPLETED)
                    self.assertEqual(result.record.metadata["environment_mode"], mode)
                    self.assertEqual(result.record.metadata["terminal_interactive"], interactive)
                    self.assertEqual(result.record.metadata["profile"], profile)
                    self.assertEqual(result.item.metadata["profile"], profile)
                    for written_asset in result.written_assets:
                        self.assertTrue(written_asset.path.is_relative_to(layout.downloads_dir))
                        self.assertTrue(written_asset.path.exists())


def _registry() -> SiteRegistry:
    registry = SiteRegistry()
    registry.register(ExampleAdapter())
    return registry
