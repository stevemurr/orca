"""Persistent terminal widgets shared across views."""

from __future__ import annotations

from typing import override

from textual import events
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import Edit, EditResult


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
        #: What was sent from here this session, oldest first, for Up to bring back the
        #: way a shell does. `_recalled` is where Up and Down are in it -- past the end
        #: when not looking back -- and `_stash` is the draft that was being typed when
        #: the looking back began, so Down at the end gives it back.
        self.sent: list[str] = []
        self._recalled: int = 0
        self._stash: str = ""

    @override
    async def _on_key(self, event: events.Key) -> None:
        if self.approval_keys:
            decision = self.approval_keys.get(event.key)
            if event.key == "enter" and self.text.strip():
                # Enter with words in the box sends the words, not the decision.
                decision = None
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
        if event.key in {"up", "down"} and self._recall(-1 if event.key == "up" else 1):
            event.stop()
            event.prevent_default()
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if not self.menu_open:
                # A message, not a menu choice: kept for Up to bring back.
                self.remember(self.text)
            self.post_message(self.Submitted(self.text))
            return
        if event.key in {"shift+enter", "alt+enter"}:
            event.stop()
            event.prevent_default()
            self.replace("\n", *self.selection)
            return
        await super()._on_key(event)

    def remember(self, text: str) -> None:
        """Keep what was just sent, for Up. The same thing twice running is kept once."""
        if text.strip() and (not self.sent or self.sent[-1] != text):
            self.sent.append(text)
        self._recalled = len(self.sent)
        self._stash = ""

    def _recall(self, step: int) -> bool:
        """Move through the history by `step`, or say no: Up and Down move the cursor
        inside a draft of several lines, and only reach back from its first line or
        forward from its last, the way a shell with a multi-line edit does."""
        row, _ = self.cursor_location
        on_edge = row == 0 if step < 0 else row == self.document.line_count - 1
        if not self.sent or not on_edge:
            return False
        target = self._recalled + step
        if target < 0 or target > len(self.sent):
            return False
        if self._recalled == len(self.sent):
            self._stash = self.text
        self._recalled = target
        self.replace_text(self.sent[target] if target < len(self.sent) else self._stash)
        return True

    @override
    def edit(self, edit: Edit) -> EditResult:
        """Every change to the text comes through here, and the height follows it at
        once. It used to follow from the `Changed` message, one message cycle later,
        which laid the screen out twice for a new line -- once for the edit, once for the
        height -- and the transcript above is laid out with it each time."""
        result = super().edit(edit)
        self.fit_height()
        return result

    def replace_text(self, text: str) -> None:
        """Put `text` in the composer with the cursor at its end. A load is not an edit,
        so the height is followed here as well: a send clears a draft of several lines
        this way, and the box stayed tall until the next keystroke."""
        self.load_text(text)
        self.move_cursor(self.document.end)
        self.fit_height()

    def fit_height(self) -> None:
        # As tall as the draft, to seven lines; the frame around it carries the border.
        # Set only when it changes: assigning a height lays the screen out again, and
        # every keystroke used to.
        lines = min(7, max(1, self.text.count("\n") + 1))
        if self.styles.height is None or self.styles.height.value != lines:
            self.styles.height = lines
