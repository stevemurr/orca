"""Conversation view."""

from orca.tui.render import render_conversation
from orca.tui.views.base import RenderedView


class ConversationView(RenderedView):
    renderer = staticmethod(render_conversation)
    follow_output = True
