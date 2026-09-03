"""Persistent terminal widgets shared across views."""

from __future__ import annotations

from typing import override

from textual import events
from textual.message import Message
from textual.widgets import TextArea


class Composer(TextArea):
    """Multiline composer where Enter submits and modified Enter inserts a line."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text: str = text

    class MenuMoved(Message):
        """Up or down in the command menu that opens on `/`."""

        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta: int = delta

    class MenuAccepted(Message):
        """Tab: take the highlighted command into the composer without sending it."""

    class Decided(Message):
        """A key answered the approval shown in the transcript."""

        def __init__(self, decision: str) -> None:
            super().__init__()
            self.decision: str = decision

    def __init__(self) -> None:
        super().__init__(
            id="composer",
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            highlight_cursor_line=False,
            placeholder="Ask the agent…",
        )
        #: Key to decision while an approval is waiting, set by the host; empty otherwise.
        #: The keys go to the approval rather than into the text, as they would in a modal.
        self.approval_keys: dict[str, str] = {}
        #: Whether the `/` menu has rows right now, set by the host as it renders them. Up,
        #: down and Tab go to the menu while it does, and to the text otherwise.
        self.menu_open: bool = False

    @override
    async def _on_key(self, event: events.Key) -> None:
        if self.approval_keys:
            decision = self.approval_keys.get(event.key)
            if decision is None and event.key == "enter" and not self.text.strip():
                decision = self.approval_keys.get("1")
            if decision is not None:
                event.stop()
                event.prevent_default()
                self.post_message(self.Decided(decision))
                return
        if self.menu_open and event.key in {"up", "down", "tab"}:
            event.stop()
            event.prevent_default()
            if event.key == "tab":
                self.post_message(self.MenuAccepted())
            else:
                self.post_message(self.MenuMoved(-1 if event.key == "up" else 1))
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self.text))
            return
        if event.key in {"shift+enter", "alt+enter"}:
            event.stop()
            event.prevent_default()
            self.replace("\n", *self.selection)
            return
        await super()._on_key(event)

    def replace_text(self, text: str) -> None:
        """Put `text` in the composer with the cursor at its end."""
        self.load_text(text)
        self.move_cursor(self.document.end)

    def fit_height(self) -> None:
        # As tall as the draft, to seven lines; the frame around it carries the border.
        self.styles.height = min(7, max(1, self.text.count("\n") + 1))
