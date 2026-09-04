"""Behavioral contract for the terminal application core."""

from __future__ import annotations

from dataclasses import replace

from orca.app.actions import (
    ApprovalDecided,
    Back,
    ClockTicked,
    CommandCompleted,
    CommandInvoked,
    ComposerSubmitted,
    Connected,
    ConnectFailed,
    EventReceived,
    FolderAdded,
    Navigate,
    OperationFailed,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
)
from orca.app.commands import ParsedCommand, parse_input
from orca.app.model import (
    NOTICE_SECONDS,
    Activity,
    AppState,
    Choice,
    Narration,
    RunStatus,
    Snippet,
    TaskEvent,
    ThreadReplay,
    TurnNote,
    Usage,
    ViewId,
)
from orca.app.update import (
    AddFolder,
    FollowRun,
    LoadThread,
    SendRunCommand,
    StartRun,
    SwitchWorkspace,
    reduce,
)
from orca.backend import Answer, Cancel, CommandOutcome
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
                "mode": "normal",
                "approval_policy": "ask",
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
        reduce(AppState(), RunAccepted("run-1", "thread-1")).state,
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


def test_a_summary_that_differs_only_in_whitespace_keeps_the_turn_in_order() -> None:
    """The harness joins its messages with blank lines and strips each; the stream has
    neither. The words are the same, so the tool calls stay where they happened rather
    than being pushed above the whole answer."""
    from orca.app.actions import EventReceived, RunAccepted
    from orca.app.model import Activity, Narration

    def ev(sequence: int, kind: str, payload: JsonObject) -> EventReceived:
        return EventReceived(TaskEvent(sequence, f"e{sequence}", kind, "user", payload))

    state = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    state = reduce(state, ev(1, "run.created", {"message": "fix it"})).state
    delta = {"effect_id": "e", "model_call_id": "m", "text": "Looking first. "}
    state = reduce(state, ev(2, "answer.delta", delta)).state
    state = reduce(state, ev(3, "run.progress", {"update_id": "r", "text": "read a.py"})).state
    state = reduce(state, ev(4, "answer.delta", {**delta, "text": "Done. "})).state

    same = reduce(state, ev(5, "run.completed", {"summary": "Looking first.\n\nDone."})).state
    turn = same.turns[-1]
    assert [type(s).__name__ for s in turn.timeline] == ["Narration", "Activity", "Narration"]
    assert turn.answer == "Looking first.\n\nDone."

    # A genuinely different summary still replaces the words, as the contract says.
    other = reduce(state, ev(5, "run.completed", {"summary": "Something else."})).state
    assert [type(s).__name__ for s in other.turns[-1].timeline] == ["Activity", "Narration"]
    assert isinstance(other.turns[-1].timeline[0], Activity)
    assert isinstance(other.turns[-1].timeline[-1], Narration)


def test_an_activity_row_keeps_the_tool_and_what_it_was_pointed_at() -> None:
    from orca.app.actions import EventReceived
    from orca.app.model import TurnState

    def progress(sequence: int, payload: JsonObject) -> EventReceived:
        return EventReceived(TaskEvent(sequence, f"e{sequence}", "run.progress", "user", payload))

    state = replace(AppState(), turns=(TurnState("run-1"),), active_run_id="run-1")
    state = reduce(
        state,
        progress(
            1,
            {
                "update_id": "r",
                "text": "read src/app.py",
                "status": "active",
                "tool": "read_file",
                "arguments": {"path": "src/app.py"},
            },
        ),
    ).state
    assert state.turns[-1].progress[0].tool == "read_file"
    assert state.turns[-1].progress[0].detail == "src/app.py"

    # A later event for the row without arguments keeps what the first one said.
    state = reduce(
        state, progress(2, {"update_id": "r", "text": "read src/app.py", "status": "completed"})
    ).state
    assert state.turns[-1].progress[0].detail == "src/app.py"

    # A command is its own detail; a multi-line one is its first line.
    state = reduce(
        state,
        progress(
            3,
            {
                "update_id": "x",
                "text": "run: ls",
                "tool": "run",
                "arguments": {"command": "ls -la\necho done", "background": False},
            },
        ),
    ).state
    assert state.turns[-1].progress[1].detail == "ls -la"


