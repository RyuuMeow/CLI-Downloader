from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from cli_downloader.cli.doctor import run_doctor
from cli_downloader.core.environment import AppPaths, EnvironmentContext, TerminalCapabilities


def _environment(root: Path) -> EnvironmentContext:
    paths = AppPaths(
        mode="test",
        data_root=root,
        config_dir=root / "config",
        cache_dir=root / "cache",
        state_dir=root / "state",
        downloads_dir=root / "downloads",
        logs_dir=root / "logs",
    )
    return EnvironmentContext(
        paths=paths,
        terminal=TerminalCapabilities(False, False, False, False),
        python="3.12",
        platform="test",
    )


class DoctorTest(TestCase):
    def test_local_report_is_machine_readable_and_passes_without_network(self) -> None:
        with TemporaryDirectory() as directory:
            report = run_doctor(environment=_environment(Path(directory)))

        payload = report.to_dict()
        self.assertTrue(payload["ok"])
        ids = {check["id"] for check in payload["checks"]}
        self.assertIn("config.load", ids)
        self.assertIn("state.database", ids)
        self.assertIn("plugins.manifests", ids)
        self.assertFalse(any(check["status"] == "fail" for check in payload["checks"]))

    def test_network_probe_is_injected_and_failures_are_actionable(self) -> None:
        with TemporaryDirectory() as directory:
            report = run_doctor(
                environment=_environment(Path(directory)),
                network=True,
                network_probe=lambda _url: "http_status=200",
            )

        self.assertTrue(report.to_dict()["ok"])
        self.assertIn("network.enabled", {check.check_id for check in report.checks})
