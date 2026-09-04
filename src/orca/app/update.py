"""Pure update function for terminal state and requested side effects."""

from __future__ import annotations

import difflib
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import assert_never

from orca.app.actions import (
    Action,
    ApprovalDecided,
    Back,
    ClockTicked,
    CommandCompleted,
    CommandInvoked,
    ComposerChanged,
    ComposerSubmitted,
    Connected,
    ConnectFailed,
    EventReceived,
    FolderAdded,
    Navigate,
    OperationFailed,
    QuestionAnswered,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
    ViewportChanged,
)
from orca.app.commands import parse_input
from orca.app.model import (
    Activity,
    AgentState,
    AppState,
    ArtifactOffer,
    Choice,
    InteractionState,
    Narration,
    Notice,
    NoticeLevel,
    PlanStep,
    ProgressItem,
    RunStatus,
    Segment,
    Snippet,
    TaskEvent,
    TurnNote,
    TurnNoteKind,
    TurnState,
    Usage,
    ViewId,
)
from orca.backend import (
    Answer,
    Cancel,
    Command,
    Pause,
    ResolveApproval,
    Resume,
    Steer,
)
from orca.json_types import JsonObject


@dataclass(frozen=True, slots=True)
class StartRun:
    message: str


@dataclass(frozen=True, slots=True)
class SendRunCommand:
    run_id: str
    command: Command


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
class AddFolder:
    """Widen the conversation to one more folder. `thread_id` is None before the first
    message; the backend then makes the thread and `FolderAdded` carries its id back."""

    thread_id: str | None
    path: str


@dataclass(frozen=True, slots=True)
class SwitchWorkspace:
    selector: str


@dataclass(frozen=True, slots=True)
class ExitApplication:
    pass


Effect = (
    StartRun
    | SendRunCommand
    | OpenHelp
    | OpenThreads
    | LoadThread
    | FollowRun
    | SwitchWorkspace
    | AddFolder
    | ExitApplication
)


@dataclass(frozen=True, slots=True)
class Transition:
    state: AppState
    effects: tuple[Effect, ...] = ()


def reduce(state: AppState, action: Action) -> Transition:
    """Apply one typed action without performing I/O or rendering."""

    if isinstance(action, Connected):
        conversation = (
            {
                "thread_id": None,
                "active_run_id": None,
                "cursor": 0,
                "run_status": RunStatus.IDLE,
                "turns": (),
                "interaction": None,
                "view_stack": (ViewId.CONVERSATION,),
                "developer_cursor": 0,
                "developer_events": (),
                "folders": (),
                "usage": None,
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
                modes=action.modes,
                policies=action.policies,
                skills=action.skills,
                **conversation,
            )
        )
    if isinstance(action, ConnectFailed):
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
        asked = (
            state.interaction
            if state.interaction and state.interaction.kind == "question"
            else None
        )
        if not text and asked is None:
            return Transition(state)
        parsed = parse_input(text)
        cleared = replace(state, composer_draft="")
        if parsed is not None:
            return reduce(cleared, CommandInvoked(parsed.name, parsed.argument))
        if asked is not None:
            # A number picks one of the offered options; anything else, including nothing,
            # is the answer as typed. The backend treats an empty answer as a real reply.
            return reduce(cleared, QuestionAnswered(_chosen_option(asked.options, text)))
        if state.active_run_id:
            return Transition(
                cleared,
                (SendRunCommand(state.active_run_id, Steer(text)),),
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
                interaction=None,
                developer_cursor=0,
                developer_events=(),
                # A new turn is a new page: what was said about the last one has been read.
                notices=(),
                run_started_at=action.started_at,
                clock=action.started_at,
            )
        )
    if isinstance(action, ClockTicked):
        return Transition(replace(state, clock=action.now))
    if isinstance(action, OperationFailed):
        failed = replace(state, submitting=False)
        asked = state.interaction
        if asked is not None and asked.kind == "approval" and asked.sending:
            # The decision did not reach the backend; offer the choices again.
            failed = replace(failed, interaction=replace(asked, sending=False))
        return Transition(_notice(failed, action.message, "error"))
    if isinstance(action, EventReceived):
        return _event(state, action.event)
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
            interaction=None,
            view_stack=(ViewId.CONVERSATION,),
            submitting=False,
            developer_cursor=0,
            developer_events=(),
            folders=(),
            usage=None,
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
                reported is not None
                and reported
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
        if interaction.sending:
            return Transition(state)
        return Transition(
            replace(state, interaction=replace(interaction, sending=True)),
            (
                SendRunCommand(
                    state.active_run_id,
                    ResolveApproval(interaction.request_id, action.decision),
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
                    Answer(interaction.request_id, action.answer),
                ),
            ),
        )
    if isinstance(action, FolderAdded):
        added = [folder for folder in action.folders if folder not in state.folders]
        label = ", ".join(added) if added else "no change"
        return Transition(
            _notice(
                replace(state, thread_id=action.thread_id, folders=action.folders),
                f"folder added: {label}",
            )
        )
    match action:
        case CommandCompleted():
            if action.command in {"resolveapproval", "answer"}:
                # The decision is already in the transcript, where `approval.resolved`
                # put it; a notice beside it would say the same thing twice and stay.
                return Transition(state)
            return Transition(
                _notice(state, f"{action.command}: {action.outcome.status or 'sent'}")
            )
        case _:
            # A new `Action` member fails here at type-check time, not as a runtime TypeError.
            assert_never(action)


