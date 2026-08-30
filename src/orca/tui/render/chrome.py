"""Shared shell chrome, transient interactions, and empty-state presentation."""

from __future__ import annotations

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orca.app.commands import visible_commands
from orca.app.model import AppState, RunStatus
from orca.tui.render.theme import (
    ACCENT,
    ERROR,
    MUTED,
    SUCCESS,
    WARNING,
)


def render_header(state: AppState, *, width: int) -> RenderableType:
    """Render the single calm line shared by every view."""

    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
    table.add_column(no_wrap=True, justify="right")
    left = Text()
    left.append("›_ ", style=f"bold {ACCENT}")
    left.append("orca", style="bold")
    if state.workspace_path:
        left.append("  ")
        left.append(state.workspace_path, style=MUTED)
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
        rows.append(Text(f"$ {interaction.command}", style=WARNING, overflow="fold"))
    if interaction.kind == "approval":
        choices = Text()
        choices.append("1", style=f"bold {ACCENT}")
        choices.append(" approve once   ", style=MUTED)
        if "approve_bash_always" in interaction.allowed_decisions:
            choices.append("2", style=f"bold {ACCENT}")
            choices.append(" always allow   ", style=MUTED)
        choices.append("3", style=f"bold {ACCENT}")
        choices.append(" reject", style=MUTED)
        rows.append(choices)
    else:
        rows.append(Text("Type your answer below and press Enter.", style=MUTED))
    return Panel(
        Group(*rows),
        title="APPROVAL" if interaction.kind == "approval" else "QUESTION",
        title_align="left",
        border_style=WARNING,
        box=box.ROUNDED,
        padding=(0, 1),
        width=max(1, width),
    )


def render_footer(state: AppState) -> Text:
    left = {
        "conversation": f"{state.mode} · {state.policy}",
        "review": "esc back · /chat conversation",
        "inspector": "developer view · esc back",
    }[state.view.value]
    right = "ctrl+p commands"
    width = max(1, state.viewport_width - 2)
    if width < len(right) + 6:
        text = Text(left, style=MUTED, overflow="ellipsis", no_wrap=True)
        text.truncate(width, overflow="ellipsis")
        return text

    left_text = Text(left, style=MUTED, overflow="ellipsis", no_wrap=True)
    left_text.truncate(width - len(right) - 1, overflow="ellipsis")
    gap = max(1, width - left_text.cell_len - len(right))
    text = Text(overflow="crop", no_wrap=True)
    text.append_text(left_text)
    text.append(" " * gap)
    text.append(right, style=MUTED)
    text.truncate(width, overflow="crop")
    return text


def render_help(*, developer: bool = False) -> RenderableType:
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True, style=f"bold {ACCENT}")
    table.add_column(ratio=1, style=MUTED)
    for command in visible_commands(developer=developer):
        suffix = f" {command.argument}" if command.argument else ""
        table.add_row(f"/{command.name}{suffix}", command.summary)
    keys = Table.grid(padding=(0, 2))
    keys.add_column(no_wrap=True, style=f"bold {ACCENT}")
    keys.add_column(style=MUTED)
    keys.add_row("Enter", "send")
    keys.add_row("Shift+Enter", "new line")
    keys.add_row("Esc", "return from a view; pause from chat")
    keys.add_row("Ctrl+P", "command palette")
    return Group(Text("Commands", style="bold"), table, Text(""), Text("Keys", style="bold"), keys)


def _run_style(status: RunStatus) -> str:
    if status is RunStatus.COMPLETED:
        return SUCCESS
    if status in {RunStatus.FAILED, RunStatus.BLOCKED}:
        return ERROR
    if status in {RunStatus.PAUSED, RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_INPUT}:
        return WARNING
    return MUTED
