"""Shared visual tokens for the terminal renderer."""

from rich.style import Style
from rich.theme import Theme

ACCENT = "#7dd3fc"
MUTED = "#8b98a5"
SUCCESS = "#75d59a"
WARNING = "#e5c07b"
ERROR = "#ef7d7d"
CALLOUT = "#2a333d"

MARKDOWN_THEME = Theme(
    {
        "markdown.code": Style(bold=True),
        "markdown.code_block": Style.null(),
        "markdown.block_quote": Style(color=MUTED, italic=True),
        "markdown.list": Style.null(),
        "markdown.item.number": Style(color=MUTED),
        "markdown.item.bullet": Style(color=MUTED, bold=True),
        "markdown.table.border": Style(color=MUTED),
        "markdown.table.header": Style(bold=True),
        "markdown.h1": Style(color=ACCENT, bold=True),
        "markdown.h2": Style(color=ACCENT, bold=True),
        "markdown.h3": Style(color=ACCENT, bold=True),
        "markdown.h4": Style(bold=True),
        "markdown.link": Style(color=ACCENT),
        "markdown.link_url": Style(color=ACCENT, underline=True),
        "markdown.kbd": Style(reverse=True),
    },
    inherit=False,
)
