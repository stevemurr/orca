"""Developer-only event inspector."""

from orca.tui.render import render_inspector
from orca.tui.views.base import RenderedView


class InspectorView(RenderedView):
    renderer = staticmethod(render_inspector)
