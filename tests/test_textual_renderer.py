import unittest

from textual.widgets import Input as TextualInput
from textual.widgets import SelectionList

from cli_downloader.cli.textual_renderer import CorePageApp
from cli_downloader.core.view_session import ViewSession
from cli_downloader.core.widgets import (
    Action,
    ActionKind,
    Button,
    Input,
    ListItem,
    ListView,
    Page,
    Pagination,
    SelectionMode,
)


def _session(*, history=()):
    return ViewSession(
        session_id="session_1",
        plugin_id="example",
        request_id="request_1",
        navigation_history=history,
    )


class TextualRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyboard_input_and_focus_update_session(self):
        page = Page(
            content=(
                Input(widget_id="query", label="Query"),
                Button(
                    widget_id="submit",
                    text="Submit",
                    action=Action(action_id="submit", kind=ActionKind.CONFIRM),
                ),
            )
        )
        app = CorePageApp(page, _session())

        async with app.run_test() as pilot:
            field = app.query_one("#query", TextualInput)
            field.focus()
            await pilot.press("h", "i", "down")
            await pilot.pause()

            self.assertEqual(app.session.widget_values["query"], "hi")
            self.assertEqual(app.session.focused_widget_id, "submit")

    async def test_multiple_selection_updates_session(self):
        app = CorePageApp(
            Page(
                content=(
                    ListView(
                        widget_id="results",
                        selection_mode=SelectionMode.MULTIPLE,
                        items=(
                            ListItem(widget_id="first", title="First"),
                            ListItem(widget_id="second", title="Second"),
                        ),
                    ),
                )
            ),
            _session(),
        )

        async with app.run_test() as pilot:
            choices = app.query_one("#results", SelectionList)
            choices.focus()
            await pilot.press("space", "down", "space")
            await pilot.pause()

            self.assertEqual(app.session.selected_item_ids, ("first", "second"))

    async def test_single_selection_keeps_only_latest_item(self):
        app = CorePageApp(
            Page(
                content=(
                    ListView(
                        widget_id="results",
                        selection_mode=SelectionMode.SINGLE,
                        items=(
                            ListItem(widget_id="first", title="First"),
                            ListItem(widget_id="second", title="Second"),
                        ),
                    ),
                )
            ),
            _session(),
        )

        async with app.run_test() as pilot:
            choices = app.query_one("#results", SelectionList)
            choices.focus()
            await pilot.press("space", "down", "space")
            await pilot.pause()

            self.assertEqual(choices.selected, ["second"])
            self.assertEqual(app.session.selected_item_ids, ("second",))

    async def test_right_arrow_activates_next_page_action(self):
        next_action = Action(
            action_id="next_page",
            kind=ActionKind.INVOKE_OPERATION,
            operation_id="browse",
        )
        app = CorePageApp(
            Page(
                content=(
                    Pagination(
                        widget_id="pages",
                        current_page=1,
                        total_pages=3,
                        next_action=next_action,
                    ),
                )
            ),
            _session(),
        )

        async with app.run_test() as pilot:
            await pilot.press("right")

        self.assertEqual(app.return_value.action, next_action)
        self.assertEqual(app.return_value.session.current_page, 2)

    async def test_enter_activates_focused_core_action(self):
        confirm = Action(action_id="confirm", kind=ActionKind.CONFIRM)
        app = CorePageApp(
            Page(actions=(Button(widget_id="confirm", text="Confirm", action=confirm),)),
            _session(),
        )

        async with app.run_test() as pilot:
            app.query_one("#confirm").focus()
            await pilot.press("enter")

        self.assertEqual(app.return_value.action, confirm)

    async def test_escape_returns_to_previous_location_or_cancels(self):
        app = CorePageApp(Page(), _session(history=("previous",)))

        async with app.run_test() as pilot:
            await pilot.press("escape")

        self.assertEqual(app.return_value.back_location, "previous")
        self.assertFalse(app.return_value.cancelled)

        cancelled_app = CorePageApp(Page(), _session())
        async with cancelled_app.run_test() as pilot:
            await pilot.press("escape")

        self.assertTrue(cancelled_app.return_value.cancelled)


if __name__ == "__main__":
    unittest.main()
