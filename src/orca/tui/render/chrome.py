"""Shared shell chrome, transient interactions, and empty-state presentation."""

from __future__ import annotations

from collections.abc import Sequence

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orca.app.commands import Choices, Suggestion, argument_label, visible_commands
from orca.app.model import AppState, Choice, RunStatus, Usage
from orca.tui.render.code import code_block
from orca.tui.render.theme import (
    ACCENT,
    CALLOUT,
    ERROR,
    MUTED,
    PLAN,
    SUCCESS,
    WARNING,
)

#: A colour for each setting a person can name, so a glance at the footer says what
#: state the next turn starts in. The vocabularies are the backend's and open, so a value
#: not listed is shown in the muted grey, never dropped. Policies run from cool to hot as
#: they let more through; `plan` is its own colour because it is a way of working, not a
#: verdict.
MODE_COLOURS = {"normal": SUCCESS, "plan": PLAN}
POLICY_COLOURS = {"ask": ACCENT, "edits": WARNING, "full-access": ERROR}


def mode_style(mode: str) -> str:
    return MODE_COLOURS.get(mode, MUTED)


def policy_style(policy: str) -> str:
    return POLICY_COLOURS.get(policy, MUTED)


def settings_label(mode: str, policy: str) -> Text:
    """`normal · ask`, each in its colour."""
    return Text.assemble((mode, mode_style(mode)), (" · ", MUTED), (policy, policy_style(policy)))


def render_header(state: AppState, *, width: int) -> RenderableType:
    """Render the single calm line shared by every view."""

    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    table.add_column(no_wrap=True, justify="right")
    left = Text()
    left.append("›_ ", style=f"bold {ACCENT}")
    left.append("orca", style="bold")
    status = "connecting" if state.booting else state.run_status.value.replace("_", " ")
    right = Text()
    right.append("● " if state.connected else "○ ", style=SUCCESS if state.connected else MUTED)
    right.append(status, style=_run_style(state.run_status))
    if width >= 96 and state.profile:
        right.append(f" · {state.profile}", style=MUTED)
    table.add_row(left, right)
    return table


def render_inspector(state: AppState, *, width: int) -> RenderableType:
    del width
    rows: list[RenderableType] = [
        Text("Developer inspector", style=f"bold {ACCENT}"),
        Text("Private execution facts are isolated from the conversation.", style=MUTED),
        Text(""),
    ]
    if not state.developer_events:
        rows.append(Text("No developer events have been received in this session.", style=MUTED))
    else:
        rows.extend(Text(line, style="dim") for line in state.developer_events[-200:])
    return Group(*rows)


def render_interaction(state: AppState, *, width: int) -> RenderableType | None:
    interaction = state.interaction
    if interaction is None:
        return None
    rows: list[RenderableType] = [Text(interaction.title, style="bold")]
    if interaction.summary:
        rows.append(Text(interaction.summary, style=MUTED))
    if interaction.command:
        rows.append(Text(""))
        rows.append(Text.assemble(("$ ", MUTED), (interaction.command, "bold"), overflow="fold"))
    for snippet in interaction.snippets:
        rows.append(Text(snippet.title, style=f"bold {MUTED}"))
        rows.append(code_block(snippet, lines=_APPROVAL_LINES, width=max(1, width - 4)))
    if interaction.kind == "approval":
        rows.append(Text(""))
        if interaction.sending:
            rows.append(Text("Sending…", style=MUTED))
        else:
            rows.append(_approval_choices(interaction.allowed_decisions, interaction.grant))
    else:
        if interaction.options:
            rows.append(Text(""))
            rows.append(_numbered(interaction.options))
        rows.append(Text(""))
        rows.append(
            Text(
                "Type a number, or your own answer, and press Enter."
                if interaction.options
                else "Type your answer and press Enter.",
                style=MUTED,
            )
        )
    return Panel(
        Group(*rows),
        title=Text("Approval" if interaction.kind == "approval" else "Question", style=WARNING),
        title_align="left",
        border_style=WARNING,
        box=box.ROUNDED,
        padding=(0, 1),
        width=max(1, width),
    )


