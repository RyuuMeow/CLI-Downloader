from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.config.loader import ConfigLoader
from cli_downloader.core.environment import AppPaths


class ConfigLoaderTest(TestCase):
    def test_loads_defaults_when_files_are_missing(self) -> None:
        with TemporaryDirectory() as directory:
            paths = _paths(Path(directory))

            loaded = ConfigLoader(paths=paths).load()

        self.assertEqual(loaded.values["download.strategy"], "auto")
        self.assertEqual(loaded.values["interface.language"], "en-US")
        self.assertEqual(loaded.secrets, {})
        self.assertEqual(loaded.sources, ())

    def test_loads_config_and_redacts_secrets(self) -> None:
        with TemporaryDirectory() as directory:
            paths = _paths(Path(directory))
            paths.config_dir.mkdir(parents=True)
            (paths.config_dir / "config.toml").write_text(
                "\n".join(
                    (
                        "[download]",
                        'strategy = "direct"',
                        "[example.http]",
                        'user_agent = "Config Agent"',
                        "timeout_seconds = 45.0",
                        "retry_attempts = 3",
                    )
                ),
                encoding="utf-8",
            )
            (paths.config_dir / "secrets.toml").write_text(
                '[example]\ncookie = "secret"\n',
                encoding="utf-8",
            )

            loaded = ConfigLoader(paths=paths).load()

        self.assertEqual(loaded.values["download.strategy"], "direct")
        self.assertEqual(loaded.values["example.http.user_agent"], "Config Agent")
        self.assertEqual(loaded.values["example.http.timeout_seconds"], 45.0)
        self.assertEqual(loaded.values["example.http.retry_attempts"], 3)
        self.assertEqual(loaded.secrets["example.cookie"], "secret")
        self.assertEqual(loaded.to_dict()["secrets"]["example.cookie"], "<redacted>")


def _paths(root: Path) -> AppPaths:
    return AppPaths(
        mode="test",
        data_root=root,
        config_dir=root / "config",
        cache_dir=root / "cache",
        state_dir=root / "state",
        downloads_dir=root / "downloads",
        logs_dir=root / "logs",
    )
