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

from orca.app.commands import Choices
from orca.backend import ThreadSummary
from orca.tui.render import render_help
from orca.tui.render.theme import ACCENT, ERROR, MUTED, SUCCESS, WARNING


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", "Close", show=False)]

    def __init__(self, *, developer: bool = False, choices: Choices | None = None) -> None:
        super().__init__()
        self._developer: bool = developer
        self._choices: Choices = choices or {}

    @override
    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-card", id="help-card"):
            yield Static(render_help(developer=self._developer, choices=self._choices))
            yield Static("esc close", classes="modal-hint")


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
            yield Static("enter continue   esc close", classes="modal-hint")

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
    """Each delegated thread directly under the thread that delegated it, to any depth.

    The listing arrives flat, newest first. A child whose parent is not listed stays where
    it was, still marked as a child, rather than vanishing. A delegated thread can delegate
    in turn, so the walk descends from each root rather than stopping at one level.
    """
    listed = {row.thread_id for row in rows}
    children: dict[str, list[ThreadSummary]] = {}
    for row in rows:
        if row.parent and row.parent in listed:
            children.setdefault(row.parent, []).append(row)
    ordered: list[ThreadSummary] = []
    seen: set[str] = set()

    def walk(row: ThreadSummary) -> None:
        # A cycle in the listing is a backend fault; refusing to loop on it beats a crash.
        if row.thread_id in seen:
            return
        seen.add(row.thread_id)
        ordered.append(row)
        for child in children.get(row.thread_id, ()):
            walk(child)

    for row in rows:
        if row.parent and row.parent in listed:
            continue
        walk(row)
    # Every row in a cycle has a listed parent, so none is a root; list them rather than lose them.
    ordered.extend(row for row in rows if row.thread_id not in seen)
    return tuple(ordered)


def _thread_prompt(row: ThreadSummary) -> Text:
    """Two lines: a mark and the title, then how it ended, when, and where, under the
    title. The mark says at a glance what the word says on the next line."""
    title = row.title or "Untitled conversation"
    status = (row.latest_run_status or "idle").replace("_", " ")
    updated = _display_timestamp(row.updated_at)
    parts = [status]
    if updated:
        parts.append(updated)
    if row.folder:
        parts.append(row.folder)
    lead = "↳ " if row.parent else ""
    mark, style = _status_mark(status)
    prompt = Text(lead, style="dim")
    prompt.append(f"{mark} ", style=style)
    prompt.append(title, style="bold")
    prompt.append("\n" + " " * (len(lead) + 2) + "  ".join(parts), style="dim")
    return prompt


def _status_mark(status: str) -> tuple[str, str]:
    if status in {"running", "queued"}:
        return "●", ACCENT
    if status == "completed":
        return "✓", SUCCESS
    if status in {"failed", "blocked", "cancelled"}:
        return "✗", ERROR
    if status.startswith("awaiting") or status == "paused":
        return "◐", WARNING
    return "○", MUTED


def _display_timestamp(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return value
    clock = parsed.strftime("%I:%M %p").lstrip("0")
    return f"{parsed:%b} {parsed.day}, {clock}"
