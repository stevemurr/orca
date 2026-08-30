"""Transient overlays that sit above the persistent shell."""

from __future__ import annotations

from datetime import datetime

from rich.console import Group, RenderableType
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from orca.app.actions import ApprovalDecided
from orca.app.model import AppState
from orca.tui.render import render_help, render_interaction


class HelpScreen(ModalScreen[None]):
    BINDINGS = (Binding("escape", "dismiss", "Close", show=False),)

    def __init__(self, *, developer: bool = False) -> None:
        super().__init__()
        self._developer = developer

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-card", id="help-card"):
            yield Static(render_help(developer=self._developer))
            yield Static("Esc close", classes="modal-hint")

    def action_dismiss(self) -> None:
        self.dismiss()


class ThreadPickerScreen(ModalScreen[tuple[str, str] | None]):
    """Choose a durable thread without replacing the persistent application shell."""

    BINDINGS = (Binding("escape", "close", "Close", show=False),)

    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        super().__init__()
        self._rows = rows
        self._choices = {
            str(row.get("thread_id") or ""): (
                str(row.get("thread_id") or ""),
                str(row.get("title") or ""),
            )
            for row in rows
            if row.get("thread_id")
        }

    def compose(self) -> ComposeResult:
        with Container(classes="modal-card thread-card"):
            yield Static("Recent conversations", classes="modal-title")
            if not self._rows:
                yield Static("No conversations yet.", classes="empty-state")
            else:
                yield OptionList(
                    *(
                        Option(_thread_prompt(row), id=str(row["thread_id"]))
                        for row in self._rows
                        if row.get("thread_id")
                    ),
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


class ApprovalScreen(ModalScreen[None]):
    BINDINGS = (
        Binding("1,y", "choose('approve')", "Approve", show=False),
        Binding("2", "choose('approve_bash_always')", "Always", show=False),
        Binding("3,n,escape", "choose('reject')", "Reject", show=False),
    )

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self._state = state
        self._sending = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-card approval-card", id="approval-card"):
            yield Static(self._content(), id="approval-content")

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.query_one("#approval-content", Static).update(
                self._content(viewport_width=event.size.width)
            )

    def action_choose(self, decision: str) -> None:
        if self._sending:
            return
        interaction = self._state.interaction
        if (
            decision == "approve_bash_always"
            and interaction is not None
            and decision not in interaction.allowed_decisions
        ):
            return
        self._sending = True
        self.query_one("#approval-content", Static).update(self._content())
        self.app.apply_model_action(ApprovalDecided(decision))  # type: ignore[attr-defined]

    def allow_retry(self) -> None:
        """Restore choices after the command could not reach the Task API."""

        self._sending = False
        self.query_one("#approval-content", Static).update(self._content())

    def _content(self, *, viewport_width: int | None = None) -> RenderableType:
        renderable = self._interaction(viewport_width=viewport_width) or Text("Approval")
        if self._sending:
            return Group(renderable, Text("Sending…", style="dim"))
        return renderable

    def _interaction(self, *, viewport_width: int | None = None) -> RenderableType | None:
        viewport_width = viewport_width or self.app.size.width
        width = max(1, min(80, viewport_width - 8))
        return render_interaction(self._state, width=width)


def _thread_prompt(row: dict[str, object]) -> Text:
    title = str(row.get("title") or "Untitled conversation")
    status = str(row.get("latest_run_status") or "idle").replace("_", " ")
    updated = _display_timestamp(str(row.get("updated_at") or ""))
    detail = status if not updated else f"{status}  ·  {updated}"
    prompt = Text(title, style="bold")
    prompt.append(f"\n{detail}", style="dim")
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
