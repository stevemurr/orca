"""Conversation and review renderers for canonical turn state."""

from __future__ import annotations

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment as Cell
from rich.table import Table
from rich.text import Text

from orca.app.model import (
    Activity,
    AgentState,
    AppState,
    Narration,
    ProgressItem,
    RunStatus,
    Segment,
    TurnNote,
    TurnState,
)
from orca.tui.render.chrome import render_interaction, settings_label
from orca.tui.render.code import code_block
from orca.tui.render.markdown import answer_markdown
from orca.tui.render.theme import ACCENT, CALLOUT, ERROR, MUTED, SUCCESS, TEXT, WARNING


def render_conversation(state: AppState, *, width: int) -> RenderableType:
    """The whole transcript, as one renderable. The host renders a turn at a time with
    `render_turn`; this is the same rows in one piece, for the plain output and for tests."""
    rows: list[RenderableType] = []
    if not state.turns:
        rows.extend(welcome(state, width=width))
    rows.extend(render_turn(state, turn, width=width) for turn in state.turns)
    return Group(*rows) if rows else Text("")


def turn_key(state: AppState, turn: TurnState, *, width: int) -> tuple[object, ...] | None:
    """What one turn's rendering depends on, for a host that keeps a turn's lines until
    they would differ.

    None for the turn a run is going in: its spinner, its shine and its answer move under
    the clock and the stream, and it is one turn. Every other turn is a function of the
    turn itself -- immutable, so identity is change -- and the little of the state it
    reads. Measured 2026-09-03: a thirty-turn transcript took 210 ms to render, and the
    host rendered it on every event and every tick of the clock, which is what typing
    into the composer was waiting behind.
    """
    if turn.run_id == state.active_run_id and (state.working or state.interaction is not None):
        return None
    return (turn, state.tools_expanded, state.working, width)


def render_turn(state: AppState, turn: TurnState, *, width: int) -> RenderableType:
    """One turn: the request, then what happened, in order."""
    head, stream, tail = _turn_rows(state, turn, width=width)
    return Group(*head, *stream, *tail)


#: One piece of a live turn: what its rendering depends on, or None for a piece that is
#: rendered every time, and the rendering.
Piece = tuple[tuple[object, ...] | None, RenderableType]


def render_live_turn(state: AppState, turn: TurnState, *, width: int) -> tuple[Piece, ...]:
    """The turn a run is going in, in pieces a host can keep or redraw one at a time.

    The head is everything settled: it changes when the stream adds a segment. The
    stream is the paragraph being written, when the turn ends in one: it changes on every
    delta, and it is the only thing that does. The tail is the group of tool calls the
    turn ends with -- the one whose last row shines -- with the spinner and the approval
    prompt under it, and it moves with the clock. Measured 2026-09-03: a tick re-rendered
    a live turn holding two code blocks, 40 ms through pygments, for a spinner frame; a
    delta did the same for a word.
    """
    head, stream, tail = _turn_rows(state, turn, width=width)
    pieces: list[Piece] = [(_head_key(state, turn, width=width), Group(*head))]
    if stream:
        pieces.append(((_timeline(turn)[_stream_start(turn) :], width), Group(*stream)))
    pieces.append((None, Group(*tail)))
    return tuple(pieces)


def _tail_start(turn: TurnState) -> int:
    """Where the tail begins: the first of the tool calls the turn ends with, or the end."""
    timeline = _timeline(turn)
    split = len(timeline)
    while split and isinstance(timeline[split - 1], Activity):
        split -= 1
    return split


def _stream_start(turn: TurnState) -> int:
    """Where the paragraph being written begins: just before the tail, when what is
    there is the model's words and nothing follows them."""
    split = _tail_start(turn)
    timeline = _timeline(turn)
    if split == len(timeline) and split and isinstance(timeline[split - 1], Narration):
        return split - 1
    return split


def _head_key(state: AppState, turn: TurnState, *, width: int) -> tuple[object, ...]:
    timeline = _timeline(turn)
    head = timeline[: _stream_start(turn)]
    shown = {segment.update_id for segment in head if isinstance(segment, Activity)}
    return (
        turn.request,
        head,
        tuple(item for item in turn.progress if item.update_id in shown),
        turn.plan,
        turn.plan_explanation,
        _pinned(state, turn),
        state.tools_expanded,
        state.working,
        width,
    )


