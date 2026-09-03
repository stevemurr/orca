"""Conversation view: one widget per turn.

Textual keeps a widget's rendered lines until the widget is updated. So a turn that has
not changed costs a frame nothing, and a frame costs what the turn a run is going in
costs -- not the transcript. The conversation used to be one Static holding the whole
transcript, re-rendered on every event and every tick of the clock: 210 ms for thirty
turns, up to twelve times a second, and a keystroke waited behind each one. (2026-09-03)
"""

from __future__ import annotations

from typing import override

from rich.console import Group, RenderableType
from textual.app import ComposeResult
from textual.widgets import Static

from orca.app.model import AppState
from orca.tui.render.conversation import (
    Piece,
    render_live_turn,
    render_turn,
    turn_key,
    welcome,
)
from orca.tui.views.base import StateView


class TurnPanel(Static):
    """One turn's rows, and the key they were rendered under."""

    def __init__(self, key: tuple[object, ...] | None, content: RenderableType) -> None:
        super().__init__(content)
        self.key: tuple[object, ...] | None = key


class ConversationView(StateView):
    def __init__(self, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        #: The panels in transcript order. Kept here rather than queried, because a panel
        #: is mounted asynchronously and a query between the ask and the mount would not
        #: see it -- and the next update would mount it again.
        self._panels: list[TurnPanel] = []
        #: The moving pieces of the turn a run is going in, under its head: the paragraph
        #: being written, and the last tool group with the spinner and prompt. Each in a
        #: panel of its own, so a delta redraws a paragraph and a tick a few lines, not
        #: the whole turn.
        self._live: list[TurnPanel] = []

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="conversation-welcome")

    def on_mount(self) -> None:
        # Anchored, the view stays at the end as content grows, and Textual keeps it
        # there during layout rather than one frame later: scrolling to the end after
        # the refresh showed the new lines at the old offset for a frame, then jumped,
        # which a person saw as a flash when a block of code arrived. A person who
        # scrolls up releases the anchor; scrolling back to the end takes it again,
        # and Textual does both without help from here.
        self.anchor()

    def _show_live(self, pieces: tuple[Piece, ...]) -> None:
        """The live turn's moving pieces, by position: a panel each, kept while its key
        holds, redrawn when it changes or when it has none, gone when the turn settles."""
        while len(self._live) > len(pieces):
            _ = self._live.pop().remove()
        for index, (key, content) in enumerate(pieces):
            if index < len(self._live):
                panel = self._live[index]
                if key is None or panel.key != key:
                    panel.key = key
                    panel.update(content)
            else:
                panel = TurnPanel(key, content)
                self._live.append(panel)
                _ = self.mount(panel)

    @override
    def update_state(self, state: AppState) -> None:
        width = max(1, state.viewport_width - 2)

        opening = self.query_one("#conversation-welcome", Static)
        opening.display = not state.turns
        if not state.turns:
            opening.update(Group(*welcome(state, width=width)))

        # A new conversation has fewer turns than the last one shown: the rest go.
        while len(self._panels) > len(state.turns):
            _ = self._panels.pop().remove()
        if not state.turns:
            self._show_live(())

        last = len(state.turns) - 1
        for index, turn in enumerate(state.turns):
            key = turn_key(state, turn, width=width)
            content: RenderableType | None = None
            moving: tuple[Piece, ...] = ()
            if key is None and index == last:
                (key, content), *rest = render_live_turn(state, turn, width=width)
                moving = tuple(rest)
            if index < len(self._panels):
                panel = self._panels[index]
                if key is None or panel.key != key:
                    panel.key = key
                    panel.update(content or render_turn(state, turn, width=width))
            else:
                panel = TurnPanel(key, content or render_turn(state, turn, width=width))
                self._panels.append(panel)
                _ = self.mount(panel)
            if index == last:
                self._show_live(moving)
