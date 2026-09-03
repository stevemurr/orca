"""Code as a block: a file about to be written, or the diff of an edit."""

from __future__ import annotations

from functools import lru_cache
from typing import override

from rich.console import Group, RenderableType
from rich.syntax import Syntax
from rich.text import Text

from orca.app.model import Snippet
from orca.tui.render.theme import CODE_BACKGROUND, MUTED

#: One scheme for every block, on the shell's own surface, so code reads as part of the
#: transcript rather than a pasted-in window. GitHub's dark scheme: cool, legible on a
#: blue-black ground, and it underlines nothing, which the terminal-native one does.
CODE_THEME = "github-dark"


class CachedSyntax(Syntax):
    """`Syntax` whose lexing is remembered.

    The same block is rendered again and again -- on every tick of the clock while a run
    goes, and once for each turn as it scrolls into view -- and pygments was most of what
    each render cost. The tokens depend on the code and the lexer alone, so they are kept
    by those and copied out; wrapping, width and the ground are done fresh each time.
    """

    @override
    def highlight(self, code: str, line_range: tuple[int | None, int | None] | None = None) -> Text:
        if line_range is not None:
            return super().highlight(code, line_range)
        lexer = self._lexer if isinstance(self._lexer, str) else self._lexer.name
        return _highlighted(code, lexer, self.background_color).copy()


@lru_cache(maxsize=256)
def _highlighted(code: str, lexer: str, background: str | None) -> Text:
    # Built the way the block is, ground included: the ground is part of how a token is
    # styled -- a diff's added line takes the theme's green ground without it.
    return Syntax(code, lexer, theme=CODE_THEME, background_color=background).highlight(code)


def code_block(snippet: Snippet, *, lines: int, width: int | None = None) -> RenderableType:
    """The snippet, highlighted, cut at `lines` with a count of what was left out."""
    every = snippet.text.splitlines()
    shown = "\n".join(every[:lines])
    lexer = snippet.language or Syntax.guess_lexer(snippet.title, code=shown)
    block = CachedSyntax(
        shown,
        lexer,
        theme=CODE_THEME,
        background_color=CODE_BACKGROUND,
        word_wrap=True,
        code_width=width,
        padding=(0, 1),
    )
    if len(every) <= lines:
        return block
    return Group(block, Text(f"… {len(every) - lines} more lines", style=MUTED))
