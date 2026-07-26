import json
import unittest

from cli_downloader.core.errors import ContractValidationError
from cli_downloader.core.widgets import (
    MAX_WIDGET_COUNT,
    MAX_WIDGET_TREE_DEPTH,
    Action,
    ActionKind,
    Button,
    Container,
    EventBinding,
    EventKind,
    EventValue,
    Input,
    ListItem,
    ListView,
    MetadataView,
    Notice,
    Page,
    Pagination,
    Progress,
    Select,
    SelectionMode,
    SelectOption,
    TagList,
    Text,
    Title,
    Toggle,
    ValueReference,
    WidgetProperty,
)


class RendererWidget(Text):
    pass


def update_action(**changes: object) -> Action:
    values: dict[str, object] = {
        "action_id": "show_advanced",
        "kind": ActionKind.UPDATE_WIDGET,
        "target_id": "advanced",
        "property": WidgetProperty.VISIBLE,
        "value": EventValue(),
    }
    values.update(changes)
    return Action(**values)


class WidgetSerializationTests(unittest.TestCase):
    def test_complete_page_serializes_to_json(self) -> None:
        page = Page(
            header=(Title(text="Search results"), Notice(text="Offline fixture")),
            content=(
                Text(text="Choose galleries"),
                Progress(widget_id="loading", current=1, total=2, text="Loading"),
                ListView(
                    widget_id="results",
                    selection_mode=SelectionMode.MULTIPLE,
                    items=(
                        ListItem(
                            widget_id="gallery_1",
                            title="Gallery one",
                            selected=True,
                            data={"source_url": "https://example.test/gallery/1"},
                        ),
                    ),
                ),
                MetadataView(items={"Language": "English"}),
                TagList(tags=("tag-one", "tag-two")),
                Input(widget_id="query", label="Query", value="cats"),
                Toggle(
                    widget_id="filters_enabled",
                    label="Enable filters",
                    bindings=(EventBinding(event=EventKind.TOGGLE, actions=(update_action(),)),),
                ),
                Select(
                    widget_id="language",
                    label="Language",
                    options=(
                        SelectOption(value="all", text="All"),
                        SelectOption(value="english", text="English"),
                    ),
                    value="all",
                ),
                Container(widget_id="advanced", visible=False, children=(Text(text="Advanced"),)),
                Pagination(
                    current_page=1,
                    total_pages=2,
                    next_action=Action(
                        action_id="next_page",
                        kind=ActionKind.INVOKE_OPERATION,
                        operation_id="search",
                        params={"query": ValueReference("query")},
                    ),
                ),
            ),
            actions=(
                Button(
                    widget_id="download_selected",
                    text="Download selected",
                    action=Action(action_id="download", kind=ActionKind.DOWNLOAD),
                ),
            ),
        )

        payload = page.to_dict()
        self.assertEqual(payload["type"], "page")
        self.assertEqual(payload["content"][2]["type"], "list_view")
        self.assertEqual(payload["content"][2]["selection_mode"], "multiple")
        self.assertEqual(
            payload["content"][6]["bindings"][0]["actions"][0]["value"],
            {"source": "event"},
        )
        self.assertEqual(
            payload["content"][9]["next_action"]["params"]["query"]["widget_id"],
            "query",
        )
        json.dumps(payload)

    def test_widget_data_is_immutable(self) -> None:
        item = ListItem(widget_id="item", title="Item", data={"tags": ["one"]})

        with self.assertRaises(TypeError):
            item.data["new"] = True


class WidgetValidationTests(unittest.TestCase):
    def test_duplicate_widget_ids_are_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            Page(
                content=(
                    Input(widget_id="query", label="One"),
                    Input(widget_id="query", label="Two"),
                )
            )

        self.assertEqual(context.exception.code, "duplicate_widget_id")

    def test_unknown_update_target_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            Page(
                actions=(
                    Button(
                        widget_id="button", text="Update", action=update_action(target_id="missing")
                    ),
                )
            )

        self.assertEqual(context.exception.code, "unknown_widget_id")

    def test_duplicate_action_ids_are_rejected_across_page(self) -> None:
        action = Action(action_id="refresh", kind=ActionKind.REFRESH)

        with self.assertRaises(ContractValidationError) as context:
            Page(
                actions=(
                    Button(widget_id="first", text="First", action=action),
                    Button(widget_id="second", text="Second", action=action),
                )
            )

        self.assertEqual(context.exception.code, "duplicate_action_id")

    def test_unsupported_target_property_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            Page(
                content=(TagList(widget_id="tags", tags=("one",)),),
                actions=(
                    Button(
                        widget_id="button",
                        text="Update",
                        action=update_action(
                            target_id="tags",
                            property=WidgetProperty.VALUE,
                            value="new",
                        ),
                    ),
                ),
            )

        self.assertEqual(context.exception.code, "unsupported_widget_property")

    def test_unknown_value_reference_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            Page(
                actions=(
                    Button(
                        widget_id="search",
                        text="Search",
                        action=Action(
                            action_id="search",
                            kind=ActionKind.INVOKE_OPERATION,
                            operation_id="search",
                            params={"query": ValueReference("missing")},
                        ),
                    ),
                )
            )

        self.assertEqual(context.exception.code, "unknown_widget_id")

    def test_callbacks_are_rejected_from_action_params(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            Action(
                action_id="download", kind=ActionKind.DOWNLOAD, params={"callback": lambda: None}
            )

        self.assertEqual(context.exception.code, "non_json_value")
        self.assertEqual(context.exception.path, "action.params.callback")

    def test_renderer_widget_subclasses_are_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            Page(content=(RendererWidget(text="renderer-owned"),))

        self.assertEqual(context.exception.code, "invalid_widget")

    def test_invalid_selection_state_is_rejected(self) -> None:
        with self.assertRaises(ContractValidationError) as context:
            ListView(
                widget_id="results",
                selection_mode=SelectionMode.SINGLE,
                items=(
                    ListItem(widget_id="one", title="One", selected=True),
                    ListItem(widget_id="two", title="Two", selected=True),
                ),
            )

        self.assertEqual(context.exception.code, "invalid_selection")

    def test_tree_depth_limit_is_enforced(self) -> None:
        widget = Text(text="deep")
        for _ in range(MAX_WIDGET_TREE_DEPTH):
            widget = Container(children=(widget,))

        with self.assertRaises(ContractValidationError) as context:
            Page(content=(widget,))

        self.assertEqual(context.exception.code, "widget_depth_exceeded")

    def test_widget_count_limit_is_enforced(self) -> None:
        widgets = tuple(Text(text=f"item {index}") for index in range(MAX_WIDGET_COUNT))

        with self.assertRaises(ContractValidationError) as context:
            Page(content=widgets)

        self.assertEqual(context.exception.code, "widget_count_exceeded")


if __name__ == "__main__":
    unittest.main()
