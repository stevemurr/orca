"""Code-aware Markdown recovery and Rich rendering for canonical answers."""

from __future__ import annotations

import re
from typing import ClassVar, override

from rich.console import Console, ConsoleOptions, RenderResult
from rich.markdown import CodeBlock, Markdown, MarkdownElement
from rich.syntax import Syntax

from orca.tui.render.code import CODE_THEME
from orca.tui.render.theme import CODE_BACKGROUND, MARKDOWN_THEME

_FLAT_HEADING = re.compile(r"#{1,6}[ \t]+")
_FLAT_ORDERED_ITEM = re.compile(r"\d{1,3}\.[ \t]+\S")
_FLAT_BULLET_ITEM = re.compile(r"[-*+][ \t]+\S")
_FLAT_OPENING_BODY = re.compile(
    r"\A(#{1,6}[ \t]+[^\n]*?`[^`\n]+`[^\n]*?)"
    + r"(?=`[^`\n]+`[ \t]+"
    + r"(?:is|are|was|were|provides?|implements?|serves?|uses?|contains?|offers?|supports?)\b)",
    re.IGNORECASE,
)
_FLAT_HEADING_PARAGRAPH = re.compile(
    r"^(#{1,6}[ \t]+(?:"
    + r"Overview|Summary|Introduction|Background|Context|Conclusion|"
    + r"Results|Result|Discussion|Details|Description|Purpose|"
    + r"Recommendations|Recommendation|Observations|Observation"
    + r"))(?=[A-Z0-9`\[])",
    re.MULTILINE,
)
_FLAT_HEADING_BLOCK = re.compile(
    r"^(#{1,6}[ \t]+[^\n]{1,120}?)"
    + r"(?=(?:"
    + r"\d{1,3}\.[ \t]+\S"
    + r"|[-*+][ \t]+\S"
    + r"|>[ \t]+\S"
    + r"|\|[ \t]*[^|\n]+[ \t]*\|"
    + r"))",
    re.MULTILINE,
)
_FLAT_ORDERED_BOUNDARY = re.compile(r"(?<=[.!?])[ \t]?(?=\d{1,3}\.[ \t]+\S)")
_FLAT_SPACED_BULLET = re.compile(r"[ \t]{2,}(?=[-*+][ \t]+\S)")
_FLAT_BULLET_BOUNDARY = re.compile(r"(?<=[.!?])[ \t]?(?=[-*+][ \t]+\S)")
_FLAT_QUOTE_BOUNDARY = re.compile(r"(?<=[.!?])[ \t]?(?=>[ \t]+\S)")
_FLAT_CODE_LIST_BOUNDARY = re.compile(r"(?<=\ue001)[ \t]*(?=(?:[-*+][ \t]+\S|\d{1,3}\.[ \t]+\S))")
_FLAT_QUOTE_ITEM = re.compile(r">[ \t]+\S")
_FLAT_TABLE = re.compile(r"(?m)^\|[^\n]*\|\|[ \t]*:?-{3,}")
_FLAT_TABLE_SIGNAL = re.compile(r"\|[^\n]*\|\|[ \t]*:?-{3,}")
_INLINE_CODE = re.compile(r"(?P<ticks>`+)(?P<body>[^\n]*?)(?P=ticks)")
_CODE_FENCE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")


class AnswerCodeBlock(CodeBlock):
    """A fenced block on the shell's surface, in terminal colours, like a tool's snippet.

    Rich's own block brings a colour scheme of its own -- monokai, an olive ground -- and
    a line of padding above and below; in a transcript that is a window pasted in, not a
    paragraph of code.
    """

    @override
    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield Syntax(
            str(self.text).rstrip(),
            self.lexer_name,
            theme=CODE_THEME,
            background_color=CODE_BACKGROUND,
            word_wrap=True,
            padding=(0, 1),
        )