def test_a_named_command_suggests_the_values_the_backend_offers() -> None:
    from orca.app.commands import suggest

    choices = {
        "mode": (Choice("normal"), Choice("plan")),
        "permissions": (
            Choice("ask", "Ask before anything that changes the machine."),
            Choice("edits"),
            Choice("full-access"),
        ),
    }

    assert [s.name for s in suggest("/permissions ", choices=choices)] == [
        "ask",
        "edits",
        "full-access",
    ]
    assert [s.insert for s in suggest("/mode p", choices=choices)] == ["/mode plan"]
    assert suggest("/mode plan x", choices=choices) == ()
    assert suggest("/add ", choices=choices) == ()
    row = suggest("/perm", choices=choices)[0]
    assert row.argument == "ask | edits | full-access"
    assert not row.runnable
    assert suggest("/perm")[0].argument == "<policy>"
    # The summary beside a value is what the backend said it means, or the command it runs.
    assert suggest("/permissions a", choices=choices)[0].summary.startswith("Ask before")
    assert suggest("/permissions e", choices=choices)[0].summary == "/permissions edits"


def test_a_workspace_skill_is_offered_like_a_command_and_left_for_a_request() -> None:
    from orca.app.commands import suggest

    skills = (Choice("deploy", "Ship a release."), Choice("triage", "Sort the inbox."))

    rows = suggest("/", skills=skills)
    assert [row.name for row in rows][-2:] == ["deploy", "triage"]
    deploy = suggest("/dep", skills=skills)[0]
    assert deploy.label == "/deploy"
    assert deploy.summary == "Ship a release."
    assert deploy.insert == "/deploy "
    assert not deploy.runnable
    # A skill named like a command does not shadow the command.
    assert [row.name for row in suggest("/rev", skills=(Choice("review"),))] == ["review"]
    assert suggest("/rev", skills=(Choice("review"),))[0].runnable


def test_a_setting_outside_what_the_backend_offers_is_refused() -> None:
    from orca.app.actions import CommandInvoked, Connected

    connected = reduce(
        AppState(),
        Connected("p", "e", "1", "ws", "n", "/n", policies=(Choice("ask"), Choice("edits"))),
    ).state

    refused = reduce(connected, CommandInvoked("permissions", "yolo")).state
    assert refused.policy == "ask"
    assert refused.notices[-1].level == "warning"
    assert "ask, edits" in refused.notices[-1].message

    assert reduce(connected, CommandInvoked("permissions", "edits")).state.policy == "edits"
    # Nothing advertised for modes, so anything goes, as before.
    assert reduce(connected, CommandInvoked("mode", "anything")).state.mode == "anything"

    told = reduce(connected, CommandInvoked("permissions", "")).state
    assert told.notices[-1].message == "permissions: ask · also edits"


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