def _command(state: AppState, name: str, argument: str) -> Transition:
    routes = {
        "chat": ViewId.CONVERSATION,
        "review": ViewId.REVIEW,
        "inspect": ViewId.INSPECTOR,
        "agents": ViewId.AGENTS,
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
                view_stack=(ViewId.CONVERSATION,),
                developer_cursor=0,
                developer_events=(),
                folders=(),
                usage=None,
            )
        )
    if name == "add":
        if not argument:
            reach = ", ".join(state.folders) if state.folders else "the workspace only"
            return Transition(_notice(state, f"folders: {reach}"))
        # Allowed while a run is going: the backend tells the agent through its inbox and the
        # stream says so. Only the thread is needed, and the backend makes one if there is none.
        return Transition(state, (AddFolder(state.thread_id, argument),))
    if name == "mode":
        return _choose(state, name, argument, state.mode, state.modes)
    if name == "permissions":
        return _choose(state, name, argument, state.policy, state.policies)
    if name == "workspace":
        if not argument:
            return Transition(_notice(state, f"workspace: {state.workspace_path or 'none'}"))
        if state.active_run_id:
            return Transition(
                _notice(state, "The workspace cannot change while a run is active.", "warning")
            )
        return Transition(state, (SwitchWorkspace(argument),))
    if name == "tools":
        return Transition(replace(state, tools_expanded=not state.tools_expanded))
    if name == "status":
        label = state.run_status.value.replace("_", " ")
        return Transition(_notice(state, f"{state.profile} · {state.workspace_path} · {label}"))
    if (lifecycle := {"pause": Pause, "resume": Resume, "cancel": Cancel}.get(name)) is not None:
        if not state.active_run_id:
            return Transition(_notice(state, "No run is active."))
        return Transition(state, (SendRunCommand(state.active_run_id, lifecycle()),))
    return Transition(_notice(state, f"Unknown command: /{name}", "warning"))


