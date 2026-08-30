"""Shared container for a pure Rich state renderer."""

from __future__ import annotations

from collections.abc import Callable

from rich.console import RenderableType
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from orca.app.model import AppState


class RenderedView(VerticalScroll):
    """Scrollable view whose only input is immutable application state."""

    renderer: Callable[..., RenderableType]
    follow_output = False

    def compose(self) -> ComposeResult:
        yield Static(id=f"{self.id}-content" if self.id else None)

    def update_state(self, state: AppState) -> None:
        was_at_end = self.follow_output and (self.is_vertical_scroll_end or self.max_scroll_y == 0)
        content = self.query_one(Static)
        content.update(self.renderer(state, width=max(1, state.viewport_width - 2)))
        if was_at_end:
            self.call_after_refresh(self.scroll_end, animate=False)