def test_a_delegated_agent_is_its_own_thing_not_rows_in_the_parents_timeline() -> None:
    """The backend used to send a child's calls as rows with its id in the text and its
    words as the parent's answer. Now the child's rows carry `agent_id`, its words arrive
    as `agent.said`, and its life is bounded by `agent.started` and `agent.finished`."""
    state = reduce(AppState(), RunAccepted("run-1", "thread-1", started_at=100.0)).state
    state = feed(
        state,
        event(
            1, "run.created", {"message": "look around", "mode": "normal", "approval_policy": "ask"}
        ),
        event(
            2,
            "run.progress",
            {
                "update_id": "d1",
                "text": "delegate",
                "status": "completed",
                "tool": "delegate",
                "arguments": {"task": "what is here?"},
            },
        ),
        event(3, "agent.started", {"agent_id": "agent_1", "task": "what is here?"}),
        event(
            4,
            "run.progress",
            {
                "update_id": "c1",
                "text": "list .",
                "status": "active",
                "tool": "list_dir",
                "agent_id": "agent_1",
                "arguments": {"path": "."},
            },
        ),
        event(
            5,
            "run.progress",
            {
                "update_id": "c1",
                "text": "list .",
                "status": "completed",
                "tool": "list_dir",
                "agent_id": "agent_1",
            },
        ),
        event(6, "agent.said", {"agent_id": "agent_1", "text": "notes.md, nothing else"}),
        event(
            7,
            "answer.delta",
            {"effect_id": "a", "model_call_id": "a", "text": "the child says notes.md"},
        ),
    )
    turn = state.turns[-1]

    (agent,) = turn.agents
    assert agent.task == "what is here?" and agent.running and agent.started_at == 100.0
    assert [row.update_id for row in agent.progress] == ["c1"]
    assert agent.progress[0].status == "completed" and agent.progress[0].tool == "list_dir"
    assert agent.said == ("notes.md, nothing else",)
    # The parent's own rows and words are untouched by the child's.
    assert [row.update_id for row in turn.progress] == ["d1"]
    assert turn.provisional_answer == "the child says notes.md"
    assert [note.text for note in turn.notes] == ["agent_1 started: what is here?"]

    state = feed(
        state,
        event(
            8,
            "agent.finished",
            {
                "agent_id": "agent_1",
                "turns": 2,
                "stop": "done",
                "answer": "notes.md, nothing else",
                "seconds": 3.5,
            },
        ),
    )
    (agent,) = state.turns[-1].agents
    assert agent.status == "finished" and agent.turns == 2 and agent.seconds == 3.5
    assert not agent.running
    assert (
        state.turns[-1].notes[-1].text == "agent_1 finished after 2 turns: notes.md, nothing else"
    )
    assert state.turns[-1].notes[-1].kind == "agent"


def test_an_agents_row_before_its_start_event_is_kept() -> None:
    """A client that joins mid-run may see a row before the start. Neither is dropped."""
    state = reduce(AppState(), RunAccepted("run-1", "thread-1")).state
    state = feed(
        state,
        event(1, "run.created", {"message": "go", "mode": "normal", "approval_policy": "ask"}),
        event(
            2,
            "run.progress",
            {
                "update_id": "c1",
                "text": "read",
                "status": "active",
                "tool": "read_file",
                "agent_id": "agent_9",
            },
        ),
        event(3, "agent.failed", {"agent_id": "agent_9", "error": "RuntimeError: boom"}),
    )
    (agent,) = state.turns[-1].agents
    assert agent.status == "failed" and [r.update_id for r in agent.progress] == ["c1"]
    assert state.turns[-1].notes[-1].text == "agent_9 failed: RuntimeError: boom"


def _connected(workspace_id: str = "ws-1", *, reset: bool = False) -> Connected:
    return Connected(
        profile="local",
        endpoint="http://127.0.0.1:8420",
        protocol_version="1.6",
        workspace_id=workspace_id,
        workspace_name=workspace_id,
        workspace_path=f"/{workspace_id}",
        reset_conversation=reset,
    )


