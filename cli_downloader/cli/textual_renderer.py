from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rich.segment import Segment
from rich.style import Style
from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.strip import Strip
from textual.widgets import (
    Button as TextualButton,
    Input as TextualInput,
    Label,
    ListItem as TextualListItem,
    ListView as TextualListView,
    ProgressBar,
    Select as TextualSelect,
    SelectionList,
    Static,
    Switch,
)
from textual.widgets.selection_list import Selection

from cli_downloader.core.operations import OperationResult
from cli_downloader.core.view_session import ViewSession
from cli_downloader.core.widgets import (
    Action,
    Button,
    Container,
    Input,
    ListView,
    MetadataView,
    Notice,
    Page,
    Pagination,
    Progress,
    Select,
    SelectionMode,
    TagList,
    Text,
    Title,
    Toggle,
    Widget,
)
from cli_downloader.cli.presentation import CLI_DOWNLOADER_THEME, CORE_APP_CSS


@dataclass(frozen=True)
class TextualRenderOutcome:
    session: ViewSession
    action: Action | None = None
    cancelled: bool = False
    back_location: str | None = None


class AsciiSelectionList(SelectionList):
    """SelectionList with a color-independent terminal checkbox."""

    def _get_left_gutter_width(self) -> int:
        return 6

    def render_line(self, y: int) -> Strip:
        line = super().render_line(y)
        _, scroll_y = self.scroll_offset
        index = scroll_y + y
        if index >= self.option_count:
            return line
        option = self.get_option_at_index(index)
        selected = option.value in self.selected
        underlying = next(iter(line)).style or self.rich_style
        assert underlying is not None
        marker = Style(
            color="#e58a32" if selected else "#999999",
            meta={"option": index},
        )
        return Strip(
            [
                Segment("[ ", style=underlying + marker),
                Segment("x" if selected else " ", style=underlying + marker),
                Segment(" ] ", style=underlying + marker),
                *list(line)[4:],
            ]
        )


