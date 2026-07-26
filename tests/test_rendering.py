import json
import unittest
from unittest.mock import patch

from cli_downloader.cli.rendering import (
    RenderMode,
    prepare_result_for_mode,
    select_render_mode,
    serialize_operation_result,
)
from cli_downloader.core.environment import TerminalCapabilities
from cli_downloader.core.operations import (
    InteractionRequiredData,
    MessageData,
    OperationResult,
    ResultKind,
)
from cli_downloader.core.widgets import (
    Action,
    ActionKind,
    Button,
    Input,
    ListItem,
    ListView,
    Page,
    SelectionMode,
)


def _terminal(*, interactive: bool) -> TerminalCapabilities:
    return TerminalCapabilities(
        is_tty=interactive,
        color=interactive,
        hyperlink=False,
        interactive=interactive,
    )


def _interactive_result() -> OperationResult:
    return OperationResult(
        request_id="request-1",
        result_kind=ResultKind.MESSAGE,
        data=MessageData(message="Choose"),
        page=Page(
            content=(
                Input(widget_id="keyword", label="Keyword", required=True),
                ListView(
                    widget_id="results",
                    items=(ListItem(widget_id="item_1", title="One"),),
                    selection_mode=SelectionMode.MULTIPLE,
                ),
            ),
            actions=(
                Button(
                    widget_id="confirm",
                    text="Confirm",
                    action=Action(action_id="confirm_selection", kind=ActionKind.CONFIRM),
                ),
            ),
        ),
    )


class RenderingTests(unittest.TestCase):
    def test_json_flag_and_non_tty_select_non_interactive_modes(self) -> None:
        self.assertEqual(
            select_render_mode(json_requested=True, terminal=_terminal(interactive=True)),
            RenderMode.JSON,
        )
        self.assertEqual(
            select_render_mode(terminal=_terminal(interactive=False)),
            RenderMode.PLAIN,
        )
        self.assertEqual(
            select_render_mode(terminal=_terminal(interactive=True)),
            RenderMode.INTERACTIVE,
        )

    def test_non_interactive_mode_returns_structured_interaction_required(self) -> None:
        result = prepare_result_for_mode(_interactive_result(), RenderMode.JSON)

        self.assertEqual(result.result_kind, ResultKind.INTERACTION_REQUIRED)
        self.assertEqual(result.data.required_fields, ("keyword", "results"))
        self.assertEqual(result.data.required_confirmations, ())
        self.assertEqual(result.available_actions, ("confirm_selection",))

    def test_json_and_plain_serialization_never_prompt(self) -> None:
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            compact = serialize_operation_result(_interactive_result(), RenderMode.JSON)
            readable = serialize_operation_result(_interactive_result(), RenderMode.PLAIN)

        self.assertEqual(json.loads(compact)["result_kind"], "interaction_required")
        self.assertEqual(json.loads(readable)["result_kind"], "interaction_required")
        self.assertNotIn("\n", compact)
        self.assertIn("\n", readable)

    def test_interactive_mode_preserves_original_result(self) -> None:
        result = _interactive_result()

        self.assertIs(prepare_result_for_mode(result, RenderMode.INTERACTIVE), result)
        with self.assertRaises(ValueError):
            serialize_operation_result(result, RenderMode.INTERACTIVE)

    def test_explicit_interaction_result_is_not_rewritten(self) -> None:
        result = OperationResult(
            request_id="request-1",
            result_kind=ResultKind.INTERACTION_REQUIRED,
            data=InteractionRequiredData(
                message="Confirm network access",
                required_confirmations=("live_network",),
            ),
            page=_interactive_result().page,
        )

        self.assertIs(prepare_result_for_mode(result, RenderMode.JSON), result)


if __name__ == "__main__":
    unittest.main()