def test_a_message_in_flight_holds_the_conversation_and_a_reset_orphans_it() -> None:
    """A workspace switch used to go through while the first message was still being sent,
    and the run it started was then spliced into the new workspace's conversation."""
    sent = reduce(reduce(AppState(), _connected()).state, ComposerSubmitted("hello"))
    assert sent.effects == (StartRun("hello"),) and sent.state.submitting

    held = reduce(sent.state, CommandInvoked("workspace", "/other"))
    assert held.effects == ()
    assert held.state.notices[-1].message.startswith("Wait for the message to be sent")
    assert reduce(sent.state, CommandInvoked("new", "")).effects == ()
    assert reduce(sent.state, CommandInvoked("threads", "")).effects == ()
    # A second message while the first is on its way is held too, and its draft kept.
    again = reduce(sent.state, ComposerSubmitted("and this"))
    assert again.effects == () and again.state.composer_draft == sent.state.composer_draft

    # Once the run is going, it is not in flight: the workspace is held by the run instead.
    accepted = reduce(sent.state, RunAccepted("run-1", "thread-1", 5.0)).state
    assert reduce(accepted, CommandInvoked("workspace", "/other")).state.notices[
        -1
    ].message == "Finish or /cancel the active run before switching workspaces."
    assert reduce(reduce(AppState(), _connected()).state, CommandInvoked("workspace", "/o")).effects == (SwitchWorkspace("/o"),)

    # The switch that went through anyway -- a reconnect, say -- orphans the request.
    switched = reduce(sent.state, _connected("ws-2", reset=True)).state
    assert not switched.submitting and switched.orphaned_submission
    late = reduce(switched, RunAccepted("run-1", "thread-A", 10.0)).state
    assert late.active_run_id is None and late.turns == () and late.thread_id is None
    assert not late.orphaned_submission
    assert "was left" in late.notices[-1].message
    # Its stream, followed by the host regardless, builds no turn with no run id.
    streamed = feed(late, event(1, "run.created", {"message": "hello"}))
    assert streamed.turns == () and streamed.run_status is RunStatus.IDLE
    # A conversation that was not mid-message accepts the next run as ever.
    fresh = reduce(reduce(AppState(), _connected()).state, _connected("ws-2", reset=True)).state
    assert reduce(fresh, RunAccepted("run-2", "thread-B")).state.active_run_id == "run-2"


def test_the_first_add_and_the_first_message_do_not_make_two_threads() -> None:
    """Before the first message, `/add` makes the thread. A message sent meanwhile made
    another, and whichever answered last took the conversation over."""
    fresh = reduce(AppState(), _connected()).state

    adding = reduce(fresh, CommandInvoked("add", "/srv/lib"))
    assert adding.effects == (AddFolder(None, "/srv/lib"),) and adding.state.adding_folder
    held = reduce(adding.state, ComposerSubmitted("first message"))
    assert held.effects == () and held.state.composer_draft == adding.state.composer_draft
    assert held.state.notices[-1].message.startswith("Wait for the folder")
    assert reduce(adding.state, CommandInvoked("add", "/srv/other")).effects == ()

    settled = reduce(adding.state, FolderAdded("thread-9", ("/srv/lib",))).state
    assert not settled.adding_folder and settled.thread_id == "thread-9"
    assert reduce(settled, ComposerSubmitted("first message")).effects == (
        StartRun("first message"),
    )
    # An add that failed frees the composer too.
    assert not reduce(adding.state, OperationFailed("no route")).state.adding_folder

    # The other way round: a message in flight holds the add.
    sent = reduce(fresh, ComposerSubmitted("first message")).state
    assert reduce(sent, CommandInvoked("add", "/srv/lib")).effects == ()
    # A folder added to some other thread does not take this conversation over.
    running = reduce(sent, RunAccepted("run-1", "thread-RUN")).state
    crossed = reduce(running, FolderAdded("thread-FOLDER", ("/srv/lib",))).state
    assert crossed.thread_id == "thread-RUN" and crossed.folders == ()
    assert crossed.notices[-1].level == "warning"
    # Once the conversation has a thread, an add while a run is going is still allowed.
    assert reduce(running, CommandInvoked("add", "/srv/lib")).effects == (
        AddFolder("thread-RUN", "/srv/lib"),
    )


