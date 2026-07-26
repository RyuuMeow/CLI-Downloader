from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from textual.widgets import SelectionList

from cli_downloader.application.operation_dispatcher import OperationDispatcher
from cli_downloader.cli.app import main
from cli_downloader.cli.rendering import RenderMode
from cli_downloader.cli.textual_renderer import CorePageApp, TextualRenderOutcome
from cli_downloader.core.operations import GalleryData, ListPageData, ResultKind
from cli_downloader.core.view_session import ViewSession
from cli_downloader.core.widgets import Action, ActionKind
from tests.example_site import build_example_runtime


class ExampleGenericOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime = build_example_runtime()
        self.dispatcher = OperationDispatcher(self.runtime)

    async def test_gallery_executes_through_generic_dispatcher(self) -> None:
        result = await self.dispatcher.dispatch(
            "https://example.test/gallery/welcome",
            request_id="request-1",
        )

        self.assertEqual(result.result_kind, ResultKind.GALLERY)
        self.assertIsInstance(result.data, GalleryData)
        self.assertEqual(result.data.gallery_id, "welcome")
        self.assertEqual(result.data.expected_asset_count, 2)

    async def test_list_page_contains_previews_selection_and_pagination(self) -> None:
        result = await self.dispatcher.dispatch(
            "https://example.test/list?page=1",
            request_id="request-1",
        )

        self.assertEqual(result.result_kind, ResultKind.LIST_PAGE)
        self.assertIsInstance(result.data, ListPageData)
        self.assertEqual(result.data.items[0].summary, "Offline preview for welcome")
        app = CorePageApp(
            result.page,
            ViewSession(
                session_id="session-1",
                plugin_id="example",
                request_id="request-1",
            ),
        )
        async with app.run_test() as pilot:
            choices = app.query_one("#results", SelectionList)
            choices.focus()
            await pilot.press("space")
            await pilot.press("down", "space")
            app.query_one("#download_selected").focus()
            await pilot.press("enter")

        self.assertEqual(
            app.return_value.session.selected_item_ids,
            ("welcome", "landscape"),
        )
        self.assertEqual(app.return_value.action.kind, ActionKind.DOWNLOAD)

        next_action = result.page.content[1].next_action
        next_result = await self.dispatcher.dispatch(
            "https://example.test/list?page=1",
            request_id="request-2",
            params=dict(next_action.params),
        )
        self.assertEqual(next_result.data.page_info.current_page, 2)
        self.assertEqual(next_result.data.items[0].item_id, "portrait")

        captured_page = await self.dispatcher.dispatch(
            "https://example.test/list?page=2",
            request_id="request-3",
        )
        self.assertEqual(captured_page.data.page_info.current_page, 2)


class ExampleGenericCLIFlowTests(unittest.TestCase):
    def test_list_download_action_reuses_persistent_download_pipeline(self) -> None:
        stream = StringIO()
        outcome = TextualRenderOutcome(
            session=ViewSession(
                session_id="session-1",
                plugin_id="example",
                request_id="request-1",
                selected_item_ids=("welcome", "landscape"),
            ),
            action=Action(action_id="download_selected", kind=ActionKind.DOWNLOAD),
        )

        with TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"CLI_DOWNLOADER_DATA_ROOT": directory}):
                with patch(
                    "cli_downloader.cli.app.select_render_mode",
                    return_value=RenderMode.INTERACTIVE,
                ):
                    with patch(
                        "cli_downloader.cli.app.render_interactive_result",
                        return_value=outcome,
                    ):
                        with redirect_stdout(stream):
                            exit_code = main(
                                ["download", "https://example.test/list?page=1"],
                                runtime=build_example_runtime(),
                            )
            payload = json.loads(stream.getvalue())
            self.assertTrue(all(Path(path).exists() for path in payload["output_files"]))
            self.assertTrue((Path(directory) / "state" / "state.sqlite3").exists())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["site_id"], "example")
        self.assertEqual(payload["batch"]["counts"]["completed"], 2)
        self.assertEqual(len(payload["output_files"]), 4)


if __name__ == "__main__":
    unittest.main()
