"""Conversation view."""

from typing import ClassVar

from orca.tui.render import render_conversation
from orca.tui.views.base import RenderedView, StateRenderer


class ConversationView(RenderedView):
    renderer: ClassVar[StateRenderer] = staticmethod(render_conversation)
    follow_output: ClassVar[bool] = True