def _choose(
    state: AppState, name: str, argument: str, current: str, offered: tuple[Choice, ...]
) -> Transition:
    """Set `/mode` or `/permissions`. Asked with nothing, it says what the value is and what
    else it could be; given a value the backend did not offer, it says what it did, since
    a typo sent on would fail at the backend with a worse message or, worse, not fail."""
    names = tuple(choice.name for choice in offered)
    if not argument:
        others = ", ".join(value for value in names if value != current)
        return Transition(
            _notice(state, f"{name}: {current}" + (f" · also {others}" if others else ""))
        )
    if names and argument not in names:
        return Transition(
            _notice(
                state,
                f"{name}: the backend does not offer {argument!r}; it offers {', '.join(names)}.",
                "warning",
            )
        )
    if name == "mode":
        return Transition(replace(state, mode=argument))
    return Transition(replace(state, policy=argument))


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
        turn = _latest_turn(state)
        agent_id = _string(payload, "agent_id")
        agent = _agent(turn, agent_id) if agent_id else None
        rows = agent.progress if agent is not None else turn.progress
        held = next((entry for entry in rows if entry.update_id == update_id), None)
        arguments = payload.get("arguments")
        item = ProgressItem(
            update_id=update_id,
            text=text,
            status=_string(payload, "status") or "active",
            # An event without arguments does not take away the code an earlier one showed.
            snippets=_snippets(arguments)
            if isinstance(arguments, Mapping)
            else (held.snippets if held is not None else ()),
            kind=_string(payload, "kind") or (held.kind if held is not None else ""),
            tool=_string(payload, "tool") or (held.tool if held is not None else ""),
            detail=_detail(arguments)
            if isinstance(arguments, Mapping)
            else (held.detail if held is not None else ""),
            agent_id=agent_id,
        )
        if agent is not None:
            # A delegated agent's row belongs to it, not to the turn's timeline: the
            # timeline is the parent's story, and the agent is drawn as its own.
            rows = _upsert_by(agent.progress, item, lambda entry: entry.update_id)
            return Transition(
                _replace_latest_turn(
                    state,
                    replace(_with_agent(turn, replace(agent, progress=rows)), status="running"),
                )
            )
        progress = _upsert_by(turn.progress, item, lambda entry: entry.update_id)
        timeline = turn.timeline
        if all(entry.update_id != update_id for entry in turn.progress):
            timeline = (*timeline, Activity(update_id))
        state = _replace_latest_turn(
            state, replace(turn, progress=progress, timeline=timeline, status="running")
        )
        return Transition(state)

    if kind == "answer.delta":
        text = _string(payload, "text")
        if not text:
            return Transition(state)
        turn = _latest_turn(state)
        stream = (_string(payload, "effect_id"), _string(payload, "model_call_id"))
        same_attempt = stream == turn.answer_stream
        previous = turn.provisional_answer if same_attempt else ""
        timeline = turn.timeline if same_attempt else _without_narration(turn.timeline)
        if timeline and isinstance(timeline[-1], Narration):
            timeline = (*timeline[:-1], Narration(timeline[-1].text + text))
        else:
            timeline = (*timeline, Narration(text))
        turn = replace(
            turn,
            provisional_answer=previous + text,
            answer_stream=stream,
            timeline=timeline,
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
                replace(turn, plan=steps, plan_explanation=_string(payload, "explanation").strip()),
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
                state,
                replace(
                    turn,
                    artifacts=_upsert_by(turn.artifacts, offer, lambda entry: entry.artifact_id),
                ),
            )
        )

    if kind == "approval.requested":
        allowed = payload.get("allowed_decisions")
        decisions = tuple(str(item) for item in allowed) if isinstance(allowed, list) else ()
        arguments = payload.get("arguments")
        command = ""
        snippets: tuple[Snippet, ...] = ()
        if isinstance(arguments, Mapping):
            argv = arguments.get("argv")
            if isinstance(argv, list):
                words = [item for item in argv if isinstance(item, str)]
                if len(words) == len(argv):
                    command = shlex.join(words)
            snippets = _snippets(arguments)
        interaction = InteractionState(
            kind="approval",
            request_id=_string(payload, "approval_id") or event.event_id,
            title=_string(payload, "title") or "Approve this action?",
            summary=_string(payload, "summary"),
            command=command,
            risk=_string(payload, "risk"),
            allowed_decisions=decisions,
            snippets=snippets,
            grant=_string(payload, "grant"),
        )
        return Transition(
            replace(state, interaction=interaction, run_status=RunStatus.AWAITING_APPROVAL)
        )

    if kind == "question.requested":
        offered = payload.get("options")
        interaction = InteractionState(
            kind="question",
            request_id=_string(payload, "question_id") or event.event_id,
            title=_string(payload, "prompt") or "The agent needs more information.",
            options=tuple(str(item) for item in offered if str(item).strip())
            if isinstance(offered, list)
            else (),
        )
        return Transition(
            replace(state, interaction=interaction, run_status=RunStatus.AWAITING_INPUT)
        )

    if kind == "approval.resolved":
        # The prompt leaves the transcript with its answer said once, near the composer, and
        # gone: the tool row that follows shows what was done, and that is the record.
        decided = replace(state, interaction=None, run_status=RunStatus.RUNNING)
        asked = state.interaction
        title = asked.title if asked is not None and asked.kind == "approval" else ""
        verdict = _decision_label(_string(payload, "decision"))
        return Transition(_notice(decided, f"{verdict}: {title}" if title else verdict))

    if kind == "question.resolved":
        return Transition(replace(state, interaction=None, run_status=RunStatus.RUNNING))

    if kind == "context.usage":
        tokens, window = payload.get("tokens"), payload.get("context_window")
        if not isinstance(tokens, int) or not isinstance(window, int):
            return Transition(state)
        return Transition(
            replace(state, usage=Usage(tokens, window, payload.get("estimated") is True))
        )

    if kind == "agent.started":
        agent_id = _string(payload, "agent_id")
        if not agent_id:
            return Transition(state)
        turn = _latest_turn(state)
        agent = replace(
            _agent(turn, agent_id),
            task=_string(payload, "task"),
            status="running",
            started_at=state.clock,
        )
        state = _replace_latest_turn(state, _with_agent(turn, agent))
        return Transition(_note(state, "agent", f"{agent_id} started: {_one_line(agent.task)}"))

    if kind == "agent.said":
        agent_id = _string(payload, "agent_id")
        text = _string(payload, "text").strip()
        if not agent_id or not text:
            return Transition(state)
        turn = _latest_turn(state)
        agent = _agent(turn, agent_id)
        agent = replace(agent, said=(*agent.said, text))
        return Transition(_replace_latest_turn(state, _with_agent(turn, agent)))

    if kind in {"agent.finished", "agent.failed", "agent.stopped"}:
        agent_id = _string(payload, "agent_id")
        if not agent_id:
            return Transition(state)
        turn = _latest_turn(state)
        agent = _agent(turn, agent_id)
        status = {"agent.finished": "finished", "agent.failed": "failed"}.get(kind, "stopped")
        seconds = payload.get("seconds")
        turns = payload.get("turns")
        agent = replace(
            agent,
            status=status,
            answer=_string(payload, "answer") or agent.answer,
            seconds=float(seconds) if isinstance(seconds, int | float) else agent.seconds,
            turns=int(turns) if isinstance(turns, int) else agent.turns,
        )
        state = _replace_latest_turn(state, _with_agent(turn, agent))
        if kind == "agent.finished":
            said = f"{agent_id} finished after {agent.turns} turns"
            if agent.answer:
                said += f": {_one_line(agent.answer)}"
        elif kind == "agent.failed":
            said = f"{agent_id} failed: {_string(payload, 'error') or 'no reason given'}"
        else:
            said = f"{agent_id} stopped"
        return Transition(_note(state, "agent", said))

    if kind == "context.compacted":
        # A `user` row on purpose: the agent now works from a summary, which is the honest
        # explanation for any change in how it behaves next.
        return Transition(
            _note(state, "compaction", "Context compacted; the agent continues from a summary.")
        )

    if kind == "run.steered":
        return Transition(_note(state, "steer", _string(payload, "content").strip()))

    if kind == "folder.added":
        path = _string(payload, "path").strip()
        if not path or path in state.folders:
            # Already reached: the widening was answered on the command, or replayed.
            return Transition(state)
        folders = (*state.folders, path)
        return Transition(_note(replace(state, folders=folders), "folder", f"Added folder {path}"))

    if kind == "run.paused":
        return Transition(replace(state, run_status=RunStatus.PAUSED))

    if kind == "run.resumed":
        # The contract listed `run.paused` and no way back, so a resumed run read as paused
        # until it ended. Neither side was violating anything -- unknown events are ignorable
        # by design, so the backend emitted this and orca dropped it. The hole was in the
        # contract: a state you can enter and not leave. (2026-08-30)
        return Transition(replace(state, run_status=RunStatus.RUNNING))

    terminal = {
        "run.completed": RunStatus.COMPLETED,
        "run.failed": RunStatus.FAILED,
        "run.cancelled": RunStatus.CANCELLED,
        "run.blocked": RunStatus.BLOCKED,
    }.get(kind)
    if terminal is not None:
        turn = _latest_turn(state)
        summary = _string(payload, "summary")
        timeline = turn.timeline
        if terminal is RunStatus.COMPLETED:
            # The summary replaces what was streamed. When it is the streamed text -- which
            # is what the harness sends -- the turn keeps its shape, words and tool calls in
            # the order they happened; otherwise the narration is replaced by the summary.
            # The same words are the same text: a backend joins its messages with blank
            # lines and strips each, and a stream has neither, so the two differed by
            # whitespace on every real run. That put every tool call above the whole
            # answer at the end of a run, and on a long turn scrolled them out of sight.
            answer = summary
            if not _same_words(summary, turn.provisional_answer):
                timeline = _without_narration(timeline)
                if summary:
                    timeline = (*timeline, Narration(summary))
        else:
            # A cancel or a failure says why the run stopped, not what it answered. What
            # the model said stays -- a cancelled run used to lose every word and keep
            # only its tool rows -- and the reason lands where the run ended.
            answer = turn.provisional_answer
            if summary:
                timeline = (*timeline, TurnNote("ended", summary))
        turn = replace(
            turn,
            answer=answer,
            provisional_answer="",
            status=terminal.value,
            timeline=timeline,
        )
        return Transition(
            _replace_latest_turn(
                replace(
                    state,
                    active_run_id=None,
                    run_status=terminal,
                    interaction=None,
                    submitting=False,
                ),
                turn,
            )
        )

    return Transition(state)


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


