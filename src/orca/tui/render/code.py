"""Code as a block: a file about to be written, or the diff of an edit."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text

from orca.app.model import Snippet
from orca.tui.render.theme import MUTED


def code_block(snippet: Snippet, *, lines: int, width: int | None = None) -> RenderableType:
    """The snippet, highlighted, cut at `lines` with a count of what was left out."""
    every = snippet.text.splitlines()
    shown = "\n".join(every[:lines])
    lexer = snippet.language or Syntax.guess_lexer(snippet.title, code=shown)
    block = Syntax(shown, lexer, theme="ansi_dark", word_wrap=True, code_width=width)
    if len(every) <= lines:
        return block
    return Group(block, Text(f"… {len(every) - lines} more lines", style=MUTED))