def _turn_rows(
    state: AppState, turn: TurnState, *, width: int
) -> tuple[list[RenderableType], list[RenderableType], list[RenderableType]]:
    """The turn's rows in three lists: the head, the paragraph being written, and the
    tail that moves while a run goes. Empty lists for the pieces a turn does not have."""
    head: list[RenderableType] = []
    stream: list[RenderableType] = []
    tail: list[RenderableType] = []
    rows = head
    head.append(_request_block(turn.request or "Submitting…"))
    if turn.plan and not _pinned(state, turn):
        # Intent frames activity; the active step is the checklist's only prominent line.
        # The active turn's plan is pinned above the composer instead, by `render_plan`.
        head.append(Padding(_plan_checklist(turn), (0, 1)))
    by_id = {item.update_id: item for item in turn.progress}
    pending: list[ProgressItem] = []
    timeline = _timeline(turn)
    stream_start, tail_start = _stream_start(turn), _tail_start(turn)
    # Whether the last row was words -- the model's or the person's. Two blocks of words
    # in a row get a blank line between them; tool rows bring their own, either side.
    after_words = False
    for index, segment in enumerate((*timeline, None)):
        if index == stream_start:
            rows = stream
        if index == tail_start:
            rows = tail
        if isinstance(segment, Activity):
            if (item := by_id.get(segment.update_id)) is not None:
                pending.append(item)
            continue
        if pending:
            # Consecutive rows share one table so their glyphs line up.
            # The group at the end of a working turn is what the agent is on now,
            # between one call and the next: it shines like a running call does.
            live = segment is None and turn.run_id == state.active_run_id and state.working
            rows.append(Padding(_activity_card(pending, state, live=live), (1, 0)))
            pending = []
            after_words = False
        if segment is None:
            if turn.run_id == state.active_run_id and any(a.running for a in turn.agents):
                # What the parent is waiting on, or working beside: each delegated agent
                # still going, with where it is. Below the activity, where it is a thing of
                # the moment, and gone once every agent has reported.
                rows.append(Padding(_agents_strip(turn, state), (0, 1)))
            continue
        if after_words:
            rows.append(Text(""))
        if isinstance(segment, Narration):
            rows.append(Padding(answer_markdown(segment.text), (0, 1)))
        else:
            # The steer's bar stands where the request's does, flush; the two are a pair.
            row = _note_row(segment)
            rows.append(row if segment.kind == "steer" else Padding(row, (0, 1)))
        after_words = True
    asked = state.interaction
    if (
        asked is not None
        and asked.kind == "approval"
        and turn.run_id == state.active_run_id
        and (prompt := render_interaction(state, width=max(1, width - 2))) is not None
    ):
        # The prompt at the end of the turn it belongs to, where the person is reading,
        # the way an editor's agent asks; its answer takes its place once decided.
        tail.append(Padding(prompt, (1, 1, 0, 1)))
    if turn.run_id == state.active_run_id and state.working:
        tail.append(Padding(_working(state), (1, 1, 0, 1)))
    for artifact in turn.artifacts:
        label = artifact.reference or artifact.artifact_id
        tail.append(
            Padding(
                Text.assemble(("◇ ", ACCENT), (artifact.title, "bold"), (f"  {label}", MUTED)),
                (0, 1),
            )
        )
    return head, stream, tail


#: Steps of a pinned plan shown at once. The strip above the composer has eight rows: one
#: for the title, five for steps, and two for the lines that say how many more lie either
#: side. A plan of fifteen steps used to fill the strip with its first seven, done, and
#: the step the model was on was cut off below -- shown nowhere, since the transcript
#: leaves the plan to the strip while the run goes.
_PLAN_WINDOW = 5


