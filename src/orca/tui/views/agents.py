"""Keyboard-navigable agent work map."""

from __future__ import annotations

from textual.binding import Binding
from textual.message import Message

from orca.tui.render import render_agents
from orca.tui.views.base import RenderedView


class AgentsView(RenderedView):
    can_focus = True
    renderer = staticmethod(render_agents)
    BINDINGS = (
        Binding("up,k", "move(-1)", "Previous", show=False),
        Binding("down,j", "move(1)", "Next", show=False),
    )

    class SelectionMoved(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    def action_move(self, delta: int) -> None:
        self.post_message(self.SelectionMoved(delta))
