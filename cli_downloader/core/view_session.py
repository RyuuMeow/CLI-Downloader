from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from cli_downloader.core.operations import (
    FrozenJsonValue,
    JsonValue,
    _error,
    _freeze_json,
    _freeze_json_object,
    _freeze_text_tuple,
    _optional_text,
    _require_identifier,
    _require_positive_int,
    _require_text,
    _thaw_json,
)


@dataclass(frozen=True)
class ViewSession:
    session_id: str
    plugin_id: str
    request_id: str
    current_page: int = 1
    focused_widget_id: str | None = None
    widget_values: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    selected_item_ids: tuple[str, ...] = ()
    navigation_history: tuple[str, ...] = ()
    operation_context: Mapping[str, FrozenJsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "session_id",
            _require_text(self.session_id, "session.session_id", maximum=128),
        )
        object.__setattr__(
            self,
            "plugin_id",
            _require_identifier(self.plugin_id, "session.plugin_id"),
        )
        object.__setattr__(
            self,
            "request_id",
            _require_text(self.request_id, "session.request_id", maximum=128),
        )
        object.__setattr__(
            self,
            "current_page",
            _require_positive_int(self.current_page, "session.current_page"),
        )
        focused = _optional_text(
            self.focused_widget_id,
            "session.focused_widget_id",
            maximum=128,
        )
        if focused is not None:
            focused = _require_identifier(focused, "session.focused_widget_id")
        object.__setattr__(self, "focused_widget_id", focused)

        values = _freeze_json_object(self.widget_values, "session.widget_values")
        for widget_id in values:
            _require_identifier(widget_id, "session.widget_values.key")
        object.__setattr__(self, "widget_values", values)
        object.__setattr__(
            self,
            "selected_item_ids",
            _freeze_text_tuple(
                self.selected_item_ids,
                "session.selected_item_ids",
                identifiers=True,
            ),
        )
        if not isinstance(self.navigation_history, Sequence) or isinstance(
            self.navigation_history,
            (str, bytes, bytearray),
        ):
            raise _error(
                "invalid_sequence",
                "session.navigation_history",
                "must be a sequence",
            )
        object.__setattr__(
            self,
            "navigation_history",
            tuple(
                _require_text(item, f"session.navigation_history[{index}]", maximum=8192)
                for index, item in enumerate(self.navigation_history)
            ),
        )
        object.__setattr__(
            self,
            "operation_context",
            _freeze_json_object(self.operation_context, "session.operation_context"),
        )

    def with_focus(self, widget_id: str | None) -> ViewSession:
        return replace(self, focused_widget_id=widget_id)

    def with_page(self, page: int) -> ViewSession:
        return replace(self, current_page=page)

    def with_widget_value(self, widget_id: str, value: object) -> ViewSession:
        widget_id = _require_identifier(widget_id, "session.widget_values.key")
        values = dict(self.widget_values)
        values[widget_id] = _freeze_json(value, f"session.widget_values.{widget_id}")
        return replace(self, widget_values=values)

    def with_selected_items(self, item_ids: Sequence[str]) -> ViewSession:
        selected = _freeze_text_tuple(
            item_ids,
            "session.selected_item_ids",
            identifiers=True,
        )
        return replace(self, selected_item_ids=selected)

    def push_navigation(self, location: str) -> ViewSession:
        return replace(self, navigation_history=(*self.navigation_history, location))

    def pop_navigation(self) -> tuple[ViewSession, str | None]:
        if not self.navigation_history:
            return self, None
        previous = self.navigation_history[-1]
        return replace(self, navigation_history=self.navigation_history[:-1]), previous

    def with_operation_context(self, **values: object) -> ViewSession:
        context = dict(self.operation_context)
        context.update(values)
        return replace(self, operation_context=context)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "session_id": self.session_id,
            "plugin_id": self.plugin_id,
            "request_id": self.request_id,
            "current_page": self.current_page,
            "focused_widget_id": self.focused_widget_id,
            "widget_values": _thaw_json(self.widget_values),
            "selected_item_ids": list(self.selected_item_ids),
            "navigation_history": list(self.navigation_history),
            "operation_context": _thaw_json(self.operation_context),
        }