def _upsert_by[T](values: tuple[T, ...], value: T, key: Callable[[T], object]) -> tuple[T, ...]:
    identity = key(value)
    for index, current in enumerate(values):
        if key(current) == identity:
            return (*values[:index], value, *values[index + 1 :])
    return (*values, value)


def _reported_run_status(value: str) -> RunStatus | None:
    try:
        return RunStatus(value.strip().lower())
    except ValueError:
        return None


def _agent(turn: TurnState, agent_id: str) -> AgentState:
    """The turn's agent by id, or a new one: a row or a word may arrive before the start
    event when a client joins mid-run, and neither should be dropped for it."""
    return next(
        (agent for agent in turn.agents if agent.agent_id == agent_id), AgentState(agent_id)
    )


def _with_agent(turn: TurnState, agent: AgentState) -> TurnState:
    return replace(turn, agents=_upsert_by(turn.agents, agent, lambda entry: entry.agent_id))


def _note(state: AppState, kind: TurnNoteKind, text: str) -> AppState:
    """Attach a note to the latest turn, or leave the state alone when there is nothing to say."""
    if not text:
        return state
    turn = _latest_turn(state)
    note = TurnNote(kind, text)
    return _replace_latest_turn(
        state, replace(turn, notes=(*turn.notes, note), timeline=(*turn.timeline, note))
    )


