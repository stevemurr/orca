"""Behavioral contract for the terminal application core."""

from __future__ import annotations

from dataclasses import replace

from orca.app.actions import (
    Back,
    ClockTicked,
    CommandCompleted,
    CommandInvoked,
    ComposerSubmitted,
    Connected,
    EventReceived,
    FolderAdded,
    Navigate,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
)
from orca.app.commands import ParsedCommand, parse_input
from orca.app.model import (
    Activity,
    AppState,
    Narration,
    RunStatus,
    Snippet,
    TaskEvent,
    ThreadReplay,
    TurnNote,
    Usage,
    ViewId,
)
from orca.app.update import AddFolder, FollowRun, LoadThread, SendRunCommand, reduce
from orca.backend import Answer, CommandOutcome
from orca.json_types import JsonObject


def event(sequence: int, kind: str, payload: JsonObject) -> TaskEvent:
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
    connected = Connected(
        profile="local",
        endpoint="http://127.0.0.1:8420",
        protocol_version="1.6",
        workspace_id="ws-2",
        workspace_name="other",
        workspace_path="/other",
    )
    initial = AppState(thread_id="thread-explicit")

    booted = reduce(initial, connected).state
    switched = reduce(booted, replace(connected, reset_conversation=True)).state

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


def _running() -> AppState:
    state = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    return feed(state, event(1, "run.created", {"message": "Do it"}))


def test_compaction_steer_and_folder_are_notes_on_the_turn() -> None:
    """Three things the backend says that are neither activity nor answer. They used to be
    dropped as unknown kinds, so a person saw a run change course with no explanation."""
    state = feed(
        _running(),
        event(2, "context.compacted", {"summary": "handoff", "chars_before": 9, "chars_after": 1}),
        event(3, "run.steered", {"content": "use the other parser"}),
        event(4, "folder.added", {"path": "/srv/lib"}),
        event(5, "folder.added", {"path": "/srv/lib"}),
    )

    assert state.folders == ("/srv/lib",)
    assert [note.kind for note in state.turns[-1].notes] == ["compaction", "steer", "folder"]
    assert state.turns[-1].notes[1] == TurnNote("steer", "use the other parser")


def test_a_question_carries_its_options_and_a_number_picks_one() -> None:
    state = feed(
        _running(),
        event(
            2,
            "question.requested",
            {"question_id": "q1", "prompt": "Which?", "options": ["sqlite", "postgres"]},
        ),
    )
    assert state.interaction is not None
    assert state.interaction.options == ("sqlite", "postgres")

    picked = reduce(state, ComposerSubmitted("2"))
    typed = reduce(state, ComposerSubmitted("neither"))
    declined = reduce(state, ComposerSubmitted(""))

    assert picked.effects == (SendRunCommand("run-1", Answer("q1", "postgres")),)
    assert typed.effects == (SendRunCommand("run-1", Answer("q1", "neither")),)
    # The backend treats an empty answer as "I am not answering", which is a real reply.
    assert declined.effects == (SendRunCommand("run-1", Answer("q1", "")),)
    assert reduce(_running(), ComposerSubmitted("")).effects == ()


def test_add_asks_the_backend_and_keeps_the_thread_it_made() -> None:
    fresh = AppState(connected=True, booting=False)

    asked = reduce(fresh, CommandInvoked("add", "/srv/lib"))
    assert asked.effects == (AddFolder(None, "/srv/lib"),)

    widened = reduce(asked.state, FolderAdded("thread-9", ("/srv/app", "/srv/lib"))).state
    assert widened.thread_id == "thread-9"
    assert widened.folders == ("/srv/app", "/srv/lib")
    assert widened.notices[-1].message == "folder added: /srv/app, /srv/lib"

    listed = reduce(widened, CommandInvoked("add", "")).state
    assert listed.notices[-1].message == "folders: /srv/app, /srv/lib"

    # Widening is allowed while a run is going; the backend tells the agent through its inbox.
    assert reduce(_running(), CommandInvoked("add", "/srv/lib")).effects == (
        AddFolder("thread-1", "/srv/lib"),
    )
    # A new conversation reaches the workspace only.
    assert reduce(widened, CommandInvoked("new", "")).state.folders == ()


def _delta(sequence: int, text: str) -> TaskEvent:
    return event(
        sequence, "answer.delta", {"effect_id": "run-1", "model_call_id": "run-1", "text": text}
    )


