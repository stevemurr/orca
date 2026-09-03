"""Developer-only event inspector."""

from typing import ClassVar

from orca.tui.render import render_inspector
from orca.tui.views.base import RenderedView, StateRenderer


class InspectorView(RenderedView):
    renderer: ClassVar[StateRenderer] = staticmethod(render_inspector)