def test_a_notice_made_before_the_clock_lives_a_full_turn_and_is_pruned_after() -> None:
    """A notice was stamped with the clock as it stood, zero before the first tick, so the
    first real tick found it long expired; and an expired notice was never dropped, so the
    tick that watched for it never stopped."""
    failed = reduce(AppState(), ConnectFailed("cannot connect")).state
    assert failed.notices[-1].shown_at == 0 and failed.live_notices == failed.notices

    first = reduce(failed, ClockTicked(12345.0)).state
    assert first.notices[-1].shown_at == 12345.0 and first.live_notices == first.notices
    soon = reduce(first, ClockTicked(12345.0 + NOTICE_SECONDS["error"] - 0.5)).state
    assert soon.notices == first.notices
    gone = reduce(soon, ClockTicked(12345.0 + NOTICE_SECONDS["error"])).state
    assert gone.notices == () and gone.clock == 12345.0 + NOTICE_SECONDS["error"]

    # Each level has its own time, and a state that has a clock stamps with it.
    told = reduce(replace(gone, clock=100.0), CommandInvoked("status", "")).state
    assert told.notices[-1].shown_at == 100.0
    assert reduce(told, ClockTicked(100.0 + NOTICE_SECONDS["info"])).state.notices == ()
    assert replace(told, clock=101.0).live_notices == told.notices


def test_while_a_question_is_pending_everything_but_cancel_is_the_answer() -> None:
    """`/cancel` typed as an answer used to cancel the run, and `/new` to start over."""
    asked = feed(
        _running(),
        event(2, "question.requested", {"question_id": "q1", "prompt": "Which command?"}),
    )
    for text in ("/new", "/workspace /x", "/threads", "/help", "/etc/passwd"):
        answered = reduce(asked, ComposerSubmitted(text))
        assert answered.effects == (SendRunCommand("run-1", Answer("q1", text)),), text
        assert answered.state.notices == ()
    cancelled = reduce(asked, ComposerSubmitted("/cancel"))
    assert cancelled.effects == (SendRunCommand("run-1", Cancel()),)


def test_a_question_is_answered_once_until_the_backend_resolves_it() -> None:
    asked = feed(
        _running(),
        event(2, "question.requested", {"question_id": "q1", "options": ["a", "b"]}),
    )
    sent = reduce(asked, ComposerSubmitted("1"))
    assert sent.effects and sent.state.interaction is not None
    assert sent.state.interaction.sending
    assert reduce(sent.state, ComposerSubmitted("2")).effects == ()
    # A send that failed asks again.
    retry = reduce(sent.state, OperationFailed("no route")).state
    assert retry.interaction is not None and not retry.interaction.sending
    assert reduce(retry, ComposerSubmitted("2")).effects == (
        SendRunCommand("run-1", Answer("q1", "b")),
    )
    resolved = feed(sent.state, event(3, "question.resolved", {"question_id": "q1"}))
    assert resolved.interaction is None


def test_an_agent_with_a_blank_task_or_answer_is_still_told() -> None:
    """`_one_line` took the first line of the text; blank text has none, and it raised."""
    started = feed(_running(), event(2, "agent.started", {"agent_id": "a1", "task": " \n "}))
    assert started.turns[-1].notes[-1].text == "a1 started: (no task)"
    assert feed(_running(), event(2, "agent.started", {"agent_id": "a1"})).turns[-1].notes
    finished = feed(
        started, event(3, "agent.finished", {"agent_id": "a1", "answer": "\n", "turns": 2})
    )
    assert finished.turns[-1].notes[-1].text == "a1 finished after 2 turns"
    assert (
        feed(started, event(3, "agent.finished", {"agent_id": "a1", "answer": " ok \nmore"}))
        .turns[-1]
        .notes[-1]
        .text.endswith(": ok")
    )


def test_a_wire_array_may_be_a_tuple() -> None:
    """`JsonValue` types an array as a `Sequence`; the reducer asked for a `list` and dropped
    a tuple-backed plan, option list, argv and decision list on the floor."""
    state = feed(
        _running(),
        event(2, "plan.progress", {"plan": ({"step": "read", "status": "done"},)}),
        event(
            3,
            "approval.requested",
            {
                "approval_id": "a1",
                "allowed_decisions": ("approve", "reject"),
                "arguments": {"argv": ("ls", "-la")},
            },
        ),
    )
    assert [step.step for step in state.turns[-1].plan] == ["read"]
    assert state.interaction is not None
    assert state.interaction.allowed_decisions == ("approve", "reject")
    assert state.interaction.command == "ls -la"
    asked = feed(
        state,
        event(4, "approval.resolved", {"decision": "approve"}),
        event(5, "question.requested", {"question_id": "q1", "options": ("a", "b")}),
    )
    assert asked.interaction is not None and asked.interaction.options == ("a", "b")
    # A string is a sequence too, and is not an array.
    plain = feed(_running(), event(2, "question.requested", {"question_id": "q", "options": "ab"}))
    assert plain.interaction is not None and plain.interaction.options == ()


