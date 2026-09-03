"""Result and artifact review view."""

from typing import ClassVar

from orca.tui.render import render_review
from orca.tui.views.base import RenderedView, StateRenderer


class ReviewView(RenderedView):
    renderer: ClassVar[StateRenderer] = staticmethod(render_review)
