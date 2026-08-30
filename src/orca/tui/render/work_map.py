"""Responsive work-map presentation for agents, topology, and unit detail."""

from __future__ import annotations

from collections import Counter

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from orca.app.model import AppState, WorkStatus, WorkUnitState
from orca.tui.render.theme import ACCENT, CALLOUT, ERROR, MUTED, SUCCESS, WARNING

_CALLSIGNS = ("atlas", "vela", "loom", "gauge", "nova", "sable", "echo", "flint")


def render_agents(state: AppState, *, width: int) -> RenderableType:
    """Render public work facts without inferring hidden orchestration state."""

    work = state.work
    units = work.units
    if not units:
        message = "The work graph has not been published yet."
        if work.graph_requested and not work.unavailable_reason:
            message = "Waiting for the work graph…"
        elif work.unavailable_reason:
            message = work.unavailable_reason
        return Group(
            Text("Work map", style=f"bold {ACCENT}"),
            Text(""),
            Text(message, style=MUTED),
            Text("The conversation continues updating while this view is open.", style=MUTED),
        )

    summary = work_summary(units)
    if width < 80:
        return _agents_narrow(units, work.selected_unit_id, summary, width=width)
    if width < 120:
        return _agents_medium(units, work.selected_unit_id, summary)
    return _agents_wide(units, work.selected_unit_id, summary, width=width)