def _call(sequence: int, update_id: str, status: str) -> TaskEvent:
    return event(
        sequence,
        "run.progress",
        {"update_id": update_id, "text": f"run: {update_id}", "status": status},
    )


def test_a_turn_keeps_words_and_tool_calls_in_the_order_they_happened() -> None:
    """A tool called between two paragraphs used to render above both, because rows and
    answer were kept apart. The timeline records arrival order; an upsert does not move a
    row; a note lands where it was said."""
    state = feed(
        _running(),
        _delta(2, "Looking first."),
        _call(3, "ls", "active"),
        _call(4, "ls", "completed"),
        _delta(5, "\n\nNow the fix."),
        event(6, "run.steered", {"content": "smaller"}),
        _delta(7, " Done."),
    )

    assert state.turns[-1].timeline == (
        Narration("Looking first."),
        Activity("ls"),
        Narration("\n\nNow the fix."),
        TurnNote("steer", "smaller"),
        Narration(" Done."),
    )
    assert state.turns[-1].provisional_answer == "Looking first.\n\nNow the fix. Done."


def test_a_summary_that_is_the_streamed_text_keeps_the_turns_shape() -> None:
    streamed = feed(
        _running(), _delta(2, "First."), _call(3, "ls", "completed"), _delta(4, " Last.")
    )

    same = feed(streamed, event(5, "run.completed", {"summary": "First. Last."}))
    other = feed(streamed, event(5, "run.completed", {"summary": "A different ending."}))

    assert same.turns[-1].timeline == (Narration("First."), Activity("ls"), Narration(" Last."))
    assert same.turns[-1].answer == "First. Last."
    # A summary that is not what was streamed replaces the narration, whole, after the rows.
    assert other.turns[-1].timeline == (Activity("ls"), Narration("A different ending."))


def test_a_new_answer_attempt_drops_the_abandoned_narration_but_not_the_rows() -> None:
    state = feed(
        _running(),
        _delta(2, "abandoned"),
        _call(3, "ls", "completed"),
        event(4, "answer.delta", {"effect_id": "x", "model_call_id": "y", "text": "fresh"}),
    )

    assert state.turns[-1].timeline == (Activity("ls"), Narration("fresh"))


def test_an_approval_carries_the_code_it_is_about() -> None:
    write = feed(
        _running(),
        event(
            2,
            "approval.requested",
            {
                "approval_id": "a1",
                "title": "write src/app.py (20 bytes)",
                "arguments": {"path": "src/app.py", "content": "print('hi')\n"},
                "allowed_decisions": ["approve", "reject"],
            },
        ),
    )
    change = feed(
        _running(),
        event(
            2,
            "approval.requested",
            {
                "approval_id": "a2",
                "title": "edit src/app.py",
                "arguments": {"path": "src/app.py", "old": "a = 1\n", "new": "a = 2\n"},
            },
        ),
    )
    shell = feed(
        _running(),
        event(
            2,
            "approval.requested",
            {"approval_id": "a3", "arguments": {"argv": ["/bin/sh", "-c", "ls"]}},
        ),
    )

    assert write.interaction is not None and write.interaction.snippets == (
        Snippet("src/app.py", "", "print('hi')\n"),
    )
    assert change.interaction is not None
    (diff,) = change.interaction.snippets
    assert diff.language == "diff"
    assert "-a = 1" in diff.text and "+a = 2" in diff.text
    assert shell.interaction is not None and shell.interaction.snippets == ()


def test_a_call_that_wrote_code_carries_it_and_an_upsert_keeps_it() -> None:
    written = event(
        2,
        "run.progress",
        {
            "update_id": "w1",
            "text": "write src/app.py (12 bytes)",
            "status": "active",
            "arguments": {"path": "src/app.py", "content": "print('hi')\n"},
        },
    )
    settled = event(
        3, "run.progress", {"update_id": "w1", "text": "write src/app.py", "status": "completed"}
    )

    state = feed(_running(), written, settled)

    (row,) = state.turns[-1].progress
    assert row.status == "completed"
    assert row.snippets == (Snippet("src/app.py", "", "print('hi')\n"),)


