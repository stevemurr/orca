"""Behavioral contract for the terminal application core."""

from __future__ import annotations

from orca.app.actions import (
    Back,
    Connected,
    EventReceived,
    Navigate,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
)
from orca.app.commands import ParsedCommand, parse_input
from orca.app.model import AppState, RunStatus, TaskEvent, ThreadReplay, ViewId
from orca.app.update import FollowRun, LoadThread, reduce


def event(sequence: int, kind: str, payload: dict[str, object]) -> TaskEvent:
    return TaskEvent(
        sequence=sequence,
        event_id=f"evt-{sequence}",
        kind=kind,
        visibility="user",
        payload=payload,
    )


def feed(state: AppState, *events: TaskEvent) -> AppState:
    for item in events:
        state = reduce(state, EventReceived(item)).state
    return state


def test_navigation_is_state_not_terminal_side_effect() -> None:
    state = AppState(composer_draft="keep this draft")

    review = reduce(state, Navigate(ViewId.REVIEW)).state
    returned = reduce(review, Back()).state

    assert review.view is ViewId.REVIEW
    assert returned.view is ViewId.CONVERSATION
    assert returned.composer_draft == "keep this draft"


def test_boot_preserves_an_explicit_thread_but_workspace_switch_resets_context() -> None:
    info = {
        "profile": "local",
        "endpoint": "http://127.0.0.1:8420",
        "protocol_version": "1.6",
        "workspace_id": "ws-2",
        "workspace_name": "other",
        "workspace_path": "/other",
        "cwd_relative": ".",
    }
    initial = AppState(thread_id="thread-explicit")

    booted = reduce(initial, Connected(**info)).state
    switched = reduce(booted, Connected(**info, reset_conversation=True)).state

    assert booted.thread_id == "thread-explicit"
    assert switched.thread_id is None
    assert switched.run_status.value == "idle"


def test_command_parser_distinguishes_navigation_from_user_paths() -> None:
    assert parse_input("/review") == ParsedCommand("review", "")
    assert parse_input("/mode plan") == ParsedCommand("mode", "plan")
    assert parse_input("/Users/murr/project") is None
    assert parse_input("ordinary request") is None


def test_event_replay_builds_the_conversation_without_renderables() -> None:
    transition = reduce(AppState(), RunAccepted("run-1", "thread-1"))
    state = transition.state
    state = feed(
        state,
        event(
            1,
            "run.created",
            {
                "message": "Build the terminal shell",
                "mode": "auto",
                "approval_policy": "safe",
            },
        ),
    )
    progress = reduce(
        state,
        EventReceived(
            event(
                2,
                "run.progress",
                {
                    "update_id": "shell",
                    "status": "active",
                    "text": "Working on the terminal shell.",
                },
            )
        ),
    )
    state = progress.state

    assert progress.effects == ()
    assert state.turns[-1].request == "Build the terminal shell"
    assert state.turns[-1].progress[0].text == "Working on the terminal shell."

    state = feed(
        state,
        event(
            4,
            "answer.delta",
            {"effect_id": "answer", "model_call_id": "call-1", "text": "Almost "},
        ),
        event(
            5,
            "answer.delta",
            {"effect_id": "answer", "model_call_id": "call-1", "text": "done."},
        ),
        event(
            6,
            "run.completed",
            {"summary": "Implemented the terminal shell."},
        ),
    )

    turn = state.turns[-1]
    assert turn.provisional_answer == ""
    assert turn.answer == "Implemented the terminal shell."
    assert state.active_run_id is None
    assert state.cursor == 6


def test_a_new_answer_attempt_replaces_abandoned_stream_and_duplicate_is_ignored() -> None:
    state = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    first = event(
        1,
        "answer.delta",
        {"effect_id": "answer", "model_call_id": "call-1", "text": "abandoned"},
    )
    second = event(
        2,
        "answer.delta",
        {"effect_id": "answer", "model_call_id": "call-2", "text": "replacement"},
    )

    state = feed(state, first, first, second)

    assert state.turns[-1].provisional_answer == "replacement"
    assert state.cursor == 2


