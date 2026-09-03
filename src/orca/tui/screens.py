"""Transient overlays that sit above the persistent shell."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, override

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from orca.backend import ThreadSummary
from orca.tui.render import render_help


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", "Close", show=False)]

    def __init__(self, *, developer: bool = False) -> None:
        super().__init__()
        self._developer: bool = developer

    @override
    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-card", id="help-card"):
            yield Static(render_help(developer=self._developer))
            yield Static("Esc close", classes="modal-hint")


class ThreadPickerScreen(ModalScreen[tuple[str, str] | None]):
    """Choose a durable thread without replacing the persistent application shell."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, rows: tuple[ThreadSummary, ...]) -> None:
        super().__init__()
        self._rows: tuple[ThreadSummary, ...] = nested_threads(rows)
        self._choices: dict[str, tuple[str, str]] = {
            row.thread_id: (row.thread_id, row.title) for row in rows
        }

    @override
    def compose(self) -> ComposeResult:
        with Container(classes="modal-card thread-card"):
            yield Static("Recent conversations", classes="modal-title")
            if not self._rows:
                yield Static("No conversations yet.", classes="empty-state")
            else:
                yield OptionList(
                    *(Option(_thread_prompt(row), id=row.thread_id) for row in self._rows),
                    id="thread-options",
                )
            yield Static("Enter continue  ·  Esc close", classes="modal-hint")

    def on_mount(self) -> None:
        options = self.query(OptionList)
        if options:
            options.first().focus()

    @on(OptionList.OptionSelected, "#thread-options")
    def option_selected(self, message: OptionList.OptionSelected) -> None:
        choice = self._choices.get(message.option_id or "")
        if choice is not None:
            self.dismiss(choice)

    def action_close(self) -> None:
        self.dismiss(None)


def nested_threads(rows: tuple[ThreadSummary, ...]) -> tuple[ThreadSummary, ...]:
    """Each delegated thread directly under the thread that delegated it.

    The listing arrives flat, newest first. A child whose parent is not listed stays where
    it was, still marked as a child, rather than vanishing.
    """
    listed = {row.thread_id for row in rows}
    ordered: list[ThreadSummary] = []
    for row in rows:
        if row.parent and row.parent in listed:
            continue
        ordered.append(row)
        ordered.extend(child for child in rows if child.parent == row.thread_id)
    return tuple(ordered)


def _thread_prompt(row: ThreadSummary) -> Text:
    title = row.title or "Untitled conversation"
    status = (row.latest_run_status or "idle").replace("_", " ")
    updated = _display_timestamp(row.updated_at)
    parts = [status]
    if updated:
        parts.append(updated)
    if row.folder:
        parts.append(row.folder)
    prompt = Text("↳ " if row.parent else "", style="dim")
    prompt.append(title, style="bold")
    prompt.append("\n" + "  ·  ".join(parts), style="dim")
    return prompt


def _display_timestamp(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return value
    clock = parsed.strftime("%I:%M %p").lstrip("0")
    return f"{parsed:%b} {parsed.day}, {clock}"
