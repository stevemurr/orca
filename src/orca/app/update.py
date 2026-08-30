"""Pure update function for terminal state and requested side effects."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from orca.app.actions import (
    Action,
    ApprovalDecided,
    Back,
    BootCompleted,
    BootFailed,
    CommandCompleted,
    CommandInvoked,
    ComposerChanged,
    ComposerSubmitted,
    EventReceived,
    Navigate,
    OperationFailed,
    QuestionAnswered,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
    ViewportChanged,
    WorkGraphLoaded,
    WorkGraphUnavailable,
    WorkSelected,
    WorkSelectionMoved,
)
from orca.app.commands import parse_input
from orca.app.model import (
    AppState,
    ArtifactOffer,
    InteractionState,
    Notice,
    PlanStep,
    ProgressItem,
    RunStatus,
    TaskEvent,
    TurnState,
    ViewId,
    WorkMapState,
    WorkStatus,
    WorkUnitSpec,
    WorkUnitState,
)


@dataclass(frozen=True, slots=True)
class StartRun:
    message: str


@dataclass(frozen=True, slots=True)
class SendRunCommand:
    run_id: str
    command: str
    fields: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoverWorkGraph:
    run_id: str
    artifact_id: str = ""


@dataclass(frozen=True, slots=True)
class OpenHelp:
    pass


@dataclass(frozen=True, slots=True)
class OpenThreads:
    pass


@dataclass(frozen=True, slots=True)
class LoadThread:
    thread_id: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class FollowRun:
    run_id: str
    after_seq: int


@dataclass(frozen=True, slots=True)
class SwitchWorkspace:
    selector: str


@dataclass(frozen=True, slots=True)
class ExitApplication:
    pass


Effect = (
    StartRun
    | SendRunCommand
    | DiscoverWorkGraph
    | OpenHelp
    | OpenThreads
    | LoadThread
    | FollowRun
    | SwitchWorkspace
    | ExitApplication
)


@dataclass(frozen=True, slots=True)
class Transition:
    state: AppState
    effects: tuple[Effect, ...] = ()


def reduce(state: AppState, action: Action) -> Transition:
    """Apply one typed action without performing I/O or rendering."""

    if isinstance(action, BootCompleted):
        conversation = (
            {
                "thread_id": None,
                "active_run_id": None,
                "cursor": 0,
                "run_status": RunStatus.IDLE,
                "turns": (),
                "work": WorkMapState(),
                "interaction": None,
                "view_stack": (ViewId.CONVERSATION,),
                "developer_cursor": 0,
                "developer_events": (),
            }
            if action.reset_conversation
            else {}
        )
        return Transition(
            replace(
                state,
                connected=True,
                booting=False,
                profile=action.profile,
                endpoint=action.endpoint,
                protocol_version=action.protocol_version,
                workspace_id=action.workspace_id,
                workspace_name=action.workspace_name,
                workspace_path=action.workspace_path,
                cwd_relative=action.cwd_relative,
                capabilities=action.capabilities,
                **conversation,
            )
        )
    if isinstance(action, BootFailed):
        return Transition(
            _notice(replace(state, booting=False, connected=False), action.message, "error")
        )
    if isinstance(action, Navigate):
        if action.view is ViewId.CONVERSATION:
            return Transition(replace(state, view_stack=(ViewId.CONVERSATION,)))
        stack = state.view_stack
        if stack[-1] is not action.view:
            stack = (*stack, action.view)
        updated = replace(state, view_stack=stack)
        if action.view is ViewId.AGENTS:
            updated, effects = _request_graph_if_needed(updated)
            return Transition(updated, effects)
        if action.view is ViewId.INSPECTOR:
            updated = replace(updated, developer=True)
            run_id = updated.active_run_id or updated.latest_run_id
            if run_id:
                return Transition(
                    updated,
                    (FollowRun(run_id, updated.developer_cursor),),
                )
        return Transition(updated)
    if isinstance(action, Back):
        stack = state.view_stack[:-1] if len(state.view_stack) > 1 else state.view_stack
        return Transition(replace(state, view_stack=stack))
    if isinstance(action, ViewportChanged):
        return Transition(
            replace(
                state,
                viewport_width=max(1, action.width),
                viewport_height=max(1, action.height),
            )
        )
    if isinstance(action, ComposerChanged):
        return Transition(replace(state, composer_draft=action.text))
    if isinstance(action, ComposerSubmitted):
        text = action.text.strip()
        if not text:
            return Transition(state)
        parsed = parse_input(text)
        cleared = replace(state, composer_draft="")
        if parsed is not None:
            return reduce(cleared, CommandInvoked(parsed.name, parsed.argument))
        if state.interaction is not None and state.interaction.kind == "question":
            return reduce(cleared, QuestionAnswered(text))
        if state.active_run_id:
            return Transition(
                cleared,
                (SendRunCommand(state.active_run_id, "steer", (("content", text),)),),
            )
        return Transition(replace(cleared, submitting=True), (StartRun(text),))
    if isinstance(action, CommandInvoked):
        return _command(state, action.name, action.argument)
    if isinstance(action, RunAccepted):
        turn = TurnState(run_id=action.run_id, status="queued")
        return Transition(
            replace(
                state,
                thread_id=action.thread_id,
                active_run_id=action.run_id,
                cursor=0,
                run_status=RunStatus.QUEUED,
                submitting=False,
                turns=(*state.turns, turn),
                work=WorkMapState(run_id=action.run_id),
                interaction=None,
                developer_cursor=0,
                developer_events=(),
            )
        )
    if isinstance(action, OperationFailed):
        return Transition(_notice(replace(state, submitting=False), action.message, "error"))
    if isinstance(action, EventReceived):
        return _event(state, action.event)
    if isinstance(action, WorkGraphLoaded):
        if action.run_id != state.work.run_id:
            return Transition(state)
        return Transition(replace(state, work=_merge_graph(state.work, action)))
    if isinstance(action, WorkGraphUnavailable):
        if action.run_id != state.work.run_id:
            return Transition(state)
        return Transition(
            replace(
                state,
                work=replace(
                    state.work,
                    graph_requested=False,
                    unavailable_reason=action.reason,
                ),
            )
        )
    if isinstance(action, WorkSelectionMoved):
        units = state.work.units
        if not units:
            return Transition(state)
        selected = next(
            (
                index
                for index, unit in enumerate(units)
                if unit.unit_id == state.work.selected_unit_id
            ),
            0,
        )
        selected = max(0, min(len(units) - 1, selected + action.delta))
        return Transition(
            replace(
                state,
                work=replace(state.work, selected_unit_id=units[selected].unit_id),
            )
        )
    if isinstance(action, WorkSelected):
        if action.unit_id not in {unit.unit_id for unit in state.work.units}:
            return Transition(state)
        return Transition(replace(state, work=replace(state.work, selected_unit_id=action.unit_id)))
    if isinstance(action, ThreadSelected):
        return Transition(
            replace(state, submitting=True),
            (LoadThread(action.thread_id, action.title),),
        )
    if isinstance(action, ThreadLoaded):
        replayed = replace(
            state,
            thread_id=action.thread_id,
            active_run_id=None,
            cursor=0,
            run_status=RunStatus.IDLE,
            turns=(),
            work=WorkMapState(),
            interaction=None,
            view_stack=(ViewId.CONVERSATION,),
            submitting=False,
            developer_cursor=0,
            developer_events=(),
        )
        for run in action.runs:
            replayed = reduce(
                replayed,
                RunAccepted(run.run_id, action.thread_id),
            ).state
            for item in run.events:
                replayed = _event(replayed, item).state
            reported = _reported_run_status(run.status)
            if (
                reported
                in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                    RunStatus.BLOCKED,
                }
                and replayed.active_run_id == run.run_id
            ):
                turn = replace(_latest_turn(replayed), status=reported.value)
                replayed = _replace_latest_turn(
                    replace(replayed, active_run_id=None, run_status=reported),
                    turn,
                )
            elif not run.events and reported is not None:
                turn = replace(_latest_turn(replayed), status=reported.value)
                replayed = _replace_latest_turn(
                    replace(replayed, run_status=reported),
                    turn,
                )
        label = action.title.strip() or action.thread_id
        replayed = _notice(replayed, f"Continuing {label}")
        effects: tuple[Effect, ...] = ()
        if replayed.active_run_id:
            after_seq = 0 if replayed.developer else replayed.cursor
            effects = (FollowRun(replayed.active_run_id, after_seq),)
        return Transition(replayed, effects)
    if isinstance(action, ApprovalDecided):
        interaction = state.interaction
        if interaction is None or interaction.kind != "approval" or not state.active_run_id:
            return Transition(_notice(state, "Nothing is awaiting approval."))
        if interaction.allowed_decisions and action.decision not in interaction.allowed_decisions:
            return Transition(_notice(state, "That approval choice is not available.", "warning"))
        return Transition(
            state,
            (
                SendRunCommand(
                    state.active_run_id,
                    "resolve_approval",
                    (
                        ("approval_id", interaction.request_id),
                        ("decision", action.decision),
                    ),
                ),
            ),
        )
    if isinstance(action, QuestionAnswered):
        interaction = state.interaction
        if interaction is None or interaction.kind != "question" or not state.active_run_id:
            return Transition(_notice(state, "Nothing is awaiting an answer."))
        return Transition(
            state,
            (
                SendRunCommand(
                    state.active_run_id,
                    "answer",
                    (
                        ("question_id", interaction.request_id),
                        ("content", action.answer),
                    ),
                ),
            ),
        )
    if isinstance(action, CommandCompleted):
        label = str(action.response.get("status") or "sent")
        return Transition(_notice(state, f"{action.command}: {label}"))
    raise TypeError(f"unsupported action: {type(action).__name__}")


def _command(state: AppState, name: str, argument: str) -> Transition:
    routes = {
        "agents": ViewId.AGENTS,
        "chat": ViewId.CONVERSATION,
        "review": ViewId.REVIEW,
        "inspect": ViewId.INSPECTOR,
    }
    if route := routes.get(name):
        return reduce(state, Navigate(route))
    if name == "help":
        return Transition(state, (OpenHelp(),))
    if name == "threads":
        if state.active_run_id:
            return Transition(
                _notice(
                    state,
                    "Pause or finish the active run before switching conversations.",
                    "warning",
                )
            )
        return Transition(state, (OpenThreads(),))
    if name == "quit":
        return Transition(state, (ExitApplication(),))
    if name == "new":
        if state.active_run_id:
            return Transition(
                _notice(
                    state,
                    "Detach from the active run before starting a new conversation.",
                    "warning",
                )
            )
        return Transition(
            replace(
                state,
                thread_id=None,
                turns=(),
                work=WorkMapState(),
                view_stack=(ViewId.CONVERSATION,),
                developer_cursor=0,
                developer_events=(),
            )
        )
    if name == "mode":
        if not argument:
            return Transition(_notice(state, f"mode: {state.mode}"))
        return Transition(replace(state, mode=argument))
    if name == "permissions":
        if not argument:
            return Transition(_notice(state, f"permissions: {state.policy}"))
        return Transition(replace(state, policy=argument))
    if name == "workspace":
        if not argument:
            return Transition(_notice(state, f"workspace: {state.workspace_path or 'none'}"))
        if state.active_run_id:
            return Transition(
                _notice(state, "The workspace cannot change while a run is active.", "warning")
            )
        return Transition(state, (SwitchWorkspace(argument),))
    if name == "status":
        label = state.run_status.value.replace("_", " ")
        return Transition(_notice(state, f"{state.profile} · {state.workspace_path} · {label}"))
    if name in {"pause", "resume", "cancel"}:
        if not state.active_run_id:
            return Transition(_notice(state, "No run is active."))
        return Transition(state, (SendRunCommand(state.active_run_id, name),))
    return Transition(_notice(state, f"Unknown command: /{name}", "warning"))


def _event(state: AppState, event: TaskEvent) -> Transition:
    if event.visibility == "developer":
        if event.sequence <= state.developer_cursor:
            return Transition(state)
        line = f"{event.sequence:>4}  {event.kind}"
        return Transition(
            replace(
                state,
                developer_cursor=event.sequence,
                developer_events=(*state.developer_events, line),
            )
        )
    if event.sequence <= state.cursor:
        return Transition(state)
    state = replace(state, cursor=event.sequence)

    payload = event.payload
    kind = event.kind
    run_id = state.active_run_id or state.latest_run_id
    state = _ensure_turn(state, run_id)

    if kind == "run.created":
        turn = _latest_turn(state)
        turn = replace(
            turn,
            request=_string(payload, "message"),
            mode=_string(payload, "mode"),
            policy=_string(payload, "approval_policy"),
            status="running",
        )
        return Transition(_replace_latest_turn(replace(state, run_status=RunStatus.RUNNING), turn))

    if kind == "run.progress":
        update_id = _string(payload, "update_id")
        text = _string(payload, "text").strip()
        if not update_id or not text:
            return Transition(state)
        item = ProgressItem(
            update_id=update_id,
            text=text,
            status=_string(payload, "status") or "active",
            work_unit_id=_string(payload, "work_unit_id"),
        )
        turn = _latest_turn(state)
        progress = _upsert_by(turn.progress, item, "update_id")
        state = _replace_latest_turn(state, replace(turn, progress=progress, status="running"))
        if item.work_unit_id:
            work = _progress_work(state.work, item)
            state = replace(state, work=work, run_status=RunStatus.RUNNING)
            state, effects = _request_graph_if_needed(state)
            return Transition(state, effects)
        return Transition(state)

    if kind == "answer.delta":
        text = _string(payload, "text")
        if not text:
            return Transition(state)
        turn = _latest_turn(state)
        stream = (_string(payload, "effect_id"), _string(payload, "model_call_id"))
        previous = turn.provisional_answer if stream == turn.answer_stream else ""
        turn = replace(
            turn,
            provisional_answer=previous + text,
            answer_stream=stream,
        )
        return Transition(_replace_latest_turn(state, turn))

    if kind == "plan.progress":
        entries = payload.get("plan")
        if not isinstance(entries, list):
            return Transition(state)
        # Whole-list replacement, not an upsert: the event carries the model's complete current
        # checklist, so merging by step text would resurrect a step it deliberately dropped.
        steps = tuple(
            PlanStep(step=_string(item, "step"), status=_string(item, "status") or "pending")
            for item in entries
            if isinstance(item, Mapping) and _string(item, "step").strip()
        )
        turn = _latest_turn(state)
        return Transition(
            _replace_latest_turn(
                state,
                replace(
                    turn, plan=steps, plan_explanation=_string(payload, "explanation").strip()
                ),
            )
        )

    if kind == "plan.available":
        artifact_id = _string(payload, "artifact_id")
        if not artifact_id:
            return Transition(state)
        turn = _latest_turn(state)
        offer = ArtifactOffer(
            artifact_id=artifact_id,
            kind="plan",
            title="Plan available",
            reference=_string(payload, "path"),
        )
        return Transition(
            _replace_latest_turn(
                state, replace(turn, artifacts=_upsert_by(turn.artifacts, offer, "artifact_id"))
            )
        )

    if kind == "work.graph.available":
        artifact_id = _string(payload, "artifact_id")
        if not run_id:
            return Transition(state)
        requested = replace(state, work=replace(state.work, graph_requested=True))
        return Transition(requested, (DiscoverWorkGraph(run_id, artifact_id),))

    if kind == "work.unit.updated":
        work = _structured_work_update(state.work, payload)
        return Transition(replace(state, work=work))

    if kind.startswith("tool."):
        unit_id = _string(payload, "work_unit_id")
        if not unit_id:
            return Transition(state)
        work = _tool_work(state.work, kind, payload)
        return Transition(replace(state, work=work))

    if kind == "approval.requested":
        allowed = payload.get("allowed_decisions")
        decisions = tuple(str(item) for item in allowed) if isinstance(allowed, list) else ()
        arguments = payload.get("arguments")
        command = ""
        if isinstance(arguments, Mapping):
            argv = arguments.get("argv")
            if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
                command = shlex.join(argv)
        interaction = InteractionState(
            kind="approval",
            request_id=_string(payload, "approval_id") or event.event_id,
            title=_string(payload, "title") or "Approve this action?",
            summary=_string(payload, "summary"),
            command=command,
            risk=_string(payload, "risk"),
            allowed_decisions=decisions,
        )
        return Transition(
            replace(state, interaction=interaction, run_status=RunStatus.AWAITING_APPROVAL)
        )

    if kind == "question.requested":
        interaction = InteractionState(
            kind="question",
            request_id=_string(payload, "question_id") or event.event_id,
            title=_string(payload, "prompt") or "The agent needs more information.",
        )
        return Transition(
            replace(state, interaction=interaction, run_status=RunStatus.AWAITING_INPUT)
        )

    if kind in {"approval.resolved", "question.resolved"}:
        return Transition(replace(state, interaction=None, run_status=RunStatus.RUNNING))

    if kind == "run.paused":
        return Transition(replace(state, run_status=RunStatus.PAUSED))

    terminal = {
        "run.completed": RunStatus.COMPLETED,
        "run.failed": RunStatus.FAILED,
        "run.cancelled": RunStatus.CANCELLED,
        "run.blocked": RunStatus.BLOCKED,
    }.get(kind)
    if terminal is not None:
        turn = _latest_turn(state)
        summary = _string(payload, "summary")
        turn = replace(
            turn,
            answer=summary,
            provisional_answer="",
            status=terminal.value,
        )
        work = _terminal_work(state.work, terminal, payload)
        return Transition(
            _replace_latest_turn(
                replace(
                    state,
                    active_run_id=None,
                    run_status=terminal,
                    interaction=None,
                    submitting=False,
                    work=work,
                ),
                turn,
            )
        )

    return Transition(state)


def _request_graph_if_needed(state: AppState) -> tuple[AppState, tuple[Effect, ...]]:
    run_id = state.work.run_id or state.latest_run_id
    if not run_id or state.work.graph_loaded or state.work.graph_requested:
        return state, ()
    work = replace(state.work, run_id=run_id, graph_requested=True, unavailable_reason="")
    return replace(state, work=work), (DiscoverWorkGraph(run_id),)


def _ensure_turn(state: AppState, run_id: str) -> AppState:
    if state.turns or not run_id:
        return state
    return replace(state, turns=(TurnState(run_id=run_id),))


def _latest_turn(state: AppState) -> TurnState:
    if not state.turns:
        return TurnState(run_id=state.latest_run_id)
    return state.turns[-1]


def _replace_latest_turn(state: AppState, turn: TurnState) -> AppState:
    if not state.turns:
        return replace(state, turns=(turn,))
    return replace(state, turns=(*state.turns[:-1], turn))


def _upsert_by(values: tuple[Any, ...], value: Any, field: str) -> tuple[Any, ...]:
    identity = getattr(value, field)
    for index, current in enumerate(values):
        if getattr(current, field) == identity:
            return (*values[:index], value, *values[index + 1 :])
    return (*values, value)


def _work_status(value: str) -> WorkStatus:
    aliases = {
        "pending": WorkStatus.WAITING,
        "ready": WorkStatus.WAITING,
        "running": WorkStatus.ACTIVE,
        "succeeded": WorkStatus.COMPLETED,
    }
    normalized = value.strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    try:
        return WorkStatus(normalized)
    except ValueError:
        return WorkStatus.UNKNOWN


def _reported_run_status(value: str) -> RunStatus | None:
    try:
        return RunStatus(value.strip().lower())
    except ValueError:
        return None


def _progress_work(work: WorkMapState, item: ProgressItem) -> WorkMapState:
    existing = next((unit for unit in work.units if unit.unit_id == item.work_unit_id), None)
    unit = existing or WorkUnitState(unit_id=item.work_unit_id, objective=item.text)
    unit = replace(unit, status=_work_status(item.status), progress=item.text)
    units = _upsert_by(work.units, unit, "unit_id")
    selected = work.selected_unit_id or item.work_unit_id
    return replace(work, units=units, selected_unit_id=selected)


def _structured_work_update(work: WorkMapState, payload: Mapping[str, Any]) -> WorkMapState:
    unit_id = _string(payload, "work_unit_id")
    if not unit_id:
        return work
    existing = next((unit for unit in work.units if unit.unit_id == unit_id), None)
    unit = existing or WorkUnitState(unit_id=unit_id)
    attempt = payload.get("attempt")
    unit = replace(
        unit,
        status=_work_status(_string(payload, "state") or _string(payload, "status")),
        agent_id=_string(payload, "worker_id") or unit.agent_id,
        attempt=int(attempt) if isinstance(attempt, int) else unit.attempt,
        started_at=_string(payload, "started_at") or unit.started_at,
        ended_at=_string(payload, "ended_at") or unit.ended_at,
    )
    return replace(
        work,
        units=_upsert_by(work.units, unit, "unit_id"),
        selected_unit_id=work.selected_unit_id or unit_id,
    )


def _tool_work(work: WorkMapState, kind: str, payload: Mapping[str, Any]) -> WorkMapState:
    unit_id = _string(payload, "work_unit_id")
    existing = next((unit for unit in work.units if unit.unit_id == unit_id), None)
    unit = existing or WorkUnitState(unit_id=unit_id)
    tool_id = _string(payload, "tool_call_id") or _string(payload, "tool_id")
    tool_ids = unit.tool_ids
    if tool_id and tool_id not in tool_ids:
        tool_ids = (*tool_ids, tool_id)
    tool_name = _string(payload, "name")
    active_tool = tool_name if kind == "tool.started" else ""
    unit = replace(unit, tool_ids=tool_ids, active_tool=active_tool)
    return replace(
        work,
        units=_upsert_by(work.units, unit, "unit_id"),
        selected_unit_id=work.selected_unit_id or unit_id,
    )


def _terminal_work(
    work: WorkMapState,
    terminal: RunStatus,
    payload: Mapping[str, Any],
) -> WorkMapState:
    units = work.units
    reported = payload.get("work_units")
    if isinstance(reported, list):
        for item in reported:
            if not isinstance(item, Mapping):
                continue
            unit_id = _string(item, "unit_id")
            if not unit_id:
                continue
            existing = next((unit for unit in units if unit.unit_id == unit_id), None)
            unit = existing or WorkUnitState(unit_id=unit_id)
            attempts = item.get("attempts")
            unit = replace(
                unit,
                status=_work_status(_string(item, "status")),
                attempt=int(attempts) if isinstance(attempts, int) else unit.attempt,
            )
            units = _upsert_by(units, unit, "unit_id")
    elif terminal is RunStatus.COMPLETED:
        units = tuple(
            replace(unit, status=WorkStatus.COMPLETED)
            if unit.status in {WorkStatus.ACTIVE, WorkStatus.CHECKING}
            else unit
            for unit in units
        )
    return replace(work, units=units)


def _merge_graph(work: WorkMapState, action: WorkGraphLoaded) -> WorkMapState:
    live = {unit.unit_id: unit for unit in work.units}
    merged: list[WorkUnitState] = []
    for spec in action.units:
        existing = live.pop(spec.unit_id, None)
        if existing is None:
            merged.append(_unit_from_spec(spec))
        else:
            merged.append(
                replace(
                    existing,
                    objective=spec.objective,
                    kind=spec.kind,
                    depends_on=spec.depends_on,
                )
            )
    merged.extend(live.values())
    selected = work.selected_unit_id or (merged[0].unit_id if merged else "")
    return replace(
        work,
        graph_fingerprint=action.graph_fingerprint,
        units=tuple(merged),
        selected_unit_id=selected,
        graph_loaded=True,
        graph_requested=True,
        unavailable_reason="",
    )


def _unit_from_spec(spec: WorkUnitSpec) -> WorkUnitState:
    return WorkUnitState(
        unit_id=spec.unit_id,
        objective=spec.objective,
        kind=spec.kind,
        depends_on=spec.depends_on,
    )


def _notice(state: AppState, message: str, level: str = "info") -> AppState:
    typed_level = level if level in {"info", "warning", "error"} else "info"
    notices = (*state.notices[-3:], Notice(message, typed_level))  # type: ignore[arg-type]
    return replace(state, notices=notices)


def _string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""
