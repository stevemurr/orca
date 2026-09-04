"""Focused coverage for terminal Markdown normalization and layout."""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from orca.tui.render.markdown import answer_markdown, recover_flattened_markdown


@pytest.mark.parametrize(
    ("source", "expected_blocks"),
    (
        (
            "## Findings- first item.- second item.",
            ("## Findings\n\n", "- first item.\n- second item."),
        ),
        (
            "# Report## Findings- first item.- second item.",
            ("# Report\n\n## Findings\n\n", "- first item.\n- second item."),
        ),
        (
            "## Steps1. first item. 2. second item. 3. third item.",
            ("## Steps\n\n", "1. first item.\n2. second item.\n3. third item."),
        ),
        (
            "## Files| Name | Purpose ||---|---||`a.py`| app ||`b.py`| tests",
            (
                "## Files\n\n| Name | Purpose |\n|---|---|",
                "|`a.py`| app |\n|`b.py`| tests",
            ),
        ),
        (
            "| Name | Purpose ||---|---||`a.py`| app ||`b.py`| tests",
            ("| Name | Purpose |\n|---|---|", "|`a.py`| app |\n|`b.py`| tests"),
        ),
        (
            "- **One**: first.- **Two**: second.",
            ("- **One**: first.\n- **Two**: second.",),
        ),
        (
            "1. first. 2. second. 3. third.",
            ("1. first.\n2. second.\n3. third.",),
        ),
        (
            "> first quote.> second quote.",
            ("> first quote.\n> second quote.",),
        ),
        (
            "* first item.* second item.",
            ("* first item.\n* second item.",),
        ),
        (
            "+ [ ] first task.+ [x] second task.",
            ("+ [ ] first task.\n+ [x] second task.",),
        ),
    ),
)
def test_recovers_unmistakable_single_line_markdown_blocks(
    source: str,
    expected_blocks: tuple[str, ...],
) -> None:
    recovered = recover_flattened_markdown(source)

    for block in expected_blocks:
        assert block in recovered


def test_recovery_does_not_treat_inline_code_as_block_markup() -> None:
    source = (
        "# Report`literal ## heading - item | a || b` is documented.## ResultThe answer is intact."
    )

    recovered = recover_flattened_markdown(source)

    assert "`literal ## heading - item | a || b`" in recovered
    assert "\n\n## Result\n\nThe answer is intact." in recovered


def test_table_recovery_does_not_split_double_pipes_inside_inline_code() -> None:
    source = "## Expressions| Value | Meaning ||---|---||`a || b`| Boolean OR."

    recovered = recover_flattened_markdown(source)

    assert "`a || b`" in recovered
    assert "`a |\n| b`" not in recovered


def test_recovery_preserves_list_indentation() -> None:
    source = "## Items- parent.  - child.- sibling."

    recovered = recover_flattened_markdown(source)

    assert "- parent.\n   - child.\n- sibling." in recovered


def test_recovery_does_not_split_an_ordinary_heading_phrase() -> None:
    source = "# Report## Why It works"

    recovered = recover_flattened_markdown(source)

    assert recovered == "# Report\n\n## Why It works"


def test_trailing_newline_does_not_disable_flattened_block_recovery() -> None:
    source = "## Findings- first item.- second item.\n"

    recovered = recover_flattened_markdown(source)

    assert recovered == "## Findings\n\n- first item.\n- second item.\n"


def test_recovery_repairs_a_flattened_line_inside_valid_multiline_markdown() -> None:
    source = (
        "# Report\n\n"
        "A valid introductory paragraph.\n\n"
        "## Findings- first item.- second item.## End\n"
    )

    recovered = recover_flattened_markdown(source)

    assert "A valid introductory paragraph.\n\n" in recovered
    assert "## Findings\n\n- first item.\n- second item.\n\n## End\n" in recovered


def test_recovery_is_idempotent() -> None:
    source = "# Report## Findings- first item.- second item."

    recovered = recover_flattened_markdown(source)

    assert recover_flattened_markdown(recovered) == recovered


def test_valid_multiline_markdown_is_never_rewritten() -> None:
    source = (
        "## Findings\n\n"
        "- parent\n"
        "  - child\n\n"
        "```python\n"
        "## Literal1. not a list.- not a bullet. | x || y\n"
        "```\n\n"
        "> quoted **text**"
    )

    assert recover_flattened_markdown(source) == source


def test_plain_single_line_prose_with_markdown_characters_is_not_rewritten() -> None:
    source = "Use C# ## syntax literally; x || y is also literal here."

    assert recover_flattened_markdown(source) == source


@pytest.mark.parametrize("width", (24, 40, 80, 120))
def test_rich_markdown_never_exceeds_the_requested_console_width(width: int) -> None:
    source = "\n".join(
        (
            "# Rendering matrix",
            "",
            "A long token: " + "a" * 160,
            "",
            "| Path | Description |",
            "|---|---|",
            "| `src/with/a/very/long/path.py` | A deliberately long description that must fold. |",
            "",
            "1. ordered",
            "2. ordered again",
        )
    )
    stream = StringIO()
    console = Console(file=stream, width=width, color_system=None)

    console.print(answer_markdown(source))

    assert max(map(len, stream.getvalue().splitlines())) <= width


def test_a_fence_with_an_info_string_does_not_close_an_open_block() -> None:
    """CommonMark: a closing fence has nothing after it but space. A "```python" line
    inside a "```" block is code, and used to close it, so the block's rest was rewritten
    as flattened prose."""
    source = "```\nline\n```python\n# H1 text. 1. a. 2. b\n```\n"

    assert recover_flattened_markdown(source) == source

    nested = "````md\nexample:\n```python\nprint(1)\n```\n````\n\n# A\n1. one. 2. two. 3. three\n"
    recovered = recover_flattened_markdown(nested)
    assert recovered.startswith("````md\nexample:\n```python\nprint(1)\n```\n````\n")
    assert "1. one.\n2. two.\n3. three" in recovered
