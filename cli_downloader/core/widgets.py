from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from cli_downloader.core.operations import (
    FrozenJsonValue,
    JsonValue,
    _error,
    _freeze_json,
    _freeze_json_object,
    _optional_text,
    _require_identifier,
    _require_non_negative_int,
    _require_positive_int,
    _require_text,
    _require_url,
    _thaw_json,
)


MAX_WIDGET_TREE_DEPTH = 16
MAX_WIDGET_COUNT = 1000


class SelectionMode(StrEnum):
    NONE = "none"
    SINGLE = "single"
    MULTIPLE = "multiple"


class EventKind(StrEnum):
    ACTIVATE = "activate"
    CHANGE = "change"
    TOGGLE = "toggle"
    SELECT = "select"
    SUBMIT = "submit"
    LOAD_MORE = "load_more"


class ActionKind(StrEnum):
    OPEN_URL = "open_url"
    DOWNLOAD = "download"
    INVOKE_OPERATION = "invoke_operation"
    UPDATE_WIDGET = "update_widget"
    NAVIGATE = "navigate"
    REFRESH = "refresh"
    COPY_TEXT = "copy_text"
    CANCEL = "cancel"
    CONFIRM = "confirm"


class WidgetProperty(StrEnum):
    VALUE = "value"
    VISIBLE = "visible"
    ENABLED = "enabled"
    SELECTED = "selected"
    ITEMS = "items"
    TEXT = "text"
    LOADING = "loading"