def _same_words(one: str, other: str) -> bool:
    """Whether two texts differ only in whitespace."""
    return one.split() == other.split()


def _without_narration(timeline: tuple[Segment, ...]) -> tuple[Segment, ...]:
    return tuple(item for item in timeline if not isinstance(item, Narration))


#: The argument that says what a call was pointed at, by the names tools give it, most
#: telling first. A tool with none of these shows its first string argument.
_DETAIL_KEYS = ("path", "command", "query", "pattern", "url", "agent_id", "task", "name")


def _detail(arguments: JsonObject) -> str:
    """One line naming what the call was about: `src/app.py`, `uv run pytest -q`."""
    for key in _DETAIL_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return _one_line(value)
    for value in arguments.values():
        if isinstance(value, str) and value.strip():
            return _one_line(value)
    return ""


def _one_line(value: str, limit: int = 120) -> str:
    first = value.strip().splitlines()[0]
    return first if len(first) <= limit else first[: limit - 1] + "…"


def _snippets(arguments: JsonObject) -> tuple[Snippet, ...]:
    """The code a request's arguments carry, if any, in the shape a person judges it in.

    A write is the file as it will be; an edit is the change as a diff. Which one is told by
    the arguments themselves -- `content`, or `old` and `new` -- because the request does not
    name its tool and the arguments are the fact being approved.
    """
    path = _string(arguments, "path")
    content = arguments.get("content")
    if isinstance(content, str):
        return (Snippet(path or "file", "", content),)
    old, new = arguments.get("old"), arguments.get("new")
    if isinstance(old, str) and isinstance(new, str):
        change = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=path or "before",
                tofile=path or "after",
            )
        )
        return (Snippet(path or "edit", "diff", change),)
    return ()


def _decision_label(decision: str) -> str:
    """The backend's decision word, as a person would say it. Unknown words pass through."""
    return {
        "allow": "Approved",
        "allow_always": "Approved, and always from now on",
        "deny": "Rejected",
    }.get(decision, decision or "Decided")


def _chosen_option(options: tuple[str, ...], text: str) -> str:
    """`2` means the second offered option; anything else is the answer as typed."""
    if text.isdigit() and 1 <= int(text) <= len(options):
        return options[int(text) - 1]
    return text


def _notice(state: AppState, message: str, level: NoticeLevel = "info") -> AppState:
    notices = (*state.notices[-3:], Notice(message, level, shown_at=state.clock))
    return replace(state, notices=notices)


def _string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""
