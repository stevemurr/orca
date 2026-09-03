"""Pure rendering tests for the terminal views."""

from __future__ import annotations

from dataclasses import replace
from io import StringIO

from rich.color import Color
from rich.console import Console, RenderableType

from orca.app.model import (
    AppState,
    Choice,
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
    render_interaction,
    render_plan,
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
        answer="# Findings\n\n- One result",
        provisional_answer="",
        status="completed",
    )
    renderable = render_conversation(replace(state, turns=(turn,)), width=90)

    segments = Console(force_terminal=True, color_system="truecolor").render(renderable)
    heading = next(segment for segment in segments if segment.text.strip() == "Findings")

    assert heading.style is not None
    assert heading.style.bold is True
    assert heading.style.color == Color.parse(ACCENT)


def _planned() -> AppState:
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
    return replace(state, turns=(turn,))


def test_the_plan_is_pinned_above_the_composer_while_the_run_goes() -> None:
    """The reason the event exists: a person watching should see where the model is, and
    not have to scroll for it. While the run goes the checklist lives in its own strip
    above the composer, not in the transcript; the active step is the one line not muted."""

    state = _planned()

    pinned = render_plan(state, width=90)
    assert pinned is not None
    strip = plain(pinned, width=90)
    transcript = plain(render_conversation(state, width=90), width=90)

    assert "the old handler was already gone" in strip
    assert "✓  read the router" in strip
    assert "▸  add the handler" in strip
    assert "○  run the suite" in strip
    assert "read the router" not in transcript


def test_a_finished_turn_keeps_its_plan_in_the_transcript_above_the_activity() -> None:
    state = replace(_planned(), run_status=RunStatus.COMPLETED, active_run_id=None)

    assert render_plan(state, width=90) is None
    rendered = plain(render_conversation(state, width=90), width=90)
    plan_line = rendered.index("read the router")
    assert rendered.index("Build a polished terminal interface") < plan_line
    assert plan_line < rendered.index("Building the terminal shell")


def test_a_status_this_client_has_never_heard_of_still_shows_its_step() -> None:
    """Statuses are an open vocabulary. Dropping the row would hide work the model named."""

    state = replace(populated_state(), run_status=RunStatus.COMPLETED, active_run_id=None)
    turn = replace(state.turns[-1], plan=(PlanStep("something new", "invented"),))
    rendered = plain(render_conversation(replace(state, turns=(turn,)), width=90), width=90)

    assert "○  something new" in rendered


def test_the_active_turn_shows_a_spinner_and_how_long_it_has_run() -> None:
    from orca.app.model import Usage

    state = replace(populated_state(), run_started_at=100.0, clock=175.0)

    rendered = plain(render_conversation(state, width=90), width=90)
    footer = plain(
        render_footer(replace(state, usage=Usage(12_300, 262_144, estimated=True))), width=90
    )

    assert "Running · 1m 15s" in rendered
    assert "━━━━━━━━━━  ≈4% of 262.1k" in footer


def test_the_footer_names_the_folder_and_what_the_conversation_reaches_beyond_it() -> None:
    state = replace(populated_state(), folders=("/Users/murr/Code/orca", "/srv/lib"))

    rendered = plain(render_footer(state), width=100)

    assert "~/Code/orca + orca, lib   normal · ask" in rendered
    assert "~/Code/orca" not in plain(render_header(state, width=100), width=100)


def test_a_live_turn_splits_where_its_last_tool_group_begins() -> None:
    """The head keeps its key across ticks; the tail holds the shining group and the
    spinner, so a tick renders those lines and not the code above them."""
    from orca.app.model import Activity, Narration
    from orca.tui.render.conversation import render_live_turn

    turn = TurnState(
        "run-1",
        request="Fix it",
        progress=(
            ProgressItem("a", "read x", "completed", kind="read"),
            ProgressItem("b", "edit x", "active", kind="edit"),
        ),
        timeline=(Narration("First, a look."), Activity("a"), Narration("Now."), Activity("b")),
    )
    state = replace(populated_state(), turns=(turn,), active_run_id="run-1", clock=1.0)

    (key, head), (tail_key, tail) = render_live_turn(state, turn, width=90)
    head_text, tail_text = plain(head, width=90), plain(tail, width=90)
    assert "First, a look." in head_text and "read x" in head_text and "Now." in head_text
    assert "edit x" in tail_text and "Running" in tail_text
    assert "edit x" not in head_text and "Running" not in head_text
    assert tail_key is None

    later = replace(state, clock=2.0)
    assert render_live_turn(later, turn, width=90)[0][0] == key

    # Words being written at the end are a piece of their own: a delta changes its key
    # and leaves the head's alone, so the code above it is not drawn again for a word.
    writing = replace(turn, timeline=(*turn.timeline, Narration("Done so")))
    (head_key, _), (stream_key, stream), (_, _) = render_live_turn(state, writing, width=90)
    assert "Done so" in plain(stream, width=90)
    more = replace(turn, timeline=(*turn.timeline, Narration("Done so far.")))
    (head_again, _), (stream_again, _), _ = render_live_turn(state, more, width=90)
    assert head_again == head_key
    assert stream_again != stream_key


