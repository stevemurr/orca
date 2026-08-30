"""Result and artifact review view."""

from orca.tui.render import render_review
from orca.tui.views.base import RenderedView


class ReviewView(RenderedView):
    renderer = staticmethod(render_review)
