"""Persistent terminal widgets shared across views."""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import TextArea


class Composer(TextArea):
    """Multiline composer where Enter submits and modified Enter inserts a line."""

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self) -> None:
        super().__init__(
            id="composer",
            soft_wrap=True,
            tab_behavior="focus",
            show_line_numbers=False,
            highlight_cursor_line=False,
            placeholder="Ask the agent…",
        )

    async def _on_key(self, event: events.Key) -> None:
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

    def fit_height(self) -> None:
        lines = min(7, max(1, self.text.count("\n") + 1))
        self.styles.height = lines + 2