def test_the_command_menu_slides_to_keep_the_highlighted_row_in_view() -> None:
    from orca.app.commands import suggest
    from orca.tui.render import render_command_menu

    rows = suggest("/", skills=(Choice("deploy", "Ship a release."),))
    assert len(rows) > 9

    top = plain(render_command_menu(rows, 0), width=90)
    assert "/chat" in top and "/deploy" not in top
    assert "below" in top

    last = plain(render_command_menu(rows, len(rows) - 1), width=90)
    deploy = next(line for line in last.splitlines() if "/deploy" in line)
    assert deploy.strip().startswith("›") and "/chat" not in last
    assert "above" in last


def test_a_tool_row_reads_as_prose_with_its_argument_beside_it_in_a_card() -> None:
    turn = TurnState(
        "run-1",
        request="Look",
        progress=(
            ProgressItem("r", "read src/app.py", "completed", kind="read", tool="read_file"),
            ProgressItem(
                "x", "run: ls", "completed", kind="execute", tool="run", detail="uv run pytest -q"
            ),
            ProgressItem("m", "files__list {}", "completed", kind="other"),
            ProgressItem("k", "skill: deploy", "completed", kind="skill", tool="use_skill"),
        ),
    )
    state = replace(populated_state(), turns=(turn,), tools_expanded=True)

    rendered = plain(render_conversation(state, width=90), width=90)

    assert "ReadFile" in rendered
    assert "◈  UseSkill" in rendered
    assert "›_ Run uv run pytest -q" in rendered
    # No tool named: the backend's own summary, as before.
    assert "files__list {}" in rendered
    # The card's rounded corners.
    assert "╭" in rendered and "╰" in rendered


def test_a_call_that_wrote_code_stays_in_the_fold_with_its_code() -> None:
    from orca.app.model import Snippet

    turn = TurnState(
        "run-1",
        request="Write it",
        progress=(
            ProgressItem("r", "read app.py", "completed", kind="read"),
            ProgressItem(
                "w",
                "write app.py",
                "completed",
                (Snippet("app.py", "python", "print('hi')\n"),),
                kind="edit",
            ),
            ProgressItem("t", "run: pytest", "completed", kind="execute"),
        ),
    )
    state = replace(populated_state(), turns=(turn,))

    rendered = plain(render_conversation(state, width=90), width=90)

    # The read folds away; the write stays with its code, and the latest call carries the
    # count below it.
    assert "read app.py" not in rendered
    assert "write app.py" in rendered
    assert "print('hi')" in rendered
    assert "run: pytest  ·  3 tool calls ›" in rendered
    assert rendered.index("print('hi')") < rendered.index("run: pytest")
    assert rendered.count("tool calls ›") == 1


def test_the_transcript_shows_a_tool_call_where_it_happened() -> None:
    from orca.app.model import Activity, Narration

    turn = TurnState(
        "run-1",
        request="Fix it",
        progress=(ProgressItem("ls", "run: ls", "completed"),),
        provisional_answer="Looking first.\n\nNow the fix.",
        timeline=(Narration("Looking first."), Activity("ls"), Narration("\n\nNow the fix.")),
    )
    state = replace(populated_state(), turns=(turn,))

    rendered = plain(render_conversation(state, width=90), width=90)

    first = rendered.index("Looking first.")
    call = rendered.index("run: ls")
    second = rendered.index("Now the fix.")
    assert first < call < second