def _approval_choices(allowed: tuple[str, ...], grant: str) -> RenderableType:
    """The decisions, one to a row, the key that makes each beside it -- the way an
    editor's agent lays out a permission prompt, so the choice is read before the key.
    The standing grant names what it would cover, when the backend said, because a
    choice whose scope is unstated is read as the narrowest or the widest thing it could
    be, and is wrong either way."""
    choices = Table.grid(padding=(0, 2))
    choices.add_column(width=1, no_wrap=True, style=f"bold {ACCENT}")
    choices.add_column(no_wrap=True)
    choices.add_column(style=MUTED, no_wrap=True)
    choices.add_row("1", "Approve once", "enter")
    if "approve_bash_always" in allowed:
        choices.add_row("2", f"Always allow {grant}" if grant else "Always allow this", "")
    choices.add_row("3", "Reject", "esc")
    return choices


def _numbered(options: tuple[str, ...]) -> RenderableType:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(width=1, no_wrap=True, style=f"bold {ACCENT}")
    rows.add_column(ratio=1, overflow="fold")
    for index, option in enumerate(options, start=1):
        rows.add_row(str(index), option)
    return rows


#: Rows of the `/` menu shown at once. A window of eight rows and a line saying how many
#: more keeps the highlighted row in view however far down the list it is, rather than
#: cut off below the strip; the strip is sized to hold exactly that.
_MENU_ROWS = 8


def render_command_menu(rows: tuple[Suggestion, ...], selected: int) -> RenderableType:
    """The rows a draft could become, one highlighted. Enter runs it, Tab takes it. A long
    list is a window that slides with the highlight, so the row a person moved to is
    always the one they can see."""
    start = max(0, min(selected - _MENU_ROWS + 1, len(rows) - _MENU_ROWS))
    shown = rows[start : start + _MENU_ROWS]
    table = Table.grid(padding=(0, 2))
    table.add_column(width=2, no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True, style=MUTED)
    table.add_column(ratio=1, style=MUTED, overflow="ellipsis")
    for index, row in enumerate(shown, start=start):
        chosen = index == selected
        table.add_row(
            Text("›" if chosen else "", style=f"bold {ACCENT}"),
            Text(row.label, style=f"bold {ACCENT}" if chosen else ACCENT),
            Text(row.argument),
            Text(row.summary, style="bold" if chosen else MUTED),
        )
    hidden = len(rows) - len(shown)
    if hidden:
        above, below = start, len(rows) - start - len(shown)
        where = " and ".join(
            part
            for part in (f"{above} above" if above else "", f"{below} below" if below else "")
            if part
        )
        table.add_row(Text(""), Text(f"… {where}", style=MUTED), Text(""), Text(""))
    return table


#: How long a notice stays, by how much it matters. An error waits to be read; a decision
#: that was just made needs only a glance.
_NOTICE_SECONDS = {"info": 3.0, "warning": 6.0, "error": 10.0}


def render_notice(state: AppState) -> Text | None:
    """The latest notice still within its time, for the line above the composer."""
    if not state.notices:
        return None
    notice = state.notices[-1]
    if state.clock - notice.shown_at >= _NOTICE_SECONDS[notice.level]:
        return None
    style = {"info": MUTED, "warning": WARNING, "error": ERROR}[notice.level]
    mark = {"info": "·", "warning": "!", "error": "✗"}[notice.level]
    return Text(f"{mark} {notice.message}", style=style, overflow="ellipsis", no_wrap=True)


