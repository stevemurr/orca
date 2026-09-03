"""Conversation and review renderers for canonical turn state."""

from __future__ import annotations

from rich import box
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from orca.app.model import (
    Activity,
    AppState,
    Narration,
    ProgressItem,
    Segment,
    TurnNote,
    TurnState,
)
from orca.tui.render.code import code_block
from orca.tui.render.markdown import answer_markdown
from orca.tui.render.theme import ACCENT, CALLOUT, ERROR, MUTED, SUCCESS, WARNING


def render_conversation(state: AppState, *, width: int) -> RenderableType:
    rows: list[RenderableType] = []
    if not state.turns:
        rows.extend(welcome(state, width=width))
    for turn_index, turn in enumerate(state.turns):
        if turn_index:
            rows.append(Text(""))
        request = Text(turn.request or "Submitting…")
        rows.append(
            Panel(
                request,
                title=Text("YOU", style=f"bold {ACCENT}"),
                title_align="left",
                border_style=CALLOUT,
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        if turn.plan and not _pinned(state, turn):
            # Intent frames activity; the active step is the checklist's only prominent line.
            # The active turn's plan is pinned above the composer instead, by `render_plan`.
            rows.append(Padding(_plan_checklist(turn), (0, 1)))
        by_id = {item.update_id: item for item in turn.progress}
        narrated = False
        pending: list[ProgressItem] = []
        for segment in (*_timeline(turn), None):
            if isinstance(segment, Activity):
                if (item := by_id.get(segment.update_id)) is not None:
                    pending.append(item)
                continue
            if pending:
                # Consecutive rows share one table so their glyphs line up.
                # A blank line either side: rows sit between paragraphs of the model's
                # own words, and against them they read as part of the sentence.
                # The group at the end of a working turn is what the agent is on now,
                # between one call and the next: it shines like a running call does.
                tail = segment is None and turn.run_id == state.active_run_id and state.working
                rows.append(Padding(_activity_table(pending, state, live=tail), (1, 1)))
                pending = []
            if isinstance(segment, Narration):
                if not narrated:
                    rows.append(Rule(style=CALLOUT))
                narrated = True
                rows.append(Padding(answer_markdown(segment.text), (0, 1)))
            elif isinstance(segment, TurnNote):
                rows.append(Padding(_note_row(segment), (0, 1)))
        if turn.run_id == state.active_run_id and state.working:
            rows.append(Padding(_working(state), (1, 1, 0, 1)))
        for artifact in turn.artifacts:
            label = artifact.reference or artifact.artifact_id
            rows.append(
                Padding(
                    Text.assemble(("◇ ", ACCENT), (artifact.title, "bold"), (f"  {label}", MUTED)),
                    (0, 1),
                )
            )
    return Group(*rows) if rows else Text("")


def render_plan(state: AppState, *, width: int) -> RenderableType | None:
    """The active turn's checklist, for the strip above the composer. None when there is no
    run going or it has no plan."""
    del width
    if not state.turns:
        return None
    turn = state.turns[-1]
    if not _pinned(state, turn):
        return None
    return Group(Text("PLAN", style=f"bold {MUTED}"), _plan_checklist(turn))


def _pinned(state: AppState, turn: TurnState) -> bool:
    return bool(turn.plan) and state.working and turn.run_id == state.active_run_id


#: A spinner that advances with the clock: one frame per tick of the host's timer.
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: How far the shine on a working tool row moves per second, in cells, and how wide it is.
_SHINE_SPEED = 24.0
_SHINE_WIDTH = 7


def shimmer(text: str, clock: float) -> Text:
    """The text with a band of light sweeping across it, placed by the clock.

    The row a person is watching, lit the way an editor's agent lights the call it is on:
    the same words, muted, with a few brighter cells that move left to right and wrap. Pure
    in the clock, so a still frame is a still frame and a test can pin one.
    """
    line = Text(text, style=MUTED)
    if not text:
        return line
    span = len(text) + _SHINE_WIDTH
    head = int(clock * _SHINE_SPEED) % span - _SHINE_WIDTH
    for offset in range(_SHINE_WIDTH):
        cell = head + offset
        if 0 <= cell < len(text):
            # Brightest at the centre of the band, accent at its edges.
            centre = abs(offset - _SHINE_WIDTH // 2)
            line.stylize("bold white" if centre <= 1 else f"bold {ACCENT}", cell, cell + 1)
    return line


def _working(state: AppState) -> Text:
    frame = _FRAMES[int(state.clock * 2) % len(_FRAMES)]
    label = state.run_status.value.replace("_", " ").capitalize()
    text = Text.assemble((f"{frame} ", f"bold {ACCENT}"), (label, f"bold {ACCENT}"))
    if state.run_started_at > 0:
        text.append(f" · {_elapsed(max(0.0, state.clock - state.run_started_at))}", style=MUTED)
    return text


def _elapsed(seconds: float) -> str:
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m {whole % 60:02d}s"
    return f"{whole // 3600}h {(whole % 3600) // 60:02d}m"


def render_review(state: AppState, *, width: int) -> RenderableType:
    del width
    if not state.turns:
        return Group(
            Text("Review", style=f"bold {ACCENT}"),
            Text(""),
            Text("No completed turn is available yet.", style=MUTED),
        )
    turn = state.turns[-1]
    rows: list[RenderableType] = [Text("Review", style=f"bold {ACCENT}"), Text("")]
    if turn.answer:
        rows.extend((Text("RESULT", style=f"bold {MUTED}"), answer_markdown(turn.answer)))
    else:
        rows.append(Text("The current answer is still provisional.", style=MUTED))
    if turn.artifacts:
        rows.extend((Text(""), Text("ARTIFACTS", style=f"bold {MUTED}")))
        artifacts = Table.grid(padding=(0, 2))
        artifacts.add_column(no_wrap=True, style=ACCENT)
        artifacts.add_column(ratio=1)
        for artifact in turn.artifacts:
            artifacts.add_row(artifact.kind, artifact.reference or artifact.artifact_id)
        rows.append(artifacts)
    return Group(*rows)


def _timeline(turn: TurnState) -> tuple[Segment, ...]:
    """The turn in arrival order, or the older shape -- every row, then the answer -- for a
    turn built without one."""
    if turn.timeline:
        return turn.timeline
    answer = turn.answer or turn.provisional_answer
    rows: tuple[Segment, ...] = tuple(Activity(item.update_id) for item in turn.progress)
    return (*rows, *turn.notes, *((Narration(answer),) if answer else ()))


def _activity_table(
    items: list[ProgressItem], state: AppState, *, live: bool = False
) -> RenderableType:
    """A run of tool calls. Folded, it is the latest call and a count -- the one a person
    is watching, and how much came before it; `/tools` or Ctrl+T shows them all. `live`
    says the group is the working turn's latest, so its last row shines even between
    calls."""
    activity = Table.grid(padding=(0, 1))
    activity.add_column(width=2, no_wrap=True)
    activity.add_column(ratio=1, overflow="fold")
    shown = items if state.tools_expanded or len(items) == 1 else items[-1:]
    folded = len(items) - len(shown)
    for item in shown:
        glyph, style, text_style = _activity_look(item)
        count = f"  ·  {folded + 1} tool calls ›" if folded else ""
        running = item.status.lower() == "active" and state.working
        if running or (live and item is shown[-1] and item.status.lower() != "failed"):
            # The whole line shines, count included: folded, the count is part of what a
            # person is watching.
            line = shimmer(item.text + count, state.clock)
        else:
            line = Text(item.text, style=text_style)
            line.append(count, style=MUTED)
        activity.add_row(Text(glyph, style=style), line)
        for snippet in item.snippets:
            # The code under its row, the way an editor's transcript shows a write.
            activity.add_row(Text(""), code_block(snippet, lines=_TRANSCRIPT_LINES))
    return activity


#: Lines of a written file shown in the transcript. Enough to read what was written; a
#: whole file is the file's job.
_TRANSCRIPT_LINES = 40


def _plan_checklist(turn: TurnState) -> RenderableType:
    """Render the working model's own step list as a checklist."""

    checklist = Table.grid(padding=(0, 1))
    checklist.add_column(width=2, no_wrap=True)
    checklist.add_column(ratio=1, overflow="fold")
    if turn.plan_explanation:
        checklist.add_row(Text(" "), Text(turn.plan_explanation, style=f"italic {MUTED}"))
    for step in turn.plan:
        glyph, style, text_style = _plan_glyph(step.status)
        checklist.add_row(Text(glyph, style=style), Text(step.step, style=text_style))
    return checklist


def _plan_glyph(status: str) -> tuple[str, str, str]:
    """Return glyph, glyph style, and text style for an open status vocabulary."""

    if status == "completed":
        return "✓", SUCCESS, MUTED
    if status == "in_progress":
        return "▸", ACCENT, f"bold {ACCENT}"
    return "○", MUTED, MUTED


#: One quiet glyph per kind of tool, so a read, a search, a write and a command tell apart
#: at a glance. The kind is the backend's word; one it has not said gets the plain mark.
_KIND_GLYPHS = {
    "read": "≡",
    "search": "⌕",
    "edit": "✎",
    "execute": "$",
    "fetch": "◎",
    "think": "✦",
    "switch_mode": "⇄",
}


def _activity_look(item: ProgressItem) -> tuple[str, str, str]:
    """Glyph, glyph style and text style. The kind picks the glyph; the status, the colour --
    working is bright, done is quiet, and a failure is the one row that is not."""
    glyph = _KIND_GLYPHS.get(item.kind, "·")
    status = item.status.lower()
    if status == "failed":
        return glyph, ERROR, ERROR
    if status == "completed":
        return glyph, MUTED, MUTED
    return glyph, f"bold {ACCENT}", MUTED


def welcome(state: AppState, *, width: int) -> list[RenderableType]:
    title = Text()
    title.append("›_ ", style=f"bold {ACCENT}")
    title.append("Ready", style="bold")
    details = Table.grid(padding=(0, 2))
    details.add_column(width=11, style=MUTED, no_wrap=True)
    details.add_column(ratio=1, overflow="fold")
    details.add_row("workspace", state.workspace_path or "resolving…")
    for folder in state.folders:
        details.add_row("folder", folder)
    details.add_row("connection", state.endpoint or "resolving…")
    details.add_row("session", f"{state.mode} · {state.policy}")
    panel = Panel(
        Group(title, Text(""), details),
        box=box.ROUNDED,
        border_style=CALLOUT,
        padding=(0, 1),
        width=max(1, width),
    )
    tip = Text.assemble(("Tip: ", MUTED), ("/help", ACCENT), (" lists every command", MUTED))
    return [panel, tip]


def _note_row(note: TurnNote) -> Text:
    glyph = {"compaction": "⇥", "steer": "↳", "folder": "+", "ended": "■"}[note.kind]
    style = WARNING if note.kind == "ended" else ACCENT
    return Text.assemble((f"{glyph} ", style), (note.text, f"italic {MUTED}"))
