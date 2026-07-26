import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from cli_downloader.cli._shell_wiring import build_interactive_shell
from tests.example_site import build_example_runtime


class ShellWiringTest(TestCase):
    def test_build_wires_callbacks_and_lazily_reuses_task_service(self) -> None:
        runner = Mock(return_value=0)
        background_runner = Mock(return_value=0)
        operation_runner = Mock()

        with TemporaryDirectory() as directory:
            plugins_dir = Path(directory) / "plugins"
            with patch.dict(os.environ, {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                runtime = build_example_runtime()
                fake_shell = Mock()
                fake_shell.run.return_value = 17
                with patch(
                    "cli_downloader.cli._shell_wiring.InteractiveShell",
                    return_value=fake_shell,
                ) as shell_constructor:
                    exit_code = build_interactive_shell(
                        runtime=runtime,
                        plugins_dir=plugins_dir,
                        runner=runner,
                        background_runner=background_runner,
                        operation_runner=operation_runner,
                    )

        self.assertEqual(exit_code, 17)
        kwargs = shell_constructor.call_args.kwargs
        self.assertIs(kwargs["runner"], runner)
        self.assertIs(kwargs["operation_runner"], operation_runner)
        self.assertEqual(kwargs["plugins_dir"], plugins_dir)
        self.assertEqual(kwargs["startup_commands"], ())
        service_loader = kwargs["task_service_loader"]
        self.assertIs(service_loader(), service_loader())
        fake_shell.run.assert_called_once_with()