class AnswerMarkdown(Markdown):
    """Markdown whose semantic styles remain legible inside Textual's console theme."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "fence": AnswerCodeBlock,
        "code_block": AnswerCodeBlock,
    }

    @override
    def __rich_console__(
        self,
        console: Console,
        options: ConsoleOptions,
    ) -> RenderResult:
        with console.use_theme(MARKDOWN_THEME):
            yield from super().__rich_console__(console, options)


def answer_markdown(source: str) -> Markdown:
    """Render canonical prose, repairing only unmistakably flattened block separators.

    Some structured-output providers preserve Markdown punctuation but flatten newlines in the
    JSON string. The durable answer remains untouched; this display projection inserts line
    boundaries only where multiple independent block signals make the intent unambiguous.
    """

    return AnswerMarkdown(recover_flattened_markdown(source))


def recover_flattened_markdown(source: str) -> str:
    """Recover explicit Markdown blocks while preserving code and valid multiline input."""

    if "\n" in source or "\r" in source:
        return _recover_flattened_lines(source)
    return _recover_flattened_line(source)


def _recover_flattened_lines(source: str) -> str:
    """Repair independently flattened lines without touching fenced code blocks."""

    recovered: list[str] = []
    active_fence = ""
    for line in source.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        fence = _CODE_FENCE.match(content)
        if active_fence:
            recovered.append(line)
            if (
                fence
                and fence.group("fence")[0] == active_fence[0]
                and len(fence.group("fence")) >= len(active_fence)
            ):
                active_fence = ""
            continue
        if fence:
            active_fence = fence.group("fence")
            recovered.append(line)
            continue
        recovered.append(_recover_flattened_line(content) + ending)
    return "".join(recovered)


def _recover_flattened_line(source: str) -> str:
    if not source:
        return source

    recovered = _FLAT_OPENING_BODY.sub(r"\1\n\n", source, count=1)
    masked, code_spans = _mask_inline_code(recovered)
    if not _looks_like_flattened_markdown(masked):
        return source

    masked = _recover_flattened_headings(masked)
    masked = _FLAT_HEADING_BLOCK.sub(r"\1\n\n", masked)
    masked = _FLAT_HEADING_PARAGRAPH.sub(r"\1\n\n", masked)
    masked = _FLAT_ORDERED_BOUNDARY.sub("\n", masked)
    masked = _FLAT_BULLET_BOUNDARY.sub("\n", masked)
    masked = _FLAT_QUOTE_BOUNDARY.sub("\n", masked)
    masked = _FLAT_CODE_LIST_BOUNDARY.sub("\n", masked)
    masked = _FLAT_SPACED_BULLET.sub("\n   ", masked)
    masked = _recover_flattened_tables(masked)
    return _restore_inline_code(masked, code_spans)


def _looks_like_flattened_markdown(source: str) -> bool:
    headings = _FLAT_HEADING.findall(source)
    starts_with_heading = bool(re.match(r"^#{1,6}[ \t]+", source))
    if len(headings) >= 2:
        return True
    if _FLAT_TABLE_SIGNAL.search(source):
        return True
    ordered_items = len(_FLAT_ORDERED_ITEM.findall(source))
    bullet_items = len(_FLAT_BULLET_ITEM.findall(source))
    quote_items = len(_FLAT_QUOTE_ITEM.findall(source))
    if starts_with_heading:
        return ordered_items >= 2 or bullet_items >= 2 or quote_items >= 2
    return (
        (bool(re.match(r"^\d{1,3}\.[ \t]+", source)) and ordered_items >= 2)
        or (bool(re.match(r"^[-*+][ \t]+", source)) and bullet_items >= 2)
        or (bool(re.match(r"^>[ \t]+", source)) and quote_items >= 2)
    )


def _recover_flattened_headings(source: str) -> str:
    headings = list(_FLAT_HEADING.finditer(source))
    if not headings:
        return source
    starts_with_heading = headings[0].start() == 0
    boundaries: list[int] = []
    for heading in headings[1:] if starts_with_heading else headings:
        prefix = source[: heading.start()]
        if prefix.endswith("\n"):
            continue
        if starts_with_heading or (prefix.rstrip() and prefix.rstrip()[-1] in ".)!?"):
            boundaries.append(heading.start())
    for boundary in reversed(boundaries):
        before = source[:boundary].rstrip(" \t")
        source = before + "\n\n" + source[boundary:]
    return source


def _mask_inline_code(source: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    spans: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        token = f"\ue000{len(spans)}\ue001"
        spans.append((token, match.group(0)))
        return token

    return _INLINE_CODE.sub(replace, source), tuple(spans)


def _restore_inline_code(source: str, spans: tuple[tuple[str, str], ...]) -> str:
    for token, code in spans:
        source = source.replace(token, code)
    return source


def _recover_flattened_tables(source: str) -> str:
    """Restore row boundaries only inside sections with a Markdown table delimiter."""

    table = _FLAT_TABLE.search(source)
    while table:
        section_end = source.find("\n\n#", table.end())
        if section_end < 0:
            section_end = len(source)
        section = source[table.start() : section_end].replace("||", "|\n|")
        source = source[: table.start()] + section + source[section_end:]
        table = _FLAT_TABLE.search(source, table.start() + len(section))
    return source