def _agents_wide(
    units: tuple[WorkUnitState, ...], selected_id: str, summary: Text, *, width: int
) -> RenderableType:
    outer = Table.grid(expand=True, padding=(0, 2))
    outer.add_column(ratio=1, overflow="fold")
    outer.add_column(ratio=2, overflow="fold")
    outer.add_column(ratio=1, overflow="fold")
    outer.add_row(
        Text("AGENTS", style=f"bold {MUTED}"),
        Text("WORK MAP", style=f"bold {MUTED}"),
        Text("DETAIL", style=f"bold {MUTED}"),
    )
    outer.add_row(
        _agent_rail(units, selected_id),
        _ranked_graph(units, selected_id, width=max(24, width // 2 - 4)),
        _unit_detail(units, selected_id),
    )
    return Group(summary, Text(""), outer)


def _agents_medium(
    units: tuple[WorkUnitState, ...], selected_id: str, summary: Text
) -> RenderableType:
    layout = Table.grid(expand=True, padding=(0, 2))
    layout.add_column(ratio=2, overflow="fold")
    layout.add_column(ratio=1, overflow="fold")
    layout.add_row(
        Text("WORK MAP", style=f"bold {MUTED}"),
        Text("DETAIL", style=f"bold {MUTED}"),
    )
    layout.add_row(_work_list(units, selected_id), _unit_detail(units, selected_id))
    return Group(summary, Text(""), layout)


def _agents_narrow(
    units: tuple[WorkUnitState, ...], selected_id: str, summary: Text, *, width: int
) -> RenderableType:
    return Group(
        Text("Work map", style=f"bold {ACCENT}"),
        summary,
        Text(""),
        _work_list(units, selected_id, objectives=True, show_agents=width >= 56),
        Text(""),
        _unit_detail(units, selected_id),
    )


def _agent_rail(units: tuple[WorkUnitState, ...], selected_id: str) -> RenderableType:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(ratio=1, overflow="ellipsis")
    table.add_row(
        Text("●", style=SUCCESS), Text("root", style="bold"), Text("coordinating", style=MUTED)
    )
    for index, unit in enumerate(units):
        glyph, style = _status(unit.status)
        alias = _short(unit.agent_id or _CALLSIGNS[index % len(_CALLSIGNS)], 12)
        selected = unit.unit_id == selected_id
        table.add_row(
            Text(glyph, style=style),
            Text(alias, style=f"bold {ACCENT}" if selected else "bold"),
            Text(_short(unit.objective or unit.unit_id, 28), style=MUTED),
        )
    return table


def _work_list(
    units: tuple[WorkUnitState, ...],
    selected_id: str,
    *,
    objectives: bool = True,
    show_agents: bool = True,
) -> RenderableType:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(width=2, no_wrap=True)
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    if show_agents:
        table.add_column(max_width=12, no_wrap=True, overflow="ellipsis", justify="right")
    for index, unit in enumerate(units):
        glyph, style = _status(unit.status)
        pointer = "›" if unit.unit_id == selected_id else " "
        label = unit.objective if objectives and unit.objective else unit.unit_id
        row: list[RenderableType] = [
            Text(pointer, style=f"bold {ACCENT}"),
            Text(glyph, style=style),
            Text(label, style="bold" if unit.unit_id == selected_id else ""),
        ]
        if show_agents:
            alias = _short(unit.agent_id or _CALLSIGNS[index % len(_CALLSIGNS)], 12)
            row.append(Text(alias, style=MUTED))
        table.add_row(*row)
    return table


def _ranked_graph(
    units: tuple[WorkUnitState, ...], selected_id: str, *, width: int
) -> RenderableType:
    ranks = _topological_ranks(units)
    rows: list[RenderableType] = []
    for rank_index, rank in enumerate(ranks):
        cards: list[RenderableType] = []
        for unit in rank:
            glyph, style = _status(unit.status)
            body = Text.assemble(
                (f"{glyph} ", style), (_short(unit.objective or unit.unit_id, 36), "bold")
            )
            cards.append(
                Panel(
                    body,
                    subtitle=unit.kind,
                    subtitle_align="right",
                    border_style=ACCENT if unit.unit_id == selected_id else CALLOUT,
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        cards_per_row = max(1, min(3, width // 26))
        for start in range(0, len(cards), cards_per_row):
            chunk = cards[start : start + cards_per_row]
            rank_row = Table.grid(expand=True, padding=(0, 1))
            for _ in chunk:
                rank_row.add_column(ratio=1, overflow="fold")
            rank_row.add_row(*chunk)
            rows.append(rank_row)
        if rank_index + 1 < len(ranks):
            rows.append(Text("↓", style=MUTED, justify="center"))
    return Group(*rows)


def _unit_detail(units: tuple[WorkUnitState, ...], selected_id: str) -> RenderableType:
    unit = next((item for item in units if item.unit_id == selected_id), units[0])
    glyph, style = _status(unit.status)
    rows: list[RenderableType] = [
        Text.assemble((f"{glyph} ", style), (unit.objective or unit.unit_id, "bold")),
        Text(unit.unit_id, style=MUTED),
        Text(""),
    ]
    facts = Table.grid(padding=(0, 1))
    facts.add_column(no_wrap=True, style=MUTED)
    facts.add_column(ratio=1, overflow="fold")
    facts.add_row("status", unit.status.value)
    if unit.depends_on:
        facts.add_row("after", ", ".join(unit.depends_on))
    if unit.attempt:
        facts.add_row("attempt", str(unit.attempt))
    facts.add_row("tools", str(unit.tool_count))
    if unit.active_tool:
        facts.add_row("active", unit.active_tool)
    rows.append(facts)
    if unit.progress:
        rows.extend((Text(""), Text(unit.progress, style=MUTED)))
    return Group(*rows)


def work_summary(units: tuple[WorkUnitState, ...]) -> Text:
    counts = Counter(unit.status for unit in units)
    done = counts[WorkStatus.COMPLETED]
    active = counts[WorkStatus.ACTIVE] + counts[WorkStatus.CHECKING]
    failed = counts[WorkStatus.FAILED] + counts[WorkStatus.BLOCKED]
    waiting = len(units) - done - active - failed
    text = Text()
    text.append(f"{done} done", style=SUCCESS)
    text.append("  ·  ", style=MUTED)
    text.append(f"{active} active", style=ACCENT)
    if failed:
        text.append("  ·  ", style=MUTED)
        text.append(f"{failed} blocked", style=ERROR)
    text.append("  ·  ", style=MUTED)
    text.append(f"{waiting} queued", style=MUTED)
    return text


def _topological_ranks(units: tuple[WorkUnitState, ...]) -> tuple[tuple[WorkUnitState, ...], ...]:
    by_id = {unit.unit_id: unit for unit in units}
    rank_by_id: dict[str, int] = {}
    unresolved = list(units)
    while unresolved:
        progressed = False
        remaining: list[WorkUnitState] = []
        for unit in unresolved:
            known_dependencies = [
                dependency for dependency in unit.depends_on if dependency in by_id
            ]
            if all(dependency in rank_by_id for dependency in known_dependencies):
                rank_by_id[unit.unit_id] = (
                    max((rank_by_id[dependency] for dependency in known_dependencies), default=-1)
                    + 1
                )
                progressed = True
            else:
                remaining.append(unit)
        if not progressed:
            next_rank = max(rank_by_id.values(), default=-1) + 1
            for unit in remaining:
                rank_by_id[unit.unit_id] = next_rank
            break
        unresolved = remaining
    max_rank = max(rank_by_id.values(), default=-1)
    return tuple(
        tuple(unit for unit in units if rank_by_id.get(unit.unit_id) == rank)
        for rank in range(max_rank + 1)
    )


def _status(status: WorkStatus) -> tuple[str, str]:
    return {
        WorkStatus.WAITING: ("○", MUTED),
        WorkStatus.ACTIVE: ("●", ACCENT),
        WorkStatus.CHECKING: ("◐", WARNING),
        WorkStatus.COMPLETED: ("✓", SUCCESS),
        WorkStatus.FAILED: ("×", ERROR),
        WorkStatus.BLOCKED: ("!", ERROR),
        WorkStatus.CANCELLED: ("−", WARNING),
        WorkStatus.UNKNOWN: ("?", MUTED),
    }[status]


def _short(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"
