from unittest import TestCase

from cli_downloader.cli.app import build_parser


class CliGrammarTest(TestCase):
    def test_version_and_doctor_options_are_root_commands(self) -> None:
        args = build_parser().parse_args(["doctor", "--json", "--network"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)
        self.assertTrue(args.network)

    def test_task_and_history_ids_are_positional(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["tasks", "show", "task-1"]).task_id, "task-1")
        self.assertEqual(
            parser.parse_args(["history", "show", "history-1"]).task_id,
            "history-1",
        )
        self.assertEqual(
            parser.parse_args(["history", "delete", "history-1"]).task_id,
            "history-1",
        )

    def test_legacy_task_id_flag_is_rejected(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["tasks", "show", "--task-id", "task-1"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["history", "--task-id", "history-1"])

    def test_download_response_modes_are_explicit_and_mutually_exclusive(self) -> None:
        parser = build_parser()
        detached = parser.parse_args(["download", "https://example.test/item", "--detach"])
        self.assertTrue(detached.detach)
        self.assertFalse(detached.wait)
        self.assertEqual(detached.selection_policy, "all")

        waiting = parser.parse_args(["download", "https://example.test/item", "--wait"])
        self.assertTrue(waiting.wait)
        self.assertFalse(waiting.detach)

        with self.assertRaises(SystemExit):
            parser.parse_args(["download", "https://example.test/item", "--detach", "--wait"])

    def test_task_pause_uses_positional_task_id(self) -> None:
        args = build_parser().parse_args(["tasks", "pause", "task-1", "--json"])
        self.assertEqual(args.task_id, "task-1")
        self.assertTrue(args.json)
