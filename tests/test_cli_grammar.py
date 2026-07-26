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