def test_a_digit_int_does_not_read_is_an_answer_not_a_pick() -> None:
    asked = feed(
        _running(),
        event(2, "question.requested", {"question_id": "q1", "options": ["a", "b", "c"]}),
    )
    for text in ("²", "①", "1.0"):
        assert reduce(asked, ComposerSubmitted(text)).effects == (
            SendRunCommand("run-1", Answer("q1", text)),
        ), text
    assert reduce(asked, ComposerSubmitted("２")).effects == (
        SendRunCommand("run-1", Answer("q1", "b")),
    )


def test_new_starts_from_the_same_slate_a_loaded_thread_does() -> None:
    """`/new` cleared the transcript and kept the last run's status, cursor, clock and
    notices, so the new conversation opened saying "failed" over an empty page."""
    ended = feed(
        reduce(AppState(), RunAccepted("run-1", "thread-1", 100.0)).state,
        event(1, "run.created", {"message": "hi"}),
        event(2, "run.failed", {"summary": "boom"}),
    )
    told = reduce(replace(ended, clock=150.0), CommandInvoked("status", "")).state
    assert told.run_status is RunStatus.FAILED and told.cursor == 2 and told.notices

    fresh = reduce(told, CommandInvoked("new", "")).state
    assert fresh.run_status is RunStatus.IDLE
    assert (fresh.cursor, fresh.run_started_at, fresh.interaction) == (0, 0.0, None)
    assert fresh.notices == () and fresh.turns == () and fresh.thread_id is None
    assert fresh.clock == 150.0


def test_leaving_a_paused_run_says_what_would_actually_free_it() -> None:
    """Pausing keeps the run active, so "pause or finish" asked for something that could
    not free the conversation, and there is no detaching from a run either."""
    paused = feed(_running(), event(2, "run.paused", {}))
    assert paused.active_run_id == "run-1"
    for name, doing in (
        ("threads", "switching conversations"),
        ("new", "starting a new conversation"),
    ):
        held = reduce(paused, CommandInvoked(name, ""))
        assert held.effects == ()
        assert held.state.notices[-1].message == f"Finish or /cancel the active run before {doing}."
    workspace = reduce(paused, CommandInvoked("workspace", "/x")).state
    assert workspace.notices[-1].message.startswith("Finish or /cancel")


def test_a_decision_in_either_vocabulary_is_said_as_prose() -> None:
    """The keys send `approve`, `reject` and `approve_bash_always`; the label table knew
    `allow` and `deny`, so the notice said `approve_bash_always: run: pytest`."""
    asked = feed(
        _running(),
        event(
            2,
            "approval.requested",
            {"approval_id": "a1", "title": "run: pytest", "allowed_decisions": ["approve"]},
        ),
    )
    assert asked.interaction is not None and asked.interaction.risk == ""
    for decision, said in (
        ("approve", "Approved"),
        ("allow", "Approved"),
        ("approve_bash_always", "Approved, and always from now on"),
        ("allow_always", "Approved, and always from now on"),
        ("reject", "Rejected"),
        ("deny", "Rejected"),
        ("approve_reads_always", "Approve reads always"),
        ("", "Decided"),
    ):
        sent = reduce(asked, ApprovalDecided("approve")).state
        decided = feed(sent, event(3, "approval.resolved", {"decision": decision}))
        assert decided.notices[-1].message == f"{said}: run: pytest", decision
    risky = feed(
        _running(), event(2, "approval.requested", {"approval_id": "a2", "risk": "high"})
    )
    assert risky.interaction is not None and risky.interaction.risk == "high"
