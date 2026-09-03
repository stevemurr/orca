"""Pure rendering tests for the terminal views."""

from __future__ import annotations

from dataclasses import replace
from io import StringIO

from rich.color import Color
from rich.console import Console

from orca.app.model import (
    AppState,
    PlanStep,
    ProgressItem,
    RunStatus,
    TurnState,
    ViewId,
)
from orca.tui.render import (
    render_conversation,
    render_footer,
    render_header,
    render_review,
)
from orca.tui.render.theme import ACCENT


def plain(renderable: object, *, width: int) -> str:
    output = StringIO()
    Console(file=output, width=width, color_system=None, force_terminal=False).print(renderable)
    return output.getvalue()


def populated_state() -> AppState:
    return AppState(
        booting=False,
        connected=True,
        endpoint="http://127.0.0.1:8420",
        workspace_path="~/Code/orca",
        run_status=RunStatus.RUNNING,
        turns=(
            TurnState(
                "run-1",
                request="Build a polished terminal interface",
                progress=(ProgressItem("shell", "Building the terminal shell.", "active"),),
                provisional_answer="The view architecture is taking shape.",
            ),
        ),
        active_run_id="run-1",
    )


def test_header_stays_quiet_and_names_exact_context() -> None:
    rendered = plain(render_header(populated_state(), width=100), width=100)

    assert "orca" in rendered
    assert "~/Code/orca" in rendered
    assert "running" in rendered


def test_conversation_preserves_semantic_order() -> None:
    rendered = plain(render_conversation(populated_state(), width=90), width=90)

    request = rendered.index("Build a polished terminal interface")
    progress = rendered.index("Building the terminal shell")
    answer = rendered.index("The view architecture is taking shape")
    assert request < progress < answer


def test_footer_stays_on_one_line_at_every_viewport_width() -> None:
    state = populated_state()

    for viewport_width in (1, 2, 5, 10, 16, 24, 40, 80):
        for view in ViewId:
            stack = (view,) if view is ViewId.CONVERSATION else (ViewId.CONVERSATION, view)
            candidate = replace(state, viewport_width=viewport_width, view_stack=stack)
            rendered = plain(render_footer(candidate), width=max(1, viewport_width))
            lines = rendered.splitlines()

            assert len(lines) == 1, (viewport_width, view, lines)
            assert len(lines[0]) <= max(1, viewport_width - 2)


def test_header_stays_on_one_line_with_a_long_workspace() -> None:
    state = replace(
        populated_state(),
        workspace_path="~/Code/clients/a-very-long-workspace-name/with/a/deep/subdirectory",
        profile="a-very-long-profile-name",
    )

    for width in (16, 24, 40, 80, 120):
        rendered = plain(render_header(state, width=width), width=width)
        lines = rendered.splitlines()

        assert len(lines) == 1, (width, lines)
        assert len(lines[0]) <= width


def test_review_uses_terminal_answer_not_provisional_stream() -> None:
    state = populated_state()
    turn = replace(
        state.turns[-1],
        answer="Implemented and verified the interface.",
        provisional_answer="discarded partial answer",
        status="completed",
    )
    state = replace(state, turns=(turn,), active_run_id=None, run_status=RunStatus.COMPLETED)

    rendered = plain(render_review(state, width=90), width=90)

    assert "Implemented and verified the interface" in rendered
    assert "discarded partial answer" not in rendered