def test_a_cancelled_run_keeps_what_the_model_said_and_says_where_it_stopped() -> None:
    """A cancel used to replace the narration with "Cancelled." and leave the tool rows, so
    a person saw a list of calls and none of the words around them."""
    state = feed(
        _running(),
        _delta(2, "Reading the router."),
        _call(3, "ls", "completed"),
        _delta(4, "\n\nNow editing."),
        event(5, "run.cancelled", {"summary": "Cancelled."}),
    )

    turn = state.turns[-1]
    assert turn.status == "cancelled"
    assert turn.answer == "Reading the router.\n\nNow editing."
    assert turn.timeline == (
        Narration("Reading the router."),
        Activity("ls"),
        Narration("\n\nNow editing."),
        TurnNote("ended", "Cancelled."),
    )


def test_an_approval_is_said_once_near_the_composer_once_decided() -> None:
    from orca.app.actions import ApprovalDecided, OperationFailed

    asked = feed(
        _running(),
        event(
            2,
            "approval.requested",
            {
                "approval_id": "a1",
                "title": "run: pytest",
                "arguments": {"argv": ["/bin/sh", "-c", "pytest"]},
                "allowed_decisions": ["approve"],
            },
        ),
    )
    sent = reduce(asked, ApprovalDecided("approve"))
    assert sent.effects and sent.state.interaction is not None
    assert sent.state.interaction.sending
    # A second key while the first is on its way does nothing.
    assert reduce(sent.state, ApprovalDecided("approve")).effects == ()
    # A send that failed offers the choices again.
    retry = reduce(sent.state, OperationFailed("no route")).state
    assert retry.interaction is not None and not retry.interaction.sending

    decided = feed(
        sent.state, event(3, "approval.resolved", {"approval_id": "a1", "decision": "allow"})
    )
    assert decided.interaction is None
    assert all(not isinstance(item, TurnNote) for item in decided.turns[-1].timeline)
    assert decided.notices[-1].message == "Approved: run: pytest"

    quiet = reduce(decided, CommandCompleted("resolveapproval", CommandOutcome("allow")))
    assert quiet.state.notices == decided.notices


def test_tools_toggles_whether_every_call_is_shown() -> None:
    opened = reduce(AppState(), CommandInvoked("tools", "")).state
    assert opened.tools_expanded
    assert not reduce(opened, CommandInvoked("tools", "")).state.tools_expanded


def test_a_run_carries_its_clock_and_a_new_turn_clears_old_notices() -> None:
    noticed = reduce(AppState(), CommandInvoked("add", "")).state
    assert noticed.notices

    accepted = reduce(noticed, RunAccepted("run-1", "thread-1", 100.0)).state
    ticked = reduce(accepted, ClockTicked(107.5)).state

    assert accepted.notices == ()
    assert (accepted.run_started_at, accepted.clock) == (100.0, 100.0)
    assert ticked.clock == 107.5


def test_usage_is_read_from_the_backend_and_reset_with_the_conversation() -> None:
    state = feed(
        _running(),
        event(2, "context.usage", {"tokens": 1200, "context_window": 8000, "estimated": True}),
    )
    assert state.usage == Usage(1200, 8000, estimated=True)

    finished = feed(state, event(3, "run.completed", {"summary": "Done."}))
    assert finished.usage == Usage(1200, 8000, estimated=True)
    assert reduce(finished, CommandInvoked("new", "")).state.usage is None


def test_a_rows_kind_is_read_from_the_backend_and_survives_an_upsert() -> None:
    state = feed(
        _running(),
        event(
            2,
            "run.progress",
            {"update_id": "r", "text": "read a", "status": "active", "kind": "read"},
        ),
        event(3, "run.progress", {"update_id": "r", "text": "read a", "status": "completed"}),
    )

    assert state.turns[-1].progress[0].kind == "read"


def test_a_slash_suggests_commands_until_an_argument_or_a_message_begins() -> None:
    from orca.app.commands import suggest

    assert [c.name for c in suggest("/")][:3] == ["chat", "review", "threads"]
    assert [c.name for c in suggest("/re")] == ["review", "resume"]
    assert [c.name for c in suggest("/RE")] == ["review", "resume"]
    assert suggest("/mode ") == ()
    assert suggest("/re\nview") == ()
    assert suggest("review") == ()
    assert suggest("/insp") == ()
    assert [c.name for c in suggest("/insp", developer=True)] == ["inspect"]
