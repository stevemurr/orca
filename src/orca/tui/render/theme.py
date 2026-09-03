"""Shared visual tokens for the terminal renderer.

Two tones and one colour. An orca is black and white: the person's words are the text
colour and bold, the agent's process is grey, and the one colour -- a cold sea-blue -- goes
only to what is live or chosen: the caret, the row being worked, the selected item. Amber
is for a decision the person has to make, red for a failure, green for a finished run.
"""

from rich.style import Style
from rich.theme import Theme

ACCENT = "#7dd3fc"
MUTED = "#8b98a5"
SUCCESS = "#75d59a"
WARNING = "#e5c07b"
ERROR = "#ef7d7d"
TEXT = "#e6edf3"
#: Deep water: the shell's ground, and the two steps up from it that a card and a code
#: block sit on. Cold rather than warm, and never a pure black, so the text colour has
#: something to be lighter than.
BACKGROUND = "#0e1319"
SURFACE = "#151b22"
PANEL = "#1b232c"
CALLOUT = "#2a333d"
CODE_BACKGROUND = SURFACE

MARKDOWN_THEME = Theme(
    {
        "markdown.code": Style(bold=True),
        "markdown.code_block": Style.null(),
        "markdown.block_quote": Style(color=MUTED, italic=True),
        "markdown.list": Style.null(),
        "markdown.item.number": Style(color=MUTED),
        "markdown.item.bullet": Style(color=MUTED, bold=True),
        "markdown.table.border": Style(color=CALLOUT),
        "markdown.table.header": Style(bold=True),
        # Headings are bold in the text colour, as an editor's agent sets them; only the
        # first level takes the accent, so a long answer is not blue all the way down.
        "markdown.h1": Style(color=ACCENT, bold=True),
        "markdown.h2": Style(bold=True),
        "markdown.h3": Style(bold=True),
        "markdown.h4": Style(bold=True),
        "markdown.link": Style(color=ACCENT),
        "markdown.link_url": Style(color=ACCENT, underline=True),
        "markdown.kbd": Style(reverse=True),
        "markdown.hr": Style(color=CALLOUT),
    },
    inherit=False,
)
