from __future__ import annotations

import json
from enum import StrEnum

from cli_downloader.core.environment import TerminalCapabilities
from cli_downloader.core.operations import (
    InteractionRequiredData,
    OperationResult,
    ResultKind,
)
from cli_downloader.core.widgets import (
    Action,
    ActionKind,
    Button,
    Container,
    Input,
    ListView,
    Page,
    Pagination,
    Select,
    SelectionMode,
    Widget,
)


class RenderMode(StrEnum):
    INTERACTIVE = "interactive"
    JSON = "json"
    PLAIN = "plain"


def select_render_mode(
    *,
    json_requested: bool = False,
    terminal: TerminalCapabilities | None = None,
) -> RenderMode:
    if json_requested:
        return RenderMode.JSON
    capabilities = terminal or TerminalCapabilities.detect()
    return RenderMode.INTERACTIVE if capabilities.interactive else RenderMode.PLAIN


def prepare_result_for_mode(result: OperationResult, mode: RenderMode) -> OperationResult:
    if (
        mode is RenderMode.INTERACTIVE
        or result.page is None
        or result.result_kind is ResultKind.INTERACTION_REQUIRED
    ):
        return result
    required_fields, confirmation_actions = _interaction_requirements(result.page)
    if not required_fields and not confirmation_actions:
        return result
    available_actions = tuple(dict.fromkeys((*result.available_actions, *confirmation_actions)))
    return OperationResult(
        request_id=result.request_id,
        result_kind=ResultKind.INTERACTION_REQUIRED,
        data=InteractionRequiredData(
            message="Additional interaction is required",
            required_fields=required_fields,
        ),
        page=result.page,
        available_actions=available_actions,
        continuation=result.continuation,
        diagnostics=result.diagnostics,
    )


def serialize_operation_result(result: OperationResult, mode: RenderMode) -> str:
    if mode is RenderMode.INTERACTIVE:
        raise ValueError("interactive results require the Textual renderer")
    prepared = prepare_result_for_mode(result, mode)
    return json.dumps(
        prepared.to_dict(),
        ensure_ascii=True,
        indent=None if mode is RenderMode.JSON else 2,
    )


def _interaction_requirements(page: Page) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required_fields: list[str] = []
    confirmation_actions: list[str] = []
    for widget in _walk_widgets(page):
        if not widget.visible or not widget.enabled:
            continue
        if type(widget) is Input and widget.required and not widget.value:
            required_fields.append(widget.widget_id)
        elif type(widget) is Select and widget.value is None:
            required_fields.append(widget.widget_id)
        elif (
            type(widget) is ListView
            and widget.selection_mode is not SelectionMode.NONE
            and not any(item.selected for item in widget.items)
        ):
            required_fields.append(widget.widget_id)
        confirmation_actions.extend(
            action.action_id
            for action in _widget_actions(widget)
            if action.kind is ActionKind.CONFIRM
        )
    return tuple(required_fields), tuple(dict.fromkeys(confirmation_actions))


def _walk_widgets(page: Page) -> tuple[Widget, ...]:
    widgets: list[Widget] = [page]
    stack = list(reversed((*page.header, *page.content, *page.actions)))
    while stack:
        widget = stack.pop()
        widgets.append(widget)
        if type(widget) is Container:
            stack.extend(reversed(widget.children))
        elif type(widget) is ListView:
            stack.extend(reversed(widget.items))
    return tuple(widgets)


def _widget_actions(widget: Widget) -> tuple[Action, ...]:
    actions = [action for binding in widget.bindings for action in binding.actions]
    if type(widget) is Button:
        actions.append(widget.action)
    elif type(widget) is Pagination:
        actions.extend(
            action for action in (widget.previous_action, widget.next_action) if action is not None
        )
    return tuple(actions)
