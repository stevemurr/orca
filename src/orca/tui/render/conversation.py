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
    Notice,
    ProgressItem,
    Segment,
    TurnNote,
    TurnState,
)
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
        if turn.plan:
            # Intent frames activity; the active step is the checklist's only prominent line.
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
                rows.append(Padding(_activity_table(pending), (0, 1)))
                pending = []
            if isinstance(segment, Narration):
                if not narrated:
                    rows.append(Rule(style=CALLOUT))
                narrated = True
                rows.append(Padding(answer_markdown(segment.text), (0, 1)))
            elif isinstance(segment, TurnNote):
                rows.append(Padding(_note_row(segment), (0, 1)))
        if not narrated and turn.status in {"queued", "running"}:
            rows.append(Padding(Text("● Working", style=f"bold {ACCENT}"), (0, 1)))
        for artifact in turn.artifacts:
            label = artifact.reference or artifact.artifact_id
            rows.append(
                Padding(
                    Text.assemble(("◇ ", ACCENT), (artifact.title, "bold"), (f"  {label}", MUTED)),
                    (0, 1),
                )
            )
    if state.notices:
        rows.append(Text(""))
        rows.extend(notice_row(notice) for notice in state.notices[-2:])
    return Group(*rows) if rows else Text("")


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


def _activity_table(items: list[ProgressItem]) -> RenderableType:
    activity = Table.grid(padding=(0, 1))
    activity.add_column(width=2, no_wrap=True)
    activity.add_column(ratio=1, overflow="fold")
    for item in items:
        glyph, style = _progress_glyph(item.status)
        activity.add_row(Text(glyph, style=style), Text(item.text, style=MUTED))
    return activity


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


def _progress_glyph(status: str) -> tuple[str, str]:
    normalized = status.lower()
    if normalized == "completed":
        return "✓", SUCCESS
    if normalized == "failed":
        return "×", ERROR
    return "●", ACCENT


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
    glyph = {"compaction": "⇥", "steer": "↳", "folder": "+"}[note.kind]
    return Text.assemble((f"{glyph} ", ACCENT), (note.text, f"italic {MUTED}"))


def notice_row(notice: Notice) -> Text:
    style = {"info": MUTED, "warning": WARNING, "error": ERROR}[notice.level]
    return Text(f"· {notice.message}", style=style)