def render_plan(state: AppState, *, width: int) -> RenderableType | None:
    """The active turn's checklist, for the strip above the composer: a window around
    the step the model is on, and a title that says which step of how many. None when
    there is no run going or it has no plan."""
    if not state.turns:
        return None
    turn = state.turns[-1]
    if not _pinned(state, turn):
        return None
    steps = turn.plan
    total = len(steps)
    # The current step is the first not finished; a plan wholly done has none.
    current = next((i for i, step in enumerate(steps) if step.status != "completed"), None)
    title = Text(no_wrap=True, overflow="ellipsis")
    title.append("Plan", style=f"bold {MUTED}")
    title.append(
        f" · step {current + 1} of {total}" if current is not None else f" · {total} of {total} done",
        style=MUTED,
    )
    if turn.plan_explanation:
        title.append(f" · {turn.plan_explanation}", style=f"italic {MUTED}")
    title.truncate(max(1, width), overflow="ellipsis")
    centre = current if current is not None else total - 1
    start = max(0, min(centre - _PLAN_WINDOW // 2, total - _PLAN_WINDOW))
    shown = steps[start : start + _PLAN_WINDOW]
    checklist = Table.grid(padding=(0, 1))
    checklist.add_column(width=2, no_wrap=True)
    checklist.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    if start:
        checklist.add_row(Text(""), Text(f"… {start} more above", style=MUTED))
    for step in shown:
        glyph, style, text_style = _plan_glyph(step.status)
        checklist.add_row(Text(glyph, style=style), Text(step.step, style=text_style))
    below = total - start - len(shown)
    if below:
        checklist.add_row(Text(""), Text(f"… {below} more below", style=MUTED))
    return Group(title, checklist)


def _pinned(state: AppState, turn: TurnState) -> bool:
    return bool(turn.plan) and state.working and turn.run_id == state.active_run_id


#: A spinner that advances with the clock: one frame per tick of the host's timer.
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

#: How far the shine on a working tool row moves per second, in cells, and how wide it is.
_SHINE_SPEED = 24.0
_SHINE_WIDTH = 7
#: The lit row's grey, a shade lighter than a finished row's so bold shows; and the band's
#: centre, a lighter accent than its edges.
SHINE_BASE = "#aab6c2"
SHINE_EDGE = "#b3e4fc"


def shimmer(text: str, clock: float) -> Text:
    """The text with a band of light sweeping across it, placed by the clock.

    The row a person is watching, lit the way an editor's agent lights the call it is on:
    the same words, muted, with a few brighter cells that move left to right and wrap. Pure
    in the clock, so a still frame is a still frame and a test can pin one.
    """
    # Bold grey: the same family as the finished rows, a shade lighter so the weight shows
    # in terminals where bold on the muted grey reads as no change. Not white, and not the
    # accent; the band that passes over it is the accent.
    line = Text(text, style=f"bold {SHINE_BASE}")
    if not text:
        return line
    span = len(text) + _SHINE_WIDTH
    head = int(clock * _SHINE_SPEED) % span - _SHINE_WIDTH
    for offset in range(_SHINE_WIDTH):
        cell = head + offset
        if 0 <= cell < len(text):
            # Brightest at the centre of the band, accent at its edges.
            centre = abs(offset - _SHINE_WIDTH // 2)
            line.stylize(f"bold {SHINE_EDGE}" if centre <= 1 else f"bold {ACCENT}", cell, cell + 1)
    return line


def _working(state: AppState) -> Text:
    frame = _FRAMES[int(state.clock * 2) % len(_FRAMES)]
    label = state.run_status.value.replace("_", " ").capitalize()
    text = Text.assemble((f"{frame} ", f"bold {ACCENT}"), (label, f"bold {ACCENT}"))
    if state.run_started_at > 0:
        text.append(f" · {_elapsed(max(0.0, state.clock - state.run_started_at))}", style=MUTED)
    if state.run_status in {RunStatus.QUEUED, RunStatus.RUNNING}:
        # Where an editor's agent says how to interrupt: beside the spinner, not in help.
        text.append("   esc to pause", style=MUTED)
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
        rows.extend((Text("Result", style=f"bold {MUTED}"), answer_markdown(turn.answer)))
    else:
        rows.append(Text("The current answer is still provisional.", style=MUTED))
    if turn.artifacts:
        rows.extend((Text(""), Text("Artifacts", style=f"bold {MUTED}")))
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


def _activity_card(
    items: list[ProgressItem], state: AppState, *, live: bool = False
) -> RenderableType:
    """A run of tool calls in a card: a rounded border in the callout grey, flush with
    the bars that mark the person's words, so what the agent did on the machine reads as
    one distinct thing between its paragraphs -- distinct, not loud."""
    return Panel(
        _activity_table(items, state, live=live),
        box=box.ROUNDED,
        border_style=CALLOUT,
        padding=(0, 1),
    )


def _row_words(item: ProgressItem) -> tuple[str, str]:
    """The row as prose and its argument: `ReadFile`, `src/app.py`.

    The tool's name in words, from the name the backend gave -- `read_file` is
    `ReadFile`, `web_search` is `WebSearch`, a tool server's `files__list` is
    `FilesList` -- and beside it the one argument that says what it was pointed at.
    A row whose backend named no tool shows the backend's own summary.
    """
    if not item.tool:
        return item.text, ""
    words = "".join(part.capitalize() for part in item.tool.replace("-", "_").split("_") if part)
    return words, item.detail


def _activity_table(
    items: list[ProgressItem], state: AppState, *, live: bool = False
) -> RenderableType:
    """A run of tool calls. Folded, it is the latest call and a count -- the one a person
    is watching, and how much came before it; `/tools` or Ctrl+T shows them all. A call
    that wrote code stays in the fold, code and all: a person reads a write after the
    next call has begun, and the fold used to take it away the moment one did. `live`
    says the group is the working turn's latest, so its last row shines even between
    calls."""
    activity = Table.grid(padding=(0, 1))
    activity.add_column(width=2, no_wrap=True)
    activity.add_column(ratio=1, overflow="fold")
    if state.tools_expanded or len(items) == 1:
        shown = items
    else:
        shown = [item for item in items[:-1] if item.snippets] + [items[-1]]
    folded = len(items) - len(shown)
    for item in shown:
        glyph, style, text_style = _activity_look(item)
        words, detail = _row_words(item)
        # The count sits on the latest call, below whatever code the fold kept.
        count = f"  ·  {len(items)} tool calls ›" if folded and item is shown[-1] else ""
        running = item.status.lower() == "active" and state.working
        if running or (live and item is shown[-1] and item.status.lower() != "failed"):
            # The whole line shines, count included: folded, the count is part of what a
            # person is watching.
            line = shimmer(" ".join(part for part in (words, detail) if part) + count, state.clock)
        else:
            # The prose in the text colour and the argument in grey, so the name of the
            # act and what it was done to read as two things; a failure is red through.
            line = Text(words, style=text_style if text_style == ERROR else TEXT)
            if detail:
                line.append(f" {detail}", style=text_style)
            line.append(count, style=MUTED)
        activity.add_row(Text(glyph, style=style), line)
        for snippet in item.snippets:
            # The code under its row, the way an editor's transcript shows a write.
            activity.add_row(Text(""), code_block(snippet, lines=_TRANSCRIPT_LINES))
        if item.snippets and item is not shown[-1]:
            # Air between the code and the next call, so the row after it is not read
            # as the block's last line.
            activity.add_row(Text(""), Text(""))
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
    "execute": "›_",
    "fetch": "◎",
    "think": "✦",
    "switch_mode": "⇄",
    "skill": "◈",
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
    """The card a session opens on: where it is, what it talks to, and how to begin."""
    # The header already says orca; the card says where this conversation stands.
    title = Table.grid(expand=True)
    title.add_column(ratio=1)
    title.add_column(justify="right", style=MUTED)
    title.add_row(Text("New conversation", style="bold"), state.profile)
    details = Table.grid(padding=(0, 2))
    details.add_column(width=11, style=MUTED, no_wrap=True)
    details.add_column(ratio=1, overflow="fold")
    details.add_row("workspace", state.workspace_path or "resolving…")
    for folder in state.folders:
        details.add_row("folder", folder)
    details.add_row("connection", state.endpoint or "resolving…")
    details.add_row("session", settings_label(state.mode, state.policy))
    how = Text(style=MUTED)
    how.append("Type a message to start. ")
    how.append("/", style=ACCENT)
    how.append(" lists the commands, ")
    how.append("/threads", style=ACCENT)
    how.append(" continues an earlier conversation, and Shift+Enter adds a line.")
    panel = Panel(
        Group(title, Text(""), details, Text(""), how),
        box=box.ROUNDED,
        border_style=CALLOUT,
        padding=(0, 1),
        width=max(1, width),
    )
    return [panel]


#: The person's words sit behind a bar; the agent's never do. This is the request that
#: opened the turn: a heavy bar in the accent, and the words bold. No box and no label --
#: the bar is the label.
_BAR = box.Box("    \n▌   \n▌   \n▌   \n▌   \n▌   \n▌   \n    \n")


def _request_block(text: str) -> RenderableType:
    return Panel(Text(text, style="bold"), box=_BAR, border_style=ACCENT, padding=(0, 1))


class _Barred:
    """A renderable with a thin bar down its left edge, on every line it takes.

    For words the person sent while the run was going: the same accent as the request
    that opened the turn, so the author is plain, and thin and regular where the request
    is heavy and bold, so the size of the act is too. No caption -- the bar says who,
    and where it sits in the turn says when. It used to be a grey italic quote under a
    "you, mid-run" caption on a line of its own, which read as a footnote rather than as
    something the person said. (2026-09-03)
    """

    def __init__(self, inner: RenderableType) -> None:
        self.inner: RenderableType = inner

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(1, options.max_width - 2)
        bar = Cell("▎ ", console.get_style(ACCENT))
        for line in console.render_lines(self.inner, options.update_width(width), pad=False):
            yield bar
            yield from line
            yield Cell.line()


_AGENT_GLYPHS = {"running": ("◐", ACCENT), "finished": ("●", MUTED), "failed": ("✗", WARNING)}


def _agents_strip(turn: TurnState, state: AppState) -> RenderableType:
    """One line per delegated agent still running: its id, its task, how long, and the
    last thing it did or said."""
    table = Table.grid(padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, style=f"bold {MUTED}")
    table.add_column(ratio=1)
    table.add_column(no_wrap=True, style=MUTED, justify="right")
    for agent in turn.agents:
        if not agent.running:
            continue
        glyph, style = _AGENT_GLYPHS["running"]
        elapsed = state.clock - agent.started_at if agent.started_at else 0.0
        table.add_row(
            Text(glyph, style=style),
            agent.agent_id,
            Text.assemble(
                (_first_line(agent.task, 60), ""), ("  ", ""), (_agent_latest(agent), MUTED)
            ),
            f"{elapsed:.0f}s" if elapsed else "",
        )
    return table


def _agent_latest(agent: AgentState) -> str:
    """What the agent is on now: its latest row, else the last thing it said."""
    if agent.progress:
        row = agent.progress[-1]
        words, detail = _row_words(row)
        return f"{words} {detail}".strip()
    if agent.said:
        return _first_line(agent.said[-1], 80)
    return "starting"


def _first_line(text: str, limit: int) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first if len(first) <= limit else first[: limit - 1] + "…"


def render_agents(state: AppState, *, width: int) -> RenderableType:
    """Every delegated agent of the conversation, newest turn first: its life in one
    line, then its rows and its words. Where to look when the parent is waiting on one."""
    del width
    pieces: list[RenderableType] = []
    for turn in reversed(state.turns):
        for agent in reversed(turn.agents):
            glyph, style = _AGENT_GLYPHS.get(agent.status, ("■", MUTED))
            head = Text.assemble(
                (f"{glyph} ", style),
                (agent.agent_id, "bold"),
                ("  ", ""),
                (agent.status, MUTED),
                (f"  {agent.turns} turns" if agent.turns else "", MUTED),
                (f"  {agent.seconds:.0f}s" if agent.seconds else "", MUTED),
            )
            pieces.append(head)
            if agent.task:
                pieces.append(Padding(Text(agent.task.strip(), style=MUTED), (0, 2)))
            if agent.progress:
                pieces.append(Padding(_activity_card(list(agent.progress), state), (0, 2)))
            for said in agent.said:
                pieces.append(Padding(answer_markdown(said), (0, 2)))
            if agent.answer:
                pieces.append(Padding(Text("answer", style=f"bold {MUTED}"), (0, 2)))
                pieces.append(Padding(answer_markdown(agent.answer), (0, 2)))
            pieces.append(Text(""))
    if not pieces:
        return Text("No delegated agents in this conversation.", style=MUTED)
    return Group(*pieces)


def _note_row(note: TurnNote) -> RenderableType:
    if note.kind == "steer":
        return _Barred(answer_markdown(note.text))
    glyph = {"compaction": "⇥", "folder": "+", "ended": "■", "agent": "⑂"}[note.kind]
    style = WARNING if note.kind == "ended" else ACCENT
    return Text.assemble((f"{glyph} ", style), (note.text, f"italic {MUTED}"))
