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
from orca.tui.render.conversation import render_turn, turn_key, welcome
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

    @override
    def compose(self) -> ComposeResult:
        yield Static(id="conversation-welcome")

    @override
    def update_state(self, state: AppState) -> None:
        was_at_end = self.is_vertical_scroll_end or self.max_scroll_y == 0
        width = max(1, state.viewport_width - 2)

        opening = self.query_one("#conversation-welcome", Static)
        opening.display = not state.turns
        if not state.turns:
            opening.update(Group(*welcome(state, width=width)))

        # A new conversation has fewer turns than the last one shown: the rest go.
        while len(self._panels) > len(state.turns):
            _ = self._panels.pop().remove()

        for index, turn in enumerate(state.turns):
            key = turn_key(state, turn, width=width)
            if index < len(self._panels):
                panel = self._panels[index]
                if key is not None and panel.key == key:
                    continue
                panel.key = key
                panel.update(render_turn(state, turn, width=width))
            else:
                panel = TurnPanel(key, render_turn(state, turn, width=width))
                self._panels.append(panel)
                _ = self.mount(panel)

        if was_at_end:
            self.call_after_refresh(self.scroll_end, animate=False)