@dataclass(frozen=True)
class ValueReference:
    widget_id: str
    property: WidgetProperty = WidgetProperty.VALUE

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "widget_id", _require_identifier(self.widget_id, "value_ref.widget_id")
        )
        if not isinstance(self.property, WidgetProperty):
            raise _error(
                "invalid_widget_property", "value_ref.property", "must be a WidgetProperty"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {"source": "widget", "widget_id": self.widget_id, "property": self.property.value}


@dataclass(frozen=True)
class EventValue:
    def to_dict(self) -> dict[str, JsonValue]:
        return {"source": "event"}


ActionValue: TypeAlias = FrozenJsonValue | ValueReference | EventValue


def _freeze_action_value(value: object, path: str) -> ActionValue:
    if type(value) in {ValueReference, EventValue}:
        return value
    return _freeze_json(value, path)


def _serialize_action_value(value: ActionValue) -> JsonValue:
    if type(value) in {ValueReference, EventValue}:
        return value.to_dict()
    return _thaw_json(value)


@dataclass(frozen=True)
class Action:
    action_id: str
    kind: ActionKind
    target_id: str | None = None
    property: WidgetProperty | None = None
    value: ActionValue | None = None
    operation_id: str | None = None
    params: Mapping[str, ActionValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "action_id", _require_identifier(self.action_id, "action.action_id")
        )
        if not isinstance(self.kind, ActionKind):
            raise _error("invalid_action_kind", "action.kind", "must be an ActionKind")
        if self.target_id is not None:
            object.__setattr__(
                self, "target_id", _require_identifier(self.target_id, "action.target_id")
            )
        if self.property is not None and not isinstance(self.property, WidgetProperty):
            raise _error("invalid_widget_property", "action.property", "must be a WidgetProperty")
        if self.value is not None:
            object.__setattr__(self, "value", _freeze_action_value(self.value, "action.value"))
        if self.operation_id is not None:
            object.__setattr__(
                self, "operation_id", _require_identifier(self.operation_id, "action.operation_id")
            )
        if not isinstance(self.params, Mapping):
            raise _error("invalid_mapping", "action.params", "must be an object")
        params: dict[str, ActionValue] = {}
        for key, item in self.params.items():
            key = _require_identifier(key, "action.params.key")
            params[key] = _freeze_action_value(item, f"action.params.{key}")
        object.__setattr__(self, "params", MappingProxyType(params))

        if self.kind is ActionKind.UPDATE_WIDGET:
            if self.target_id is None or self.property is None or self.value is None:
                raise _error(
                    "incomplete_action",
                    "action",
                    "update_widget requires target_id, property, and value",
                )
        elif self.target_id is not None or self.property is not None:
            raise _error(
                "unexpected_action_field",
                "action",
                "target_id and property are only valid for update_widget",
            )
        if self.kind is ActionKind.INVOKE_OPERATION and self.operation_id is None:
            raise _error(
                "incomplete_action", "action.operation_id", "invoke_operation requires operation_id"
            )
        if self.kind is not ActionKind.INVOKE_OPERATION and self.operation_id is not None:
            raise _error(
                "unexpected_action_field",
                "action.operation_id",
                "is only valid for invoke_operation",
            )
        if self.kind is ActionKind.OPEN_URL and isinstance(self.value, str):
            object.__setattr__(self, "value", _require_url(self.value, "action.value"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action_id": self.action_id,
            "kind": self.kind.value,
            "target_id": self.target_id,
            "property": self.property.value if self.property is not None else None,
            "value": _serialize_action_value(self.value) if self.value is not None else None,
            "operation_id": self.operation_id,
            "params": {key: _serialize_action_value(value) for key, value in self.params.items()},
        }


def _freeze_actions(value: object, path: str) -> tuple[Action, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("invalid_sequence", path, "must be a sequence of Action")
    actions = tuple(value)
    if any(type(action) is not Action for action in actions):
        raise _error("invalid_action", path, "must contain only core Action objects")
    action_ids = tuple(action.action_id for action in actions)
    if len(set(action_ids)) != len(action_ids):
        raise _error("duplicate_action_id", path, "action ids must be unique")
    return actions


@dataclass(frozen=True)
class EventBinding:
    event: EventKind
    actions: tuple[Action, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.event, EventKind):
            raise _error("invalid_event_kind", "binding.event", "must be an EventKind")
        object.__setattr__(self, "actions", _freeze_actions(self.actions, "binding.actions"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"event": self.event.value, "actions": [action.to_dict() for action in self.actions]}


def _freeze_bindings(value: object, path: str) -> tuple[EventBinding, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("invalid_sequence", path, "must be a sequence of EventBinding")
    bindings = tuple(value)
    if any(type(binding) is not EventBinding for binding in bindings):
        raise _error("invalid_binding", path, "must contain only core EventBinding objects")
    events = tuple(binding.event for binding in bindings)
    if len(set(events)) != len(events):
        raise _error("duplicate_event_binding", path, "each event may be bound once per widget")
    return bindings


@dataclass(frozen=True, kw_only=True)
class Widget:
    widget_id: str | None = None
    visible: bool = True
    enabled: bool = True
    bindings: tuple[EventBinding, ...] = ()

    def __post_init__(self) -> None:
        if self.widget_id is not None:
            object.__setattr__(
                self, "widget_id", _require_identifier(self.widget_id, "widget.widget_id")
            )
        if not isinstance(self.visible, bool):
            raise _error("invalid_boolean", "widget.visible", "must be a boolean")
        if not isinstance(self.enabled, bool):
            raise _error("invalid_boolean", "widget.enabled", "must be a boolean")
        object.__setattr__(self, "bindings", _freeze_bindings(self.bindings, "widget.bindings"))

    def _to_dict(self, **values: JsonValue) -> dict[str, JsonValue]:
        widget_type = re.sub(r"(?<!^)(?=[A-Z])", "_", type(self).__name__).lower()
        return {
            "type": widget_type,
            "widget_id": self.widget_id,
            "visible": self.visible,
            "enabled": self.enabled,
            "bindings": [binding.to_dict() for binding in self.bindings],
            **values,
        }


@dataclass(frozen=True, kw_only=True)
class Title(Widget):
    text: str
    level: int = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "text", _require_text(self.text, "title.text"))
        if (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or not 1 <= self.level <= 6
        ):
            raise _error("invalid_heading_level", "title.level", "must be between 1 and 6")

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(text=self.text, level=self.level)


@dataclass(frozen=True, kw_only=True)
class Text(Widget):
    text: str

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "text", _require_text(self.text, "text.text"))

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(text=self.text)


@dataclass(frozen=True, kw_only=True)
class Notice(Widget):
    text: str
    severity: str = "info"

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "text", _require_text(self.text, "notice.text"))
        if self.severity not in {"info", "warning", "error"}:
            raise _error(
                "invalid_notice_severity", "notice.severity", "must be info, warning, or error"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(text=self.text, severity=self.severity)


@dataclass(frozen=True, kw_only=True)
class Progress(Widget):
    current: int
    total: int | None = None
    text: str | None = None
    loading: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self, "current", _require_non_negative_int(self.current, "progress_widget.current")
        )
        if self.total is not None:
            object.__setattr__(
                self, "total", _require_non_negative_int(self.total, "progress_widget.total")
            )
            if self.current > self.total:
                raise _error("invalid_progress", "progress_widget.current", "must not exceed total")
        object.__setattr__(self, "text", _optional_text(self.text, "progress_widget.text"))
        if not isinstance(self.loading, bool):
            raise _error("invalid_boolean", "progress_widget.loading", "must be a boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(
            current=self.current, total=self.total, text=self.text, loading=self.loading
        )


@dataclass(frozen=True, kw_only=True)
class ListItem(Widget):
    title: str
    text: str | None = None
    selected: bool = False
    data: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.widget_id is None:
            raise _error("missing_widget_id", "list_item.widget_id", "is required")
        object.__setattr__(self, "title", _require_text(self.title, "list_item_widget.title"))
        object.__setattr__(self, "text", _optional_text(self.text, "list_item_widget.text"))
        if not isinstance(self.selected, bool):
            raise _error("invalid_boolean", "list_item_widget.selected", "must be a boolean")
        object.__setattr__(self, "data", _freeze_json_object(self.data, "list_item_widget.data"))

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(
            title=self.title,
            text=self.text,
            selected=self.selected,
            data=_thaw_json(self.data),
        )


@dataclass(frozen=True, kw_only=True)
class ListView(Widget):
    items: tuple[ListItem, ...]
    selection_mode: SelectionMode = SelectionMode.NONE
    loading: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.widget_id is None and self.selection_mode is not SelectionMode.NONE:
            raise _error("missing_widget_id", "list_view.widget_id", "is required for selection")
        if not isinstance(self.items, Sequence) or isinstance(self.items, (str, bytes, bytearray)):
            raise _error("invalid_sequence", "list_view.items", "must be a sequence of ListItem")
        items = tuple(self.items)
        if any(type(item) is not ListItem for item in items):
            raise _error(
                "invalid_widget", "list_view.items", "must contain only core ListItem objects"
            )
        object.__setattr__(self, "items", items)
        if not isinstance(self.selection_mode, SelectionMode):
            raise _error(
                "invalid_selection_mode", "list_view.selection_mode", "must be a SelectionMode"
            )
        if self.selection_mode is SelectionMode.NONE and any(item.selected for item in items):
            raise _error("invalid_selection", "list_view.items", "selection is disabled")
        if self.selection_mode is SelectionMode.SINGLE and sum(item.selected for item in items) > 1:
            raise _error("invalid_selection", "list_view.items", "single selection allows one item")
        if not isinstance(self.loading, bool):
            raise _error("invalid_boolean", "list_view.loading", "must be a boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(
            items=[item.to_dict() for item in self.items],
            selection_mode=self.selection_mode.value,
            loading=self.loading,
        )


@dataclass(frozen=True, kw_only=True)
class MetadataView(Widget):
    items: Mapping[str, str]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.items, Mapping):
            raise _error("invalid_mapping", "metadata_view.items", "must be an object")
        items = {
            _require_text(key, "metadata_view.items.key", maximum=256): _require_text(
                value, f"metadata_view.items.{key}"
            )
            for key, value in self.items.items()
        }
        object.__setattr__(self, "items", MappingProxyType(items))

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(items=dict(self.items))


@dataclass(frozen=True, kw_only=True)
class TagList(Widget):
    tags: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.tags, Sequence) or isinstance(self.tags, (str, bytes, bytearray)):
            raise _error("invalid_sequence", "tag_list.tags", "must be a sequence of strings")
        tags = tuple(
            _require_text(tag, f"tag_list.tags[{index}]") for index, tag in enumerate(self.tags)
        )
        object.__setattr__(self, "tags", tags)

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(tags=list(self.tags))


