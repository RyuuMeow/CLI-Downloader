from unittest import TestCase

from cli_downloader.cli.table import TableColumn, TableSpec, build_rich_table, render_table_text


class SharedTableTest(TestCase):
    def test_render_keeps_long_cells_intact_for_captured_output(self) -> None:
        spec = TableSpec(
            columns=(TableColumn("ID"), TableColumn("Title")),
            rows=(("task-1", "A" * 40),),
        )

        output = render_table_text(spec)

        self.assertIn("task-1", output)
        self.assertIn("A" * 40, output)

    def test_column_overflow_policy_is_shared(self) -> None:
        table = build_rich_table(TableSpec(columns=(TableColumn("Title"),), rows=(("value",),)))

        self.assertTrue(table.columns[0].no_wrap)
        self.assertEqual(table.columns[0].overflow, "ellipsis")
