from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from cli_downloader.cli.app import _emit_hard_retry_error, _with_default_strategy, main
from cli_downloader.cli.i18n import TranslationService, translation_context
from cli_downloader.config.loader import LoadedConfig
from cli_downloader.core.models import DownloadStrategy
from tests.example_site import build_example_runtime


def _main(argv, **kwargs):
    return main(argv, runtime=build_example_runtime(), **kwargs)


class CLITest(TestCase):
    def test_human_cli_errors_follow_the_selected_locale(self) -> None:
        stream = StringIO()
        with translation_context(TranslationService("zh-TW")):
            with redirect_stdout(stream):
                _emit_hard_retry_error("task is not retryable", json_output=False)

        self.assertEqual(stream.getvalue().strip(), "錯誤: task is not retryable")

    def test_global_strategy_is_applied_and_explicit_value_wins(self) -> None:
        runtime = build_example_runtime()
        config = LoadedConfig(
            values={"download.strategy": "torrent"},
            secrets={},
            sources=(),
        )
        url = "plugin://example/single_download"

        self.assertEqual(
            _with_default_strategy(runtime, url, {}, config),
            {"strategy": DownloadStrategy.TORRENT.value},
        )
        self.assertEqual(
            _with_default_strategy(runtime, url, {"strategy": "direct"}, config),
            {"strategy": "direct"},
        )

    def test_sites_command_lists_example(self) -> None:
        stream = StringIO()

        with redirect_stdout(stream):
            exit_code = _main(["sites"])

        self.assertEqual(exit_code, 0)
        self.assertIn("example", stream.getvalue())

    def test_download_dry_run_reports_unsupported_url(self) -> None:
        stream = StringIO()

        with redirect_stdout(stream):
            exit_code = _main(["download", "https://unsupported.test/gallery/1", "--dry-run"])

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["matched"])
        self.assertEqual(payload["error_type"], "UnsupportedInputUrl")
        self.assertFalse(payload["network_request_performed"])

    def test_download_example_gallery_uses_generic_pipeline(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with redirect_stdout(stream):
                    exit_code = _main(
                        [
                            "download",
                            "https://example.test/gallery/welcome",
                            "--json",
                        ]
                    )
            payload = json.loads(stream.getvalue())
            output_files = [Path(path) for path in payload["output_files"]]
            self.assertTrue(all(path.exists() for path in output_files))
            self.assertTrue((Path(directory) / "state" / "state.sqlite3").exists())
            self.assertTrue((Path(directory) / "state" / "state.sqlite3").exists())
            self.assertTrue((Path(directory) / "state" / "state.sqlite3").exists())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["render_mode"], "json")
        self.assertEqual(payload["operation_result"]["result_kind"], "gallery")
        self.assertFalse(payload["network_request_performed"])

    def test_download_example_list_json_returns_interaction_required(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                    with redirect_stdout(stream):
                        exit_code = _main(
                            ["download", "https://example.test/list?page=1", "--json"]
                        )
            self.assertTrue((Path(directory) / "state" / "state.sqlite3").exists())
            self.assertFalse((Path(directory) / "state" / "batch.sqlite3").exists())

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["result_kind"], "interaction_required")
        self.assertEqual(payload["data"]["required_fields"], ["results"])
        self.assertEqual(payload["page"]["content"][0]["type"], "list_view")

    def test_download_example_list_json_confirms_selection_and_reports_duplicates(self) -> None:
        first_stream = StringIO()
        second_stream = StringIO()
        forced_stream = StringIO()
        progress: list[str] = []
        command = [
            "download",
            "https://example.test/list?page=1",
            "--json",
            "--select",
            "welcome",
            "--select",
            "landscape",
        ]

        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                    with redirect_stdout(first_stream):
                        first_exit = _main(
                            command,
                            progress=lambda reason, payload: progress.append(reason),
                        )
                    with redirect_stdout(second_stream):
                        second_exit = _main(command)
                    with redirect_stdout(forced_stream):
                        forced_exit = _main([*command, "--force"])

        first = json.loads(first_stream.getvalue())
        second = json.loads(second_stream.getvalue())
        forced = json.loads(forced_stream.getvalue())
        self.assertEqual(first_exit, 0)
        self.assertIn("batch_created", progress)
        self.assertIn("batch_started", progress)
        self.assertEqual(first["batch"]["counts"]["completed"], 2)
        self.assertEqual(len(first["output_files"]), 4)
        self.assertEqual(second_exit, 0)
        self.assertEqual(second["batch"]["counts"]["skipped"], 2)
        self.assertEqual(
            second["duplicates"],
            {"welcome": "completed", "landscape": "completed"},
        )
        self.assertEqual(forced_exit, 0)
        self.assertTrue(forced["batch_creation"]["forced"])
        self.assertEqual(forced["batch"]["counts"]["completed"], 2)

    def test_download_example_list_honors_background_cancellation(self) -> None:
        stream = StringIO()
        cancellation = Event()

        def cancel_created_batch(reason: str, payload: object) -> None:
            if reason == "batch_created":
                cancellation.set()

        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with redirect_stdout(stream):
                    exit_code = _main(
                        [
                            "download",
                            "https://example.test/list?page=1",
                            "--json",
                            "--select",
                            "welcome",
                        ],
                        cancellation=cancellation,
                        progress=cancel_created_batch,
                    )

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 130)
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["batch"]["execution_status"], "cancelled")

    def test_download_json_mode_is_explicit_and_non_interactive(self) -> None:
        stream = StringIO()

        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            with redirect_stdout(stream):
                exit_code = _main(
                    [
                        "download",
                        "https://example.test/gallery/welcome",
                        "--dry-run",
                        "--json",
                    ]
                )

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["render_mode"], "json")

    def test_config_defaults_command_prints_defaults(self) -> None:
        stream = StringIO()

        with redirect_stdout(stream):
            exit_code = _main(["config", "defaults"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["download.strategy"], "auto")

    def test_generic_download_prints_completed_task(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with redirect_stdout(stream):
                    exit_code = _main(
                        ["download", "https://example.test/gallery/welcome", "--json"]
                    )

        self.assertEqual(exit_code, 0)
        self.assertIn('"site_id": "example"', stream.getvalue())
        self.assertIn('"status": "completed"', stream.getvalue())
        self.assertIn('"environment_mode":', stream.getvalue())

    def test_plugins_command_without_subcommand_prints_help(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            exit_code = _main(["plugins"])

        self.assertEqual(exit_code, 0)
        self.assertIn("usage: clid plugins", stream.getvalue())

    def test_plugins_list_reports_valid_plugin_without_executing_plugin_code(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            _write_test_plugin(root / "sample", plugin_id="sample", site_id="sample")
            with redirect_stdout(stream):
                exit_code = _main(["plugins", "list", "--root", str(root)])

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["command"], "plugins list")
        self.assertFalse(payload["network_request_performed"])
        self.assertFalse(payload["external_plugin_code_executed"])
        self.assertEqual(payload["valid_count"], 1)
        self.assertEqual(payload["results"][0]["status"], "valid")
        self.assertEqual(payload["results"][0]["plugin_id"], "sample")
        self.assertEqual(payload["results"][0]["site_id"], "sample")

    def test_plugins_inspect_reports_invalid_and_ignored_plugin_folders(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            root.mkdir()
            (root / "notes").mkdir()
            invalid = root / "invalid"
            invalid.mkdir()
            _write_json(
                invalid / "plugin.json",
                _plugin_payload(plugin_id="invalid", manifest="missing.json"),
            )
            with redirect_stdout(stream):
                exit_code = _main(
                    [
                        "plugins",
                        "inspect",
                        "--root",
                        str(root),
                        "--include-ignored",
                    ]
                )

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["command"], "plugins inspect")
        self.assertFalse(payload["external_plugin_code_executed"])
        self.assertEqual(payload["valid_count"], 0)
        statuses = {Path(result["folder"]).name: result["status"] for result in payload["results"]}
        self.assertEqual(statuses["invalid"], "invalid")
        self.assertEqual(statuses["notes"], "ignored")

    def test_plugins_activate_refuses_without_external_code_gate(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            marker = Path(directory) / "activated.txt"
            _write_executable_test_plugin(
                root / "sample",
                plugin_id="sample",
                site_id="sample",
                marker=marker,
            )
            with redirect_stdout(stream):
                exit_code = _main(["plugins", "activate", "--root", str(root)])

            self.assertFalse(marker.exists())

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["command"], "plugins activate")
        self.assertEqual(payload["error_type"], "ExternalPluginCodeGateRequired")
        self.assertFalse(payload["external_plugin_code_executed"])

    def test_plugins_activate_executes_plugin_only_with_external_code_gate(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            marker = Path(directory) / "activated.txt"
            _write_executable_test_plugin(
                root / "sample",
                plugin_id="sample",
                site_id="sample",
                marker=marker,
            )
            with redirect_stdout(stream):
                exit_code = _main(
                    [
                        "plugins",
                        "activate",
                        "--root",
                        str(root),
                        "--enable-external-code",
                    ]
                )
            self.assertTrue(marker.exists())

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["command"], "plugins activate")
        self.assertTrue(payload["external_plugin_code_executed"])
        self.assertEqual(payload["activated_count"], 1)
        self.assertEqual(payload["registered_external_count"], 1)
        self.assertIn("sample", payload["registered_external_site_ids"])
        self.assertEqual(payload["results"][0]["status"], "activated")
        self.assertEqual(payload["results"][0]["plugin_id"], "sample")

    def test_plugins_activate_has_no_core_site_id_reservation(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            marker = Path(directory) / "activated.txt"
            _write_executable_test_plugin(
                root / "example_external",
                plugin_id="example_external",
                site_id="example",
                marker=marker,
            )
            with redirect_stdout(stream):
                exit_code = _main(
                    [
                        "plugins",
                        "activate",
                        "--root",
                        str(root),
                        "--enable-external-code",
                    ]
                )

            self.assertTrue(marker.exists())

        payload = json.loads(stream.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["activated_count"], 1)
        self.assertEqual(payload["registered_external_count"], 1)
        self.assertEqual(payload["results"][0]["status"], "activated")

    def test_sites_startup_can_explicitly_activate_external_plugins(self) -> None:
        stream = StringIO()

        with TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            marker = Path(directory) / "activated.txt"
            _write_executable_test_plugin(
                root / "sample",
                plugin_id="sample",
                site_id="sample",
                marker=marker,
            )
            with redirect_stdout(stream):
                exit_code = main(
                    [
                        "--plugin-root",
                        str(root),
                        "--enable-external-code",
                        "sites",
                    ]
                )
            self.assertTrue(marker.exists())

        self.assertEqual(exit_code, 0)
        self.assertIn("sample\tSample", stream.getvalue())

    def test_plugins_install_defaults_to_installed_plugins_root(self) -> None:
        with TemporaryDirectory() as directory:
            appdata_plugins = Path(directory) / "CLI-Downloader" / "plugins"
            with patch("cli_downloader.cli.app.installed_plugins_root", return_value=appdata_plugins):
                with patch("cli_downloader.cli.app.install_plugin", return_value="1.0.0") as mock_install:
                    stream = StringIO()
                    with redirect_stdout(stream):
                        exit_code = _main(["plugins", "install", "https://github.com/example/test.git", "--json"])
                    self.assertEqual(exit_code, 0)
                    mock_install.assert_called_once_with(appdata_plugins, "https://github.com/example/test.git", catalog_id=None)

    def test_plugins_install_honors_explicit_root(self) -> None:
        with TemporaryDirectory() as directory:
            custom_root = Path(directory) / "my_plugins"
            with patch("cli_downloader.cli.app.install_plugin", return_value="1.0.0") as mock_install:
                stream = StringIO()
                with redirect_stdout(stream):
                    exit_code = _main(["plugins", "install", "https://github.com/example/test.git", "--root", str(custom_root), "--json"])
                self.assertEqual(exit_code, 0)
                mock_install.assert_called_once_with(custom_root, "https://github.com/example/test.git", catalog_id=None)

    def test_tasks_lists_persisted_tasks(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    run_exit_code = _main(
                        ["download", "https://example.test/gallery/welcome", "--json"]
                    )

                tasks_stream = StringIO()
                with redirect_stdout(tasks_stream):
                    tasks_exit_code = _main(["tasks", "--json"])

            self.assertTrue((Path(directory) / "state" / "state.sqlite3").exists())

        self.assertEqual(run_exit_code, 0)
        self.assertEqual(tasks_exit_code, 0)
        self.assertIn('"status": "completed"', tasks_stream.getvalue())
        self.assertIn('"progress": "2/2"', tasks_stream.getvalue())
        self.assertNotIn('"metadata":', tasks_stream.getvalue())

    def test_tasks_can_filter_by_status(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])

                tasks_stream = StringIO()
                with redirect_stdout(tasks_stream):
                    exit_code = _main(["tasks", "--status", "completed", "--json"])

        self.assertEqual(exit_code, 0)
        tasks = json.loads(tasks_stream.getvalue())
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "completed")

    def test_tasks_returns_empty_list_when_store_is_missing(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                tasks_stream = StringIO()
                with redirect_stdout(tasks_stream):
                    exit_code = _main(["tasks"])

                self.assertFalse((Path(directory) / "state" / "tasks.sqlite3").exists())

        self.assertEqual(exit_code, 0)
        self.assertEqual(tasks_stream.getvalue().strip(), "No tasks.")

    def test_history_lists_persisted_records(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])

                history_stream = StringIO()
                with redirect_stdout(history_stream):
                    exit_code = _main(["history", "--json"])

                self.assertTrue((Path(directory) / "state" / "state.sqlite3").exists())

        self.assertEqual(exit_code, 0)
        self.assertIn('"kind": "item"', history_stream.getvalue())
        self.assertIn('"title": "Example Item: welcome"', history_stream.getvalue())

    def test_history_returns_empty_list_when_store_is_missing(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                history_stream = StringIO()
                with redirect_stdout(history_stream):
                    exit_code = _main(["history", "--json"])

                self.assertFalse((Path(directory) / "state" / "history.sqlite3").exists())

        self.assertEqual(exit_code, 0)
        self.assertEqual(history_stream.getvalue().strip(), "[]")

    def test_tasks_output_hides_internal_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])

                tasks_stream = StringIO()
                with redirect_stdout(tasks_stream):
                    exit_code = _main(["tasks", "--json"])

        self.assertEqual(exit_code, 0)
        self.assertNotIn('"metadata":', tasks_stream.getvalue())

    def test_tasks_retry_transitions_completed_task(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])
                task_id = _extract_task_id(run_stream.getvalue())

                retry_stream = StringIO()
                with redirect_stdout(retry_stream):
                    retry_exit_code = _main(["tasks", "retry", task_id])

                history_stream = StringIO()
                with redirect_stdout(history_stream):
                    history_exit_code = _main(["history", "show", task_id, "--json"])

        self.assertEqual(retry_exit_code, 0)
        self.assertIn('"event": "task_retry_requested"', retry_stream.getvalue())
        self.assertEqual(history_exit_code, 0)
        self.assertNotIn('"status": "running"', history_stream.getvalue())
        self.assertIn('"status": "completed"', history_stream.getvalue())
        self.assertIn('"completed_files": 2', history_stream.getvalue())

    def test_tasks_resume_rejects_completed_task(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])
                task_id = _extract_task_id(run_stream.getvalue())

                resume_stream = StringIO()
                with redirect_stdout(resume_stream):
                    exit_code = _main(["tasks", "resume", task_id])

        self.assertEqual(exit_code, 1)
        self.assertIn("cannot resume", resume_stream.getvalue())

    # ── history --json output ──

    def test_history_json_output_is_parseable(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])

                history_stream = StringIO()
                with redirect_stdout(history_stream):
                    exit_code = _main(["history", "--json"])

        self.assertEqual(exit_code, 0)
        data = json.loads(history_stream.getvalue())
        self.assertIsInstance(data, list)
        self.assertGreaterEqual(len(data), 1)
        self.assertIn("task_id", data[0])
        self.assertIn("kind", data[0])
        self.assertIn("title", data[0])
        self.assertIn("output_files", data[0])

    def test_history_json_task_id_filters_records(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])
                task_id = _extract_task_id(run_stream.getvalue())

                history_stream = StringIO()
                with redirect_stdout(history_stream):
                    exit_code = _main(["history", "show", task_id, "--json"])

        self.assertEqual(exit_code, 0)
        data = json.loads(history_stream.getvalue())
        self.assertIsInstance(data, list)
        for event in data:
            self.assertEqual(event["task_id"], task_id)

    def test_history_child_flag_lists_children(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                select_stream = StringIO()
                with redirect_stdout(select_stream):
                    _main(
                        [
                            "download",
                            "--json",
                            "https://example.test/list",
                            "--select",
                            "welcome",
                            "--select",
                            "landscape",
                        ]
                    )
                parent_id = _extract_parent_id(select_stream.getvalue())

                child_stream = StringIO()
                with redirect_stdout(child_stream):
                    exit_code = _main(["history", "show", parent_id, "--child"])

        self.assertEqual(exit_code, 0)
        output = child_stream.getvalue()
        self.assertIn("Total entries:", output)

    # ── tasks delete ──

    def test_tasks_delete_dry_run_previews_without_deleting(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])

                delete_stream = StringIO()
                with redirect_stdout(delete_stream):
                    exit_code = _main(["tasks", "delete", "--status", "completed", "--dry-run"])

        self.assertEqual(exit_code, 0)
        data = json.loads(delete_stream.getvalue())
        self.assertTrue(data["dry_run"])
        self.assertGreater(data["matched"], 0)

    def test_tasks_delete_removes_matching_tasks(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])

                delete_stream = StringIO()
                with redirect_stdout(delete_stream):
                    exit_code = _main(["tasks", "delete", "--status", "completed", "--yes"])

        self.assertEqual(exit_code, 0)
        data = json.loads(delete_stream.getvalue())
        self.assertEqual(data["deleted"], 1)

    def test_tasks_delete_blocked_active_status(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])
                task_id = _extract_task_id(run_stream.getvalue())
                with redirect_stdout(StringIO()):
                    _main(["tasks", "retry", task_id])

                delete_stream = StringIO()
                with redirect_stdout(delete_stream):
                    exit_code = _main(["tasks", "delete", "--status", "queued", "--yes"])

        self.assertEqual(exit_code, 2)
        self.assertIn("blocked_active", delete_stream.getvalue())

    def test_tasks_delete_requires_confirmation_without_yes(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                run_stream = StringIO()
                with redirect_stdout(run_stream):
                    _main(["download", "https://example.test/gallery/welcome", "--json"])

                delete_stream = StringIO()
                with redirect_stdout(delete_stream):
                    exit_code = _main(["tasks", "delete", "--older-than", "30"])

        self.assertEqual(exit_code, 2)
        self.assertIn("rerun with --yes", delete_stream.getvalue())

    # ── batches show ──

    def test_batches_show_dumps_batch_details(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                select_stream = StringIO()
                with redirect_stdout(select_stream):
                    _main(
                        [
                            "download",
                            "--json",
                            "https://example.test/list",
                            "--select",
                            "welcome",
                        ]
                    )
                parent_id = _extract_parent_id(select_stream.getvalue())

                show_stream = StringIO()
                with redirect_stdout(show_stream):
                    exit_code = _main(["batches", "show", parent_id])

        self.assertEqual(exit_code, 0)
        data = json.loads(show_stream.getvalue())
        self.assertIn("batch", data)
        self.assertIn("entries", data)

    def test_batches_show_reports_missing_parent(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            exit_code = _main(["batches", "show", "nonexistent-id"])

        self.assertEqual(exit_code, 1)
        self.assertIn("error", json.loads(stream.getvalue()))

    # ── tasks error paths ──

    def test_tasks_show_reports_missing_task(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                stream = StringIO()
                with redirect_stdout(stream):
                    exit_code = _main(["tasks", "show", "nonexistent-id"])

        self.assertEqual(exit_code, 1)
        self.assertIn("error", json.loads(stream.getvalue()))

    def test_tasks_retry_reports_missing_task(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            exit_code = _main(["tasks", "retry", "nonexistent-id"])

        self.assertEqual(exit_code, 1)
        self.assertIn("error", json.loads(stream.getvalue()))

    def test_tasks_pause_reports_missing_task(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            exit_code = _main(["tasks", "pause", "nonexistent-id"])

        self.assertEqual(exit_code, 1)
        self.assertIn("error", json.loads(stream.getvalue()))

    def test_plugins_remote_command(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(_main(["plugins", "remote", "--json"]), 0)
        payload = json.loads(stream.getvalue())
        self.assertIn("status", payload)
        self.assertIn("entries", payload)

    def test_plugins_refresh_command(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(_main(["plugins", "refresh", "--json"]), 0)
        payload = json.loads(stream.getvalue())
        self.assertIn("status", payload)
        self.assertIn("entries", payload)

    def test_plugins_versions_and_switch_command(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pdir = root / "testplugin"
            (pdir / "0.1.0").mkdir(parents=True)
            (pdir / "0.2.0").mkdir(parents=True)
            (pdir / "active").write_text("0.1.0", encoding="utf-8")
            (pdir / "0.1.0" / "plugin.json").write_text(
                json.dumps(
                    {
                        "plugin_id": "testplugin",
                        "display_name": "Test Plugin",
                        "version": "0.1.0",
                        "entrypoint": "plugin.py:Plugin",
                        "manifest": "site.json",
                    }
                ),
                encoding="utf-8",
            )
            (pdir / "0.2.0" / "plugin.json").write_text(
                json.dumps(
                    {
                        "plugin_id": "testplugin",
                        "display_name": "Test Plugin",
                        "version": "0.2.0",
                        "entrypoint": "plugin.py:Plugin",
                        "manifest": "site.json",
                    }
                ),
                encoding="utf-8",
            )

            # test versions
            stream = StringIO()
            with redirect_stdout(stream):
                self.assertEqual(
                    _main(["plugins", "versions", "testplugin", "--root", str(root), "--json"]), 0
                )
            payload = json.loads(stream.getvalue())
            self.assertEqual(payload["plugin_id"], "testplugin")
            self.assertEqual(len(payload["versions"]), 2)

            # test switch
            stream2 = StringIO()
            with redirect_stdout(stream2):
                self.assertEqual(
                    _main(
                        ["plugins", "switch", "testplugin", "0.2.0", "--root", str(root), "--json"]
                    ),
                    0,
                )
            payload2 = json.loads(stream2.getvalue())
            self.assertEqual(payload2["status"], "switched")
            self.assertEqual((pdir / "active").read_text(encoding="utf-8").strip(), "0.2.0")

    def test_check_update_command(self) -> None:
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(_main(["check-update", "--json"]), 0)
        payload = json.loads(stream.getvalue())
        self.assertIn("status", payload)
        self.assertIn("current_version", payload)


def _extract_task_id(payload: str) -> str:
    import json

    return json.loads(payload)["task_id"]


def _extract_parent_id(payload: str) -> str:
    import json

    return json.loads(payload)["batch"]["parent"]["parent_id"]


def _write_test_plugin(plugin_folder: Path, *, plugin_id: str, site_id: str) -> None:
    plugin_folder.mkdir(parents=True)
    (plugin_folder / "manifests").mkdir()
    _write_json(
        plugin_folder / "plugin.json",
        _plugin_payload(plugin_id=plugin_id, manifest="site.json"),
    )
    _write_json(
        plugin_folder / "manifests" / "site.json",
        _manifest_payload(site_id=site_id),
    )
    (plugin_folder / "plugin.py").write_text(
        "raise RuntimeError('plugin code should not execute')\n",
        encoding="utf-8",
    )


def _write_executable_test_plugin(
    plugin_folder: Path,
    *,
    plugin_id: str,
    site_id: str,
    marker: Path,
) -> None:
    plugin_folder.mkdir(parents=True)
    (plugin_folder / "manifests").mkdir()
    _write_json(
        plugin_folder / "plugin.json",
        _plugin_payload(plugin_id=plugin_id, manifest="site.json"),
    )
    _write_json(
        plugin_folder / "manifests" / "site.json",
        _manifest_payload(site_id=site_id),
    )
    (plugin_folder / "plugin.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from cli_downloader.sites.manifest import site_manifest_from_json_file",
                f"Path(r'{marker}').write_text('activated', encoding='utf-8')",
                "class Plugin:",
                f"    site_id = {site_id!r}",
                f"    display_name = {site_id.title()!r}",
                "    def can_handle_url(self, url):",
                f"        return url.startswith('https://{site_id}.test/')",
                "    def classify_url(self, url):",
                "        return None",
                "    async def resolve(self, request):",
                "        raise NotImplementedError",
                "    async def list_assets(self, item):",
                "        raise NotImplementedError",
                "    async def choose_strategy(self, item):",
                "        raise NotImplementedError",
                "    async def execute(self, request):",
                "        raise NotImplementedError",
                "    def get_manifest_name(self):",
                "        return 'site.json'",
                "    def get_manifest(self):",
                "        return site_manifest_from_json_file(Path(__file__).parent / 'manifests' / 'site.json')",
                "",
            )
        ),
        encoding="utf-8",
    )


def _plugin_payload(*, plugin_id: str, manifest: str) -> dict[str, object]:
    return {
        "plugin_id": plugin_id,
        "display_name": plugin_id.title(),
        "version": "0.1.0",
        "entrypoint": "plugin.py:Plugin",
        "manifest": manifest,
    }


def _manifest_payload(*, site_id: str) -> dict[str, object]:
    return {
        "site_id": site_id,
        "display_name": site_id.title(),
        "domains": [f"{site_id}.test"],
        "url_rules": [
            {
                "name": "gallery",
                "pattern": rf"^https://{site_id}\.test/g/(?P<gallery_id>\d+)$",
                "input_kind": "single",
                "operation": "single_download",
            }
        ],
        "operations": {
            "single_download": {
                "params": {
                    "strategy": {
                        "type": "string",
                        "default": "auto",
                    }
                }
            }
        },
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