@dataclass(frozen=True, kw_only=True)
class Input(Widget):
    label: str
    value: str = ""
    placeholder: str | None = None
    required: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.widget_id is None:
            raise _error("missing_widget_id", "input.widget_id", "is required")
        object.__setattr__(self, "label", _require_text(self.label, "input.label"))
        if not isinstance(self.value, str):
            raise _error("invalid_text", "input.value", "must be a string")
        object.__setattr__(
            self, "placeholder", _optional_text(self.placeholder, "input.placeholder")
        )
        if not isinstance(self.required, bool):
            raise _error("invalid_boolean", "input.required", "must be a boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(
            label=self.label, value=self.value, placeholder=self.placeholder, required=self.required
        )


@dataclass(frozen=True, kw_only=True)
class Toggle(Widget):
    label: str
    value: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.widget_id is None:
            raise _error("missing_widget_id", "toggle.widget_id", "is required")
        object.__setattr__(self, "label", _require_text(self.label, "toggle.label"))
        if not isinstance(self.value, bool):
            raise _error("invalid_boolean", "toggle.value", "must be a boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(label=self.label, value=self.value)


@dataclass(frozen=True)
class SelectOption:
    value: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _require_identifier(self.value, "select_option.value"))
        object.__setattr__(self, "text", _require_text(self.text, "select_option.text"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"value": self.value, "text": self.text}


@dataclass(frozen=True, kw_only=True)
class Select(Widget):
    label: str
    options: tuple[SelectOption, ...]
    value: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.widget_id is None:
            raise _error("missing_widget_id", "select.widget_id", "is required")
        object.__setattr__(self, "label", _require_text(self.label, "select.label"))
        if not isinstance(self.options, Sequence) or isinstance(
            self.options, (str, bytes, bytearray)
        ):
            raise _error("invalid_sequence", "select.options", "must be a sequence of SelectOption")
        options = tuple(self.options)
        if any(type(option) is not SelectOption for option in options):
            raise _error("invalid_select_option", "select.options", "must contain SelectOption")
        values = tuple(option.value for option in options)
        if len(set(values)) != len(values):
            raise _error("duplicate_value", "select.options", "option values must be unique")
        object.__setattr__(self, "options", options)
        if self.value is not None:
            value = _require_identifier(self.value, "select.value")
            if value not in values:
                raise _error("invalid_select_value", "select.value", "must match an option")
            object.__setattr__(self, "value", value)

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(
            label=self.label,
            options=[option.to_dict() for option in self.options],
            value=self.value,
        )


@dataclass(frozen=True, kw_only=True)
class Button(Widget):
    text: str
    action: Action

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.widget_id is None:
            raise _error("missing_widget_id", "button.widget_id", "is required")
        object.__setattr__(self, "text", _require_text(self.text, "button.text"))
        if type(self.action) is not Action:
            raise _error("invalid_action", "button.action", "must be a core Action")

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(text=self.text, action=self.action.to_dict())


@dataclass(frozen=True, kw_only=True)
class Pagination(Widget):
    current_page: int
    total_pages: int | None = None
    previous_action: Action | None = None
    next_action: Action | None = None
    loading: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(
            self,
            "current_page",
            _require_positive_int(self.current_page, "pagination.current_page"),
        )
        if self.total_pages is not None:
            object.__setattr__(
                self,
                "total_pages",
                _require_non_negative_int(self.total_pages, "pagination.total_pages"),
            )
            if self.total_pages > 0 and self.current_page > self.total_pages:
                raise _error(
                    "invalid_page_range", "pagination.current_page", "must not exceed total_pages"
                )
        for name in ("previous_action", "next_action"):
            action = getattr(self, name)
            if action is not None and type(action) is not Action:
                raise _error("invalid_action", f"pagination.{name}", "must be a core Action")
        if not isinstance(self.loading, bool):
            raise _error("invalid_boolean", "pagination.loading", "must be a boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(
            current_page=self.current_page,
            total_pages=self.total_pages,
            previous_action=self.previous_action.to_dict() if self.previous_action else None,
            next_action=self.next_action.to_dict() if self.next_action else None,
            loading=self.loading,
        )


def _freeze_widgets(value: object, path: str) -> tuple[Widget, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _error("invalid_sequence", path, "must be a sequence of core widgets")
    widgets = tuple(value)
    if any(not isinstance(widget, Widget) for widget in widgets):
        raise _error("invalid_widget", path, "must contain only core widgets")
    return widgets


@dataclass(frozen=True, kw_only=True)
class Container(Widget):
    children: tuple[Widget, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "children", _freeze_widgets(self.children, "container.children"))

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(children=[child.to_dict() for child in self.children])


_CORE_WIDGET_TYPES = {
    Title,
    Text,
    Notice,
    Progress,
    ListView,
    ListItem,
    MetadataView,
    TagList,
    Input,
    Toggle,
    Select,
    Button,
    Pagination,
    Container,
}

_SUPPORTED_PROPERTIES: dict[type[Widget], frozenset[WidgetProperty]] = {
    widget_type: frozenset({WidgetProperty.VISIBLE, WidgetProperty.ENABLED})
    for widget_type in _CORE_WIDGET_TYPES
}
_SUPPORTED_PROPERTIES.update(
    {
        Title: _SUPPORTED_PROPERTIES[Title] | {WidgetProperty.TEXT},
        Text: _SUPPORTED_PROPERTIES[Text] | {WidgetProperty.TEXT},
        Notice: _SUPPORTED_PROPERTIES[Notice] | {WidgetProperty.TEXT},
        Progress: _SUPPORTED_PROPERTIES[Progress]
        | {WidgetProperty.VALUE, WidgetProperty.TEXT, WidgetProperty.LOADING},
        ListView: _SUPPORTED_PROPERTIES[ListView]
        | {WidgetProperty.ITEMS, WidgetProperty.SELECTED, WidgetProperty.LOADING},
        ListItem: _SUPPORTED_PROPERTIES[ListItem] | {WidgetProperty.SELECTED, WidgetProperty.TEXT},
        Input: _SUPPORTED_PROPERTIES[Input] | {WidgetProperty.VALUE},
        Toggle: _SUPPORTED_PROPERTIES[Toggle] | {WidgetProperty.VALUE},
        Select: _SUPPORTED_PROPERTIES[Select] | {WidgetProperty.VALUE},
        Button: _SUPPORTED_PROPERTIES[Button] | {WidgetProperty.TEXT},
        Pagination: _SUPPORTED_PROPERTIES[Pagination] | {WidgetProperty.LOADING},
    }
)


def _children(widget: Widget) -> tuple[Widget, ...]:
    if type(widget) is Container:
        return widget.children
    if type(widget) is ListView:
        return widget.items
    return ()


def _widget_actions(widget: Widget) -> tuple[Action, ...]:
    actions = [action for binding in widget.bindings for action in binding.actions]
    if type(widget) is Button:
        actions.append(widget.action)
    elif type(widget) is Pagination:
        actions.extend(
            action for action in (widget.previous_action, widget.next_action) if action is not None
        )
    return tuple(actions)


def _action_references(action: Action) -> tuple[ValueReference, ...]:
    values = (action.value, *action.params.values())
    return tuple(value for value in values if type(value) is ValueReference)


@dataclass(frozen=True, kw_only=True)
class Page(Widget):
    header: tuple[Widget, ...] = ()
    content: tuple[Widget, ...] = ()
    actions: tuple[Widget, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "header", _freeze_widgets(self.header, "page.header"))
        object.__setattr__(self, "content", _freeze_widgets(self.content, "page.content"))
        object.__setattr__(self, "actions", _freeze_widgets(self.actions, "page.actions"))
        self._validate_tree()

    def _validate_tree(self) -> None:
        widgets: list[Widget] = []
        stack = [(widget, 2) for widget in reversed((*self.header, *self.content, *self.actions))]
        while stack:
            widget, depth = stack.pop()
            if type(widget) not in _CORE_WIDGET_TYPES:
                raise _error("invalid_widget", "page", "must contain only core widget classes")
            if depth > MAX_WIDGET_TREE_DEPTH:
                raise _error(
                    "widget_depth_exceeded",
                    "page",
                    f"must not exceed {MAX_WIDGET_TREE_DEPTH} levels",
                )
            widgets.append(widget)
            if len(widgets) + 1 > MAX_WIDGET_COUNT:
                raise _error(
                    "widget_count_exceeded", "page", f"must not exceed {MAX_WIDGET_COUNT} widgets"
                )
            stack.extend((child, depth + 1) for child in reversed(_children(widget)))

        by_id: dict[str, Widget] = {}
        for widget in widgets:
            if widget.widget_id is None:
                continue
            if widget.widget_id in by_id:
                raise _error(
                    "duplicate_widget_id", "page", f"duplicate widget id: {widget.widget_id}"
                )
            by_id[widget.widget_id] = widget

        action_ids: set[str] = set()
        for widget in (self, *widgets):
            for action in _widget_actions(widget):
                if action.action_id in action_ids:
                    raise _error(
                        "duplicate_action_id", "page", f"duplicate action id: {action.action_id}"
                    )
                action_ids.add(action.action_id)
                if action.kind is ActionKind.UPDATE_WIDGET:
                    self._validate_reference(
                        by_id, action.target_id, action.property, "action.target_id"
                    )
                for reference in _action_references(action):
                    self._validate_reference(
                        by_id, reference.widget_id, reference.property, "value_ref.widget_id"
                    )

    @staticmethod
    def _validate_reference(
        by_id: Mapping[str, Widget],
        widget_id: str | None,
        property: WidgetProperty | None,
        path: str,
    ) -> None:
        if widget_id not in by_id:
            raise _error(
                "unknown_widget_id", path, f"does not reference a widget in this page: {widget_id}"
            )
        widget = by_id[widget_id]
        if property not in _SUPPORTED_PROPERTIES[type(widget)]:
            raise _error(
                "unsupported_widget_property",
                path,
                f"{type(widget).__name__} does not support {property.value}",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return self._to_dict(
            header=[widget.to_dict() for widget in self.header],
            content=[widget.to_dict() for widget in self.content],
            actions=[widget.to_dict() for widget in self.actions],
        )