def render_footer(state: AppState) -> Text:
    if state.view.value == "conversation":
        # The folder the run works in, where an editor's agent shows it: under the input,
        # beside the settings for the next turn. The folders the conversation reaches
        # beyond it by name -- a path each would take the line, and `/add` lists them.
        # Three groups, set apart by space rather than by another dot.
        place = state.workspace_path
        if state.folders:
            place += " + " + ", ".join(_folder_name(folder) for folder in state.folders)
        groups: list[Text] = []
        if place:
            groups.append(Text(place, style=MUTED))
        groups.append(settings_label(state.mode, state.policy))
        if state.usage is not None:
            groups.append(context_meter(state.usage))
        left_text = Text("   ", style=MUTED).join(groups)
    else:
        words = {
            "review": "esc back · /chat conversation",
            "inspector": "developer view · esc back",
            "agents": "delegated agents · esc back",
        }[state.view.value]
        left_text = Text(words, style=MUTED)
    left_text.no_wrap = True
    left_text.overflow = "ellipsis"
    right = "ctrl+p commands"
    # The footer sits two cells in from either edge, level with the transcript's text.
    width = max(1, state.viewport_width - 4)
    if width < len(right) + 6:
        left_text.truncate(width, overflow="ellipsis")
        return left_text

    left_text.truncate(width - len(right) - 1, overflow="ellipsis")
    gap = max(1, width - left_text.cell_len - len(right))
    text = Text(overflow="crop", no_wrap=True)
    text.append_text(left_text)
    text.append(" " * gap)
    text.append(right, style=MUTED)
    text.truncate(width, overflow="crop")
    return text


def render_help(
    *,
    developer: bool = False,
    choices: Choices | None = None,
    skills: Sequence[Choice] = (),
) -> RenderableType:
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True, style=f"bold {ACCENT}")
    table.add_column(no_wrap=True, style=MUTED)
    table.add_column(ratio=1, style=MUTED)
    for command in visible_commands(developer=developer):
        values = (choices or {}).get(command.name, ())
        table.add_row(f"/{command.name}", argument_label(command, values), command.summary)
    parts: list[RenderableType] = [Text("Commands", style="bold"), table]
    if skills:
        listed = Table.grid(padding=(0, 2))
        listed.add_column(no_wrap=True, style=f"bold {ACCENT}")
        listed.add_column(ratio=1, style=MUTED)
        for skill in skills:
            listed.add_row(f"/{skill.name}", skill.summary)
        parts += [Text(""), Text("Skills of this workspace", style="bold"), listed]
    keys = Table.grid(padding=(0, 2))
    keys.add_column(no_wrap=True, style=f"bold {ACCENT}")
    keys.add_column(style=MUTED)
    keys.add_row("Enter", "send")
    keys.add_row("Shift+Enter", "new line")
    keys.add_row("Up / Down", "earlier messages, from the first or last line")
    keys.add_row("Esc", "return from a view; pause from chat")
    keys.add_row("Ctrl+P", "command palette")
    keys.add_row("Ctrl+T", "show every tool call, or fold them again")
    return Group(*parts, Text(""), Text("Keys", style="bold"), keys)


#: Lines of code shown on an approval before the rest is elided. The card scrolls, so this
#: is about a person finding the choices, not about fitting a screen.
_APPROVAL_LINES = 120


#: The context meter's width, in cells. Ten, so each cell is a tenth and the eye can
#: read it without the number -- and the number is there too.
_METER_CELLS = 10


def context_meter(usage: Usage) -> Text:
    """How full the context is, as a bar and a share: `━━━───────  24% of 262k`.

    The bar is in the accent while there is room, amber past six tenths and red past
    eight and a half, the way a fuel gauge changes colour rather than the number: the
    number says how much, the colour says whether to care. `≈` when the backend estimated
    rather than measured. Without a window to measure against, only the count.
    """
    mark = "≈" if usage.estimated else ""
    if not usage.context_window:
        return Text(f"{mark}{_compact(usage.tokens)} tokens", style=MUTED)
    share = min(1.0, usage.tokens / usage.context_window)
    filled = round(share * _METER_CELLS)
    colour = ERROR if share >= 0.85 else WARNING if share >= 0.6 else ACCENT
    meter = Text()
    meter.append("━" * filled, style=colour)
    meter.append("━" * (_METER_CELLS - filled), style=CALLOUT)
    meter.append(f"  {mark}{int(share * 100)}%", style=colour)
    meter.append(f" of {_compact(usage.context_window)}", style=MUTED)
    return meter


def _compact(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _folder_name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _run_style(status: RunStatus) -> str:
    if status is RunStatus.COMPLETED:
        return SUCCESS
    if status in {RunStatus.FAILED, RunStatus.BLOCKED}:
        return ERROR
    if status in {RunStatus.PAUSED, RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_INPUT}:
        return WARNING
    return MUTED
