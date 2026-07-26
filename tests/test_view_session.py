import json
import unittest

from cli_downloader.core.errors import ContractValidationError
from cli_downloader.core.view_session import ViewSession


class ViewSessionTests(unittest.TestCase):
    def test_session_transitions_preserve_validated_state(self) -> None:
        session = ViewSession(
            session_id="session-1",
            plugin_id="sample",
            request_id="request-1",
            operation_context={"operation_id": "browse"},
        )

        session = (
            session.with_focus("search_input")
            .with_widget_value("search_input", "cats")
            .with_selected_items(("item_1", "item_2"))
            .with_page(2)
            .push_navigation("https://sample.test/search?page=1")
            .with_operation_context(page_token="next")
        )
        session, previous = session.pop_navigation()

        self.assertEqual(previous, "https://sample.test/search?page=1")
        self.assertEqual(session.focused_widget_id, "search_input")
        self.assertEqual(session.widget_values["search_input"], "cats")
        self.assertEqual(session.selected_item_ids, ("item_1", "item_2"))
        self.assertEqual(session.current_page, 2)
        self.assertEqual(session.operation_context["page_token"], "next")
        self.assertEqual(session.navigation_history, ())
        json.dumps(session.to_dict())

    def test_invalid_updates_do_not_modify_original_session(self) -> None:
        session = ViewSession(
            session_id="session-1",
            plugin_id="sample",
            request_id="request-1",
        )

        with self.assertRaises(ContractValidationError):
            session.with_widget_value("bad id", "value")

        self.assertEqual(session.widget_values, {})


if __name__ == "__main__":
    unittest.main()
