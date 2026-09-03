"""Delegated agents: each one's life, rows and words, where the parent's timeline shows
only that it was started and what it answered."""

from typing import ClassVar

from orca.tui.render import render_agents
from orca.tui.views.base import RenderedView, StateRenderer


class AgentsView(RenderedView):
    renderer: ClassVar[StateRenderer] = staticmethod(render_agents)