def test_conversation_recovers_flattened_markdown_blocks_for_display() -> None:
    """A structured model may flatten Markdown newlines inside its JSON string result.

    The durable answer must stay byte-for-byte canonical, but the terminal still has enough
    structure to recover headings and list boundaries instead of rendering the whole response
    as one giant heading.
    """

    state = populated_state()
    flattened = (
        "## Analysis of `demo``demo` is compact."
        "### Findings"
        "1. **Storage**: Local."
        "2. **UI**: Browser."
        "### Evidence"
        "- README: `abc`"
        "- go.mod: `def`"
    )
    turn = replace(
        state.turns[-1],
        answer=flattened,
        provisional_answer="",
        status="completed",
    )

    rendered = plain(render_conversation(replace(state, turns=(turn,)), width=90), width=90)
    lines = {line.strip() for line in rendered.splitlines()}

    assert "###" not in rendered
    assert "1 Storage: Local." in lines
    assert "2 UI: Browser." in lines
    assert "• README: abc" in lines
    assert "• go.mod: def" in lines


def test_conversation_recovers_flattened_paragraphs_tables_and_later_sections() -> None:
    """Collapsed headings and table rows remain separate Markdown blocks."""

    state = populated_state()
    flattened = (
        "# Analysis (`ref`)"
        "## Overview"
        "The repository is compact."
        "## Features"
        "- **One**: First feature."
        "- **Two**: Second feature."
        "## Files"
        "| Path | Description ||---|---||`main.py`| Entry point. ||`test.py`| Tests. "
        "## Observations"
        "- **Simple**: The filesystem is the source of truth. "
        "- **Fast**: Range requests make seeking efficient."
    )
    turn = replace(
        state.turns[-1],
        answer=flattened,
        provisional_answer="",
        status="completed",
    )

    rendered = plain(render_conversation(replace(state, turns=(turn,)), width=100), width=100)
    lines = {line.strip() for line in rendered.splitlines()}

    assert "##" not in rendered
    assert "||" not in rendered
    assert "Overview" in lines
    assert "The repository is compact." in lines
    assert "• One: First feature." in lines
    assert "• Two: Second feature." in lines
    assert "Path" in rendered and "Description" in rendered
    assert "main.py" in rendered and "Entry point." in rendered
    assert "Observations" in lines
    assert "• Simple: The filesystem is the source of truth." in lines
    assert "• Fast: Range requests make seeking efficient." in lines


def test_markdown_headings_keep_visible_semantic_styling() -> None:
    state = populated_state()
    turn = replace(
        state.turns[-1],
        answer="### Findings\n\n- One result",
        provisional_answer="",
        status="completed",
    )
    renderable = render_conversation(replace(state, turns=(turn,)), width=90)

    segments = Console(force_terminal=True, color_system="truecolor").render(renderable)
    heading = next(segment for segment in segments if segment.text.strip() == "Findings")

    assert heading.style is not None
    assert heading.style.bold is True
    assert heading.style.color == Color.parse(ACCENT)


def test_the_conversation_shows_the_plan_and_which_step_is_running() -> None:
    """The reason the event exists: a person watching should see where the model is.

    The checklist is rendered above the activity rows, so intent frames the narration, and the
    active step is the one line that is not muted.
    """

    state = populated_state()
    turn = replace(
        state.turns[-1],
        plan=(
            PlanStep("read the router", "completed"),
            PlanStep("add the handler", "in_progress"),
            PlanStep("run the suite", "pending"),
        ),
        plan_explanation="the old handler was already gone",
    )
    rendered = plain(render_conversation(replace(state, turns=(turn,)), width=90), width=90)

    assert "the old handler was already gone" in rendered
    assert "✓  read the router" in rendered
    assert "▸  add the handler" in rendered
    assert "○  run the suite" in rendered
    plan_line = rendered.index("read the router")
    assert rendered.index("Build a polished terminal interface") < plan_line
    assert plan_line < rendered.index("Building the terminal shell")


def test_a_status_this_client_has_never_heard_of_still_shows_its_step() -> None:
    """Statuses are an open vocabulary. Dropping the row would hide work the model named."""

    state = populated_state()
    turn = replace(state.turns[-1], plan=(PlanStep("something new", "invented"),))
    rendered = plain(render_conversation(replace(state, turns=(turn,)), width=90), width=90)

    assert "○  something new" in rendered