class CorePageView(Vertical):
    class SelectionChanged(Message):
        def __init__(
            self,
            session: ViewSession,
            *,
            single_selection: bool,
        ) -> None:
            super().__init__()
            self.session = session
            self.single_selection = single_selection

    class ActionSelected(Message):
        def __init__(self, action: Action, session: ViewSession) -> None:
            super().__init__()
            self.action = action
            self.session = session

    class Cancelled(Message):
        def __init__(self, session: ViewSession) -> None:
            super().__init__()
            self.session = session

    class BackRequested(Message):
        def __init__(self, location: str, session: ViewSession) -> None:
            super().__init__()
            self.location = location
            self.session = session

    BINDINGS = [
        Binding("up", "focus_previous", "Previous", show=False),
        Binding("down", "focus_next", "Next", show=False),
        Binding("escape", "back_or_cancel", "Back", show=False),
        Binding("ctrl+c", "cancel", "Cancel", show=False),
        Binding("left", "previous_page", "Previous page", show=False),
        Binding("right", "next_page", "Next page", show=False),
    ]

    def __init__(self, page: Page, session: ViewSession, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.page = page
        self.session = session
        self._actions: dict[str, Action] = {}
        self._pagination: Pagination | None = None
        self._single_selection_ids: set[str] = set()
        self._generated_id = 0

    def compose(self) -> ComposeResult:
        yield Vertical(
            *self._render_many(self.page.header),
            id="page-header",
        )
        yield Vertical(
            *self._render_many(self.page.content),
            id="page-content",
        )
        yield Vertical(
            *self._render_many(self.page.actions),
            id="page-actions",
        )

    def on_mount(self) -> None:
        if not self._restore_focus():
            first = next((control for control in self.query("*") if control.can_focus), None)
            if first is not None:
                first.focus()

    def _restore_focus(self) -> bool:
        if self.session.focused_widget_id:
            try:
                control = self.query_one(f"#{self.session.focused_widget_id}")
            except NoMatches:
                return False
            if control.can_focus:
                control.focus()
                return True
        return False

    def on_descendant_focus(self, event) -> None:
        if event.widget.id and not event.widget.id.startswith("renderer_"):
            self.session = self.session.with_focus(event.widget.id)

    def on_input_changed(self, event: TextualInput.Changed) -> None:
        if event.input.id:
            self.session = self.session.with_widget_value(event.input.id, event.value)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id:
            self.session = self.session.with_widget_value(event.switch.id, event.value)

    def on_select_changed(self, event: TextualSelect.Changed) -> None:
        if event.select.id and event.value is not TextualSelect.BLANK:
            self.session = self.session.with_widget_value(event.select.id, event.value)

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        control = event.selection_list
        selected = list(control.selected)
        if control.id in self._single_selection_ids and len(selected) > 1:
            selected = selected[-1:]
            control.deselect_all()
            control.select(selected[0])
        self.session = self.session.with_selected_items(selected)
        self.post_message(
            self.SelectionChanged(
                self.session,
                single_selection=control.id in self._single_selection_ids,
            )
        )

    def on_button_pressed(self, event: TextualButton.Pressed) -> None:
        action = self._actions.get(event.button.id or "")
        if action is not None:
            session = self.session
            if self._pagination:
                if action is self._pagination.previous_action:
                    session = session.with_page(max(1, session.current_page - 1))
                elif action is self._pagination.next_action:
                    session = session.with_page(session.current_page + 1)
            self.session = session
            self.post_message(self.ActionSelected(action, session))

    def action_focus_previous(self) -> None:
        self.screen.focus_previous()

    def action_focus_next(self) -> None:
        self.screen.focus_next()

    def action_back_or_cancel(self) -> None:
        session, location = self.session.pop_navigation()
        self.session = session
        if location is None:
            self.post_message(self.Cancelled(session))
        else:
            self.post_message(self.BackRequested(location, session))

    def action_cancel(self) -> None:
        self.post_message(self.Cancelled(self.session))

    def action_previous_page(self) -> None:
        if self._pagination and self._pagination.previous_action:
            self.session = self.session.with_page(max(1, self.session.current_page - 1))
            self.post_message(self.ActionSelected(self._pagination.previous_action, self.session))

    def action_next_page(self) -> None:
        if self._pagination and self._pagination.next_action:
            self.session = self.session.with_page(self.session.current_page + 1)
            self.post_message(self.ActionSelected(self._pagination.next_action, self.session))

    async def show_page(self, page: Page, session: ViewSession) -> None:
        self.page = page
        self.session = session
        self._actions.clear()
        self._pagination = None
        self._single_selection_ids.clear()
        self._generated_id = 0
        await self.recompose()
        self._restore_focus()

    def _render_many(self, widgets: Iterable[Widget]) -> list[object]:
        return [rendered for widget in widgets if (rendered := self._translate_widget(widget))]

    def _translate_widget(self, widget: Widget):
        if not widget.visible:
            return None
        common = {"id": widget.widget_id, "disabled": not widget.enabled}
        if type(widget) is Title:
            return Static(widget.text, classes="core-title", **common)
        if type(widget) is Text:
            return Static(widget.text, **common)
        if type(widget) is Notice:
            return Static(widget.text, classes=f"notice-{widget.severity}", **common)
        if type(widget) is Progress:
            return self._render_progress(widget, common)
        if type(widget) is ListView:
            return self._render_list(widget, common)
        if type(widget) is MetadataView:
            return Vertical(
                *(
                    Horizontal(Label(key, classes="metadata-key"), Static(value))
                    for key, value in widget.items.items()
                ),
                **common,
            )
        if type(widget) is TagList:
            return Static("  ".join(widget.tags), **common)
        if type(widget) is Input:
            return Vertical(
                Label(widget.label, classes="field-label"),
                TextualInput(
                    value=str(self.session.widget_values.get(widget.widget_id, widget.value)),
                    placeholder=widget.placeholder or "",
                    id=widget.widget_id,
                    disabled=not widget.enabled,
                ),
            )
        if type(widget) is Toggle:
            return Vertical(
                Label(widget.label, classes="field-label"),
                Switch(
                    value=bool(self.session.widget_values.get(widget.widget_id, widget.value)),
                    id=widget.widget_id,
                    disabled=not widget.enabled,
                ),
            )
        if type(widget) is Select:
            value = self.session.widget_values.get(widget.widget_id, widget.value)
            return Vertical(
                Label(widget.label, classes="field-label"),
                TextualSelect(
                    ((option.text, option.value) for option in widget.options),
                    value=TextualSelect.BLANK if value is None else value,
                    id=widget.widget_id,
                    disabled=not widget.enabled,
                ),
            )
        if type(widget) is Button:
            self._actions[widget.widget_id] = widget.action
            return TextualButton(widget.text, id=widget.widget_id, disabled=not widget.enabled)
        if type(widget) is Pagination:
            return self._render_pagination(widget, common)
        if type(widget) is Container:
            return Vertical(*self._render_many(widget.children), **common)
        return None

    def _render_progress(self, widget: Progress, common: dict[str, object]):
        total = widget.total or max(widget.current, 1)
        return Vertical(
            Static(widget.text) if widget.text else Static(),
            ProgressBar(total=total, progress=widget.current, show_eta=False),
            **common,
        )

    def _render_list(self, widget: ListView, common: dict[str, object]):
        if widget.selection_mode is SelectionMode.NONE:
            return TextualListView(
                *(
                    TextualListItem(
                        Label(self._list_item_text(item, index)),
                        id=item.widget_id,
                        disabled=not item.enabled,
                    )
                    for index, item in enumerate(widget.items, start=1)
                    if item.visible
                ),
                **common,
            )
        selected = set(self.session.selected_item_ids)
        if not selected:
            selected = {item.widget_id for item in widget.items if item.selected}
        if widget.selection_mode is SelectionMode.SINGLE:
            self._single_selection_ids.add(widget.widget_id)
        return AsciiSelectionList(
            *(
                Selection(
                    self._list_item_text(item, index),
                    item.widget_id,
                    item.widget_id in selected,
                    id=item.widget_id,
                    disabled=not item.enabled,
                )
                for index, item in enumerate(widget.items, start=1)
                if item.visible
            ),
            **common,
        )

    def _render_pagination(self, widget: Pagination, common: dict[str, object]):
        self._pagination = widget
        children: list[object] = []
        if widget.previous_action:
            button_id = self._internal_id("previous")
            self._actions[button_id] = widget.previous_action
            children.append(TextualButton("Previous", id=button_id))
        total = "?" if widget.total_pages is None else str(widget.total_pages)
        children.append(Static(f"Page {widget.current_page} / {total}"))
        if widget.next_action:
            button_id = self._internal_id("next")
            self._actions[button_id] = widget.next_action
            children.append(TextualButton("Next", id=button_id))
        return Horizontal(*children, classes="pagination", **common)

    def _internal_id(self, name: str) -> str:
        self._generated_id += 1
        return f"renderer_{name}_{self._generated_id}"

    @staticmethod
    def _list_item_text(item, index: int | None = None) -> RichText:
        rendered = RichText()
        if index is not None:
            rendered.append(f"{index}. ", style="#777777")
        rendered.append(item.title)
        if item.text is not None:
            rendered.append(f"\n{item.text}", style="#999999")
        return rendered


class CorePageApp(App[TextualRenderOutcome]):
    CSS = CORE_APP_CSS

    BINDINGS = CorePageView.BINDINGS

    def __init__(self, page: Page, session: ViewSession) -> None:
        super().__init__()
        self.register_theme(CLI_DOWNLOADER_THEME)
        self.theme = CLI_DOWNLOADER_THEME.name
        self.page = page
        self._session = session

    @property
    def session(self) -> ViewSession:
        try:
            return self.query_one(CorePageView).session
        except NoMatches:
            return self._session

    def compose(self) -> ComposeResult:
        yield CorePageView(self.page, self._session)

    def action_focus_previous(self) -> None:
        self.screen.focus_previous()

    def action_focus_next(self) -> None:
        self.screen.focus_next()

    def action_back_or_cancel(self) -> None:
        self.query_one(CorePageView).action_back_or_cancel()

    def action_cancel(self) -> None:
        self.query_one(CorePageView).action_cancel()

    def action_previous_page(self) -> None:
        self.query_one(CorePageView).action_previous_page()

    def action_next_page(self) -> None:
        self.query_one(CorePageView).action_next_page()

    def on_core_page_view_action_selected(self, event: CorePageView.ActionSelected) -> None:
        self.exit(TextualRenderOutcome(session=event.session, action=event.action))

    def on_core_page_view_cancelled(self, event: CorePageView.Cancelled) -> None:
        self.exit(TextualRenderOutcome(session=event.session, cancelled=True))

    def on_core_page_view_back_requested(self, event: CorePageView.BackRequested) -> None:
        self.exit(
            TextualRenderOutcome(
                session=event.session,
                back_location=event.location,
            )
        )


def render_interactive_result(
    result: OperationResult,
    *,
    plugin_id: str,
    session: ViewSession | None = None,
) -> TextualRenderOutcome:
    if result.page is None:
        raise ValueError("interactive results require a core Page")
    current = session or ViewSession(
        session_id=result.request_id,
        plugin_id=plugin_id,
        request_id=result.request_id,
    )
    outcome = CorePageApp(result.page, current).run()
    return outcome or TextualRenderOutcome(session=current, cancelled=True)