def test_selecting_a_thread_starts_a_clean_continuation_context() -> None:
    active = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    completed = feed(active, event(1, "run.completed", {"summary": "Done."}))

    selection = reduce(completed, ThreadSelected("thread-2", "Polish the terminal"))
    loaded = reduce(
        selection.state,
        ThreadLoaded(
            "thread-2",
            "Polish the terminal",
            (
                ThreadReplay(
                    "run-2",
                    "running",
                    (event(1, "run.created", {"message": "Keep polishing"}),),
                ),
            ),
        ),
    )
    selected = loaded.state

    assert selection.effects == (LoadThread("thread-2", "Polish the terminal"),)
    assert selected.thread_id == "thread-2"
    assert selected.turns[-1].request == "Keep polishing"
    assert selected.view is ViewId.CONVERSATION
    assert selected.notices[-1].message == "Continuing Polish the terminal"
    assert loaded.effects == (FollowRun("run-2", 1),)


def test_unknown_public_event_is_an_additive_no_op_except_for_cursor() -> None:
    state = feed(AppState(), event(7, "future.event", {"anything": True}))

    assert state.cursor == 7
    assert state.turns == ()


def test_developer_replay_has_an_independent_cursor_from_public_events() -> None:
    state = AppState(cursor=10, developer=True)
    developer_event = TaskEvent(
        sequence=4,
        event_id="dev-4",
        kind="control.transition",
        visibility="developer",
        payload={},
    )

    state = feed(state, developer_event, developer_event)

    assert state.cursor == 10
    assert state.developer_cursor == 4
    assert state.developer_events == ("   4  control.transition",)


def test_a_published_plan_replaces_the_previous_one_wholesale() -> None:
    """`plan.progress` carries the model's complete current checklist, so the client replaces.

    Merging by step text would resurrect a step the model deliberately dropped, which is exactly
    the state a person watching would misread as work still outstanding.
    """

    state = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    state = feed(
        state,
        event(1, "run.created", {"message": "Add the endpoint"}),
        event(
            2,
            "plan.progress",
            {
                "explanation": "",
                "plan": [
                    {"step": "read the router", "status": "in_progress"},
                    {"step": "add the handler", "status": "pending"},
                    {"step": "delete the old one", "status": "pending"},
                ],
            },
        ),
        event(
            3,
            "plan.progress",
            {
                "explanation": "the old handler was already gone",
                "plan": [
                    {"step": "read the router", "status": "completed"},
                    {"step": "add the handler", "status": "in_progress"},
                ],
            },
        ),
    )

    turn = state.turns[-1]
    assert [(step.step, step.status) for step in turn.plan] == [
        ("read the router", "completed"),
        ("add the handler", "in_progress"),
    ]
    assert turn.plan_explanation == "the old handler was already gone"


def test_a_plan_the_server_never_counted_is_rendered_as_it_arrived() -> None:
    """The server states "at most one in_progress" and enforces nothing, so neither does this.

    A client that repaired the list here would be counting what the contract says nobody counts,
    and would show a person something the model did not say.
    """

    state = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    state = feed(
        state,
        event(
            1,
            "plan.progress",
            {
                "plan": [
                    {"step": "one", "status": "in_progress"},
                    {"step": "two", "status": "in_progress"},
                    {"step": "three", "status": "invented"},
                    {"step": "   ", "status": "pending"},
                    "not a step at all",
                ]
            },
        ),
    )

    assert [(step.step, step.status) for step in state.turns[-1].plan] == [
        ("one", "in_progress"),
        ("two", "in_progress"),
        ("three", "invented"),
    ]


def test_a_plan_event_without_a_list_changes_nothing_but_the_cursor() -> None:
    state = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    state = feed(
        state,
        event(1, "plan.progress", {"plan": [{"step": "one", "status": "pending"}]}),
        event(2, "plan.progress", {"explanation": "no list here"}),
    )

    assert [step.step for step in state.turns[-1].plan] == ["one"]
    assert state.cursor == 2


def test_a_resumed_run_stops_reading_as_paused() -> None:
    """`run.paused` had no counterpart, so a resumed run showed "paused" until it ended.

    Neither side was at fault: unknown events are ignorable by design, so the backend emitted
    `run.resumed` and orca dropped it. The hole was in the contract -- a state you could enter
    and not leave. (2026-08-30)
    """
    state = feed(
        AppState(),
        event(1, "run.created", {"message": "do it"}),
        event(2, "run.paused", {}),
    )
    assert state.run_status is RunStatus.PAUSED

    resumed = feed(state, event(3, "run.resumed", {}))

    assert resumed.run_status is RunStatus.RUNNING