def test_an_approval_shows_the_code_it_would_write() -> None:
    from orca.app.model import InteractionState, Snippet

    state = replace(
        populated_state(),
        interaction=InteractionState(
            kind="approval",
            request_id="a1",
            title="write src/app.py (20 bytes)",
            allowed_decisions=("approve", "reject"),
            snippets=(Snippet("src/app.py", "", "def greet():\n    return 'hi'\n"),),
        ),
    )

    rendered = plain(render_interaction(state, width=80), width=80)

    assert "src/app.py" in rendered
    assert "def greet():" in rendered
    assert "return 'hi'" in rendered


def test_the_transcript_shows_the_code_a_call_wrote_under_its_row() -> None:
    from orca.app.model import Snippet

    turn = TurnState(
        "run-1",
        request="Write it",
        progress=(
            ProgressItem(
                "w1",
                "write src/app.py (30 bytes)",
                "completed",
                snippets=(Snippet("src/app.py", "", "def greet():\n    return 'hi'\n"),),
            ),
        ),
        answer="Written.",
    )
    state = replace(populated_state(), turns=(turn,))

    rendered = plain(render_conversation(state, width=90), width=90)

    row = rendered.index("write src/app.py")
    code = rendered.index("def greet():")
    answer = rendered.index("Written.")
    assert row < code < answer


def test_each_kind_of_tool_has_its_own_quiet_glyph() -> None:
    turn = TurnState(
        "run-1",
        request="Look, then change",
        progress=(
            ProgressItem("r", "read src/app.py", "completed", kind="read"),
            ProgressItem("e", "edit src/app.py", "active", kind="edit"),
            ProgressItem("x", "run: pytest", "failed", kind="execute"),
            ProgressItem("m", "files__list", "completed", kind="other"),
        ),
    )
    state = replace(populated_state(), turns=(turn,), tools_expanded=True)

    rendered = plain(render_conversation(state, width=90), width=90)

    assert "≡  read src/app.py" in rendered
    assert "✎  edit src/app.py" in rendered
    assert "›_ run: pytest" in rendered
    assert "·  files__list" in rendered


def test_a_run_of_tool_calls_folds_to_the_latest_and_a_count() -> None:
    from orca.app.model import Activity

    calls = tuple(
        ProgressItem(f"c{n}", f"read file{n}.py", "completed", kind="read") for n in range(8)
    )
    turn = TurnState(
        "run-1",
        request="Read them",
        progress=calls,
        timeline=tuple(Activity(item.update_id) for item in calls),
    )
    folded = replace(populated_state(), turns=(turn,))
    opened = replace(folded, tools_expanded=True)

    quiet = plain(render_conversation(folded, width=90), width=90)
    loud = plain(render_conversation(opened, width=90), width=90)

    assert "read file7.py  ·  8 tool calls ›" in quiet
    assert quiet.count("tool calls ›") == 1
    assert "read file0.py" not in quiet
    assert "read file0.py" in loud and "read file7.py" in loud
    assert "tool calls ›" not in loud


def test_a_notice_shows_for_a_moment_and_then_goes() -> None:
    from orca.app.model import Notice
    from orca.tui.render import render_notice

    said = replace(populated_state(), notices=(Notice("Approved: run: pytest", shown_at=10.0),))

    fresh = render_notice(replace(said, clock=11.0))
    stale = render_notice(replace(said, clock=13.5))

    assert fresh is not None and "Approved: run: pytest" in plain(fresh, width=90)
    assert stale is None


def test_the_working_tool_row_carries_a_shine_that_moves_with_the_clock() -> None:
    from orca.tui.render.conversation import shimmer

    early = shimmer("read src/app.py", 0.3)
    later = shimmer("read src/app.py", 0.5)

    assert early.plain == later.plain == "read src/app.py"
    lit_early = {span.start for span in early.spans if ACCENT in str(span.style)}
    lit_later = {span.start for span in later.spans if ACCENT in str(span.style)}
    assert lit_early and lit_later and lit_early != lit_later


def test_the_latest_group_of_a_working_turn_shines_between_calls() -> None:
    from orca.app.model import Activity, Narration
    from orca.tui.render.conversation import _activity_table  # pyright: ignore[reportPrivateUsage]

    calls = tuple(
        ProgressItem(f"c{n}", f"read file{n}.py", "completed", kind="read") for n in range(3)
    )
    state = replace(populated_state(), clock=0.3)

    live = _activity_table(list(calls), state, live=True)
    still = _activity_table(list(calls), state, live=False)
    finished = _activity_table(
        list(calls), replace(state, run_status=RunStatus.COMPLETED, active_run_id=None), live=False
    )

    def shines(renderable: RenderableType) -> bool:
        console = Console(force_terminal=True, color_system="truecolor", width=90)
        return any(
            segment.style is not None
            and segment.style.color is not None
            and segment.style.color == Color.parse(ACCENT)
            for segment in console.render(renderable)
        )

    assert shines(live)
    assert not shines(still)
    assert not shines(finished)

    # In the transcript: the group at the end of the working turn, and nothing after it.
    turn = TurnState(
        "run-1",
        request="Read them",
        progress=calls,
        timeline=(Narration("Looking."), *(Activity(c.update_id) for c in calls)),
    )
    rendered = plain(render_conversation(replace(state, turns=(turn,)), width=90), width=90)
    assert "read file2.py  ·  3 tool calls ›" in rendered


def test_an_approval_is_asked_at_the_end_of_its_turn_and_leaves_once_decided() -> None:
    from orca.app.model import InteractionState

    state = replace(
        populated_state(),
        run_status=RunStatus.AWAITING_APPROVAL,
        interaction=InteractionState(
            kind="approval",
            request_id="a1",
            title="Run the tests?",
            command="pytest -q",
            allowed_decisions=("approve", "approve_bash_always", "reject"),
        ),
    )
    asked = plain(render_conversation(state, width=90), width=90)
    assert asked.index("Building the terminal shell") < asked.index("Run the tests?")
    assert "$ pytest -q" in asked
    assert "Approve once" in asked and "Always allow" in asked and "Reject" in asked

    decided = replace(state, interaction=None, run_status=RunStatus.RUNNING)
    rendered = plain(render_conversation(decided, width=90), width=90)
    assert "Run the tests?" not in rendered
    assert "Approve once" not in rendered


def test_a_message_sent_mid_run_is_quoted_in_the_transcript_where_it_arrived() -> None:
    from orca.app.model import Activity, Narration, TurnNote

    turn = TurnState(
        "run-1",
        request="Fix the parser",
        progress=(ProgressItem("ls", "read parser.py", "completed", kind="read"),),
        timeline=(
            Narration("Reading it."),
            Activity("ls"),
            TurnNote("steer", "Use the **new** tokenizer, not the old one."),
            Narration("\n\nSwitching."),
        ),
    )
    state = replace(populated_state(), turns=(turn,))

    rendered = plain(render_conversation(state, width=90), width=90)

    assert "▎ Use the new tokenizer" in rendered
    assert "mid-run" not in rendered
    steer = rendered.index("Use the new tokenizer")
    assert rendered.index("read parser.py") < steer < rendered.index("Switching.")
    # A blank line between the steer and the words that answer it, as between any two
    # blocks of words; the tool rows above it bring their own.
    lines = [line.rstrip() for line in rendered.splitlines()]
    at = next(index for index, line in enumerate(lines) if "Use the new tokenizer" in line)
    assert lines[at + 1] == ""
    assert "Switching." in lines[at + 2]


def test_the_agents_view_and_strip_show_a_delegated_agent() -> None:
    from orca.app.model import AgentState, ProgressItem, TurnState
    from orca.tui.render import render_agents, render_conversation

    agent = AgentState(
        agent_id="agent_1",
        task="search the web for the hang",
        started_at=10.0,
        progress=(
            ProgressItem(
                "c1",
                "search",
                "active",
                tool="web_search",
                detail="xctest hang",
                agent_id="agent_1",
            ),
        ),
        said=("Let me search for that.",),
    )
    turn = TurnState(run_id="run-1", request="find the hang", agents=(agent,))
    state = AppState(turns=(turn,), active_run_id="run-1", run_status=RunStatus.RUNNING, clock=25.0)

    strip = plain(render_conversation(state, width=100), width=100)
    view = plain(render_agents(state, width=100), width=100)

    assert "agent_1" in strip and "search the web for the hang" in strip and "15s" in strip
    assert "WebSearch xctest hang" in strip
    assert "agent_1" in view and "running" in view and "Let me search for that." in view
    assert "xctest hang" in view
