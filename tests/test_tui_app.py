"""Headless interaction tests for the persistent Textual shell."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import override
from unittest.mock import patch

import pytest
from textual.widgets import ContentSwitcher, Static

from orca.app.actions import CommandInvoked, EventReceived, RunAccepted
from orca.app.model import AppState, Choice, TaskEvent, ThreadReplay, ViewId
from orca.backend import (
    BackendError,
    Command,
    CommandOutcome,
    ResolveApproval,
    RunInfo,
    RunRequest,
    SessionInfo,
    ThreadFolders,
    ThreadHistoryInfo,
    ThreadSummary,
)
from orca.json_types import JsonObject
from orca.tui.app import OrcaApp
from orca.tui.screens import (
    HelpScreen,
    ThreadPickerScreen,
    _display_timestamp,  # pyright: ignore[reportPrivateUsage]
    nested_threads,
)
from orca.tui.widgets import Composer


def event(sequence: int, kind: str, payload: JsonObject) -> TaskEvent:
    return TaskEvent(sequence, f"evt-{sequence}", kind, "user", payload)


class FakeBackend:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.commands: list[tuple[str, Command]] = []
        self.streams: list[tuple[str, int, bool]] = []
        self.widened: list[tuple[str | None, str]] = []
        self.closed: bool = False

    async def connect(self) -> SessionInfo:
        return SessionInfo(
            profile="local",
            endpoint="http://127.0.0.1:8420",
            protocol_version="1.6",
            workspace_id="ws-1",
            workspace_name="orca",
            workspace_path="~/Code/orca",
            modes=(Choice("normal"), Choice("plan")),
            policies=(Choice("ask"), Choice("edits"), Choice("full-access")),
            skills=(Choice("deploy", "Ship a release."),),
        )

    async def start_run(self, request: RunRequest) -> RunInfo:
        self.started.append(request.message)
        return RunInfo("run-1", "thread-1")

    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncGenerator[TaskEvent, None]:
        assert run_id == "run-1"
        assert after_seq == 0
        self.streams.append((run_id, after_seq, developer))
        items = [
            event(1, "run.created", {"message": self.started[-1] if self.started else "Attached"}),
            event(
                2,
                "run.progress",
                {"update_id": "shell", "status": "active", "text": "Building the shell."},
            ),
        ]
        if developer:
            items.append(
                TaskEvent(
                    3,
                    "dev-3",
                    "control.transition",
                    "developer",
                    {},
                )
            )
        items.append(
            event(4 if developer else 3, "run.completed", {"summary": "The shell is ready."})
        )
        for item in items:
            yield item

    async def send_command(self, run_id: str, command: Command) -> CommandOutcome:
        self.commands.append((run_id, command))
        return CommandOutcome("accepted")

    async def switch_workspace(self, selector: str) -> SessionInfo:
        raise AssertionError(f"unexpected workspace switch: {selector}")

    async def add_folder(self, thread_id: str | None, path: str) -> ThreadFolders:
        self.widened.append((thread_id, path))
        return ThreadFolders(thread_id or "thread-made", ("/Users/murr/Code/orca", path))

    async def recent_threads(self) -> tuple[ThreadSummary, ...]:
        return (
            ThreadSummary(
                thread_id="thread-recent",
                title="Polish the terminal",
                latest_run_status="completed",
                updated_at="2026-08-28T18:00:00Z",
            ),
        )

    async def load_thread(self, thread_id: str) -> ThreadHistoryInfo:
        assert thread_id == "thread-recent"
        return ThreadHistoryInfo(
            thread_id,
            "Polish the terminal",
            (
                ThreadReplay(
                    "run-recent",
                    "completed",
                    (
                        event(1, "run.created", {"message": "Polish the terminal"}),
                        event(2, "run.completed", {"summary": "Polished it."}),
                    ),
                ),
            ),
        )

    async def close(self) -> None:
        self.closed = True


class RetryApprovalBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once: bool = True

    @override
    async def send_command(self, run_id: str, command: Command) -> CommandOutcome:
        if self.fail_once:
            self.fail_once = False
            raise BackendError("The server is reconnecting.")
        return await super().send_command(run_id, command)


async def test_view_command_swaps_only_the_center_surface() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        assert app.model.view is ViewId.CONVERSATION
        assert app.model.connected

        composer = app.query_one(Composer)
        composer.load_text("/review")
        composer.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert app.model.view is ViewId.REVIEW
        assert app.query_one("#view-host", ContentSwitcher).current == "review"

        await pilot.press("escape")
        await pilot.pause()

        assert app.model.view is ViewId.CONVERSATION
        assert app.query_one(Composer) is composer


async def test_submit_follows_the_run_without_view_owned_io() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("Build it")
        composer.focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert backend.started == ["Build it"]
        assert app.model.turns[-1].request == "Build it"
        assert [item.text for item in app.model.turns[-1].progress] == ["Building the shell."]
        assert app.model.turns[-1].answer == "The shell is ready."


async def test_approval_shortcuts_dispatch_typed_command() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        # Establish the active run without coupling this interaction test to submission.
        app.apply_model_action(RunAccepted("run-1", "thread-1"))
        app.apply_model_action(
            EventReceived(
                event(
                    1,
                    "approval.requested",
                    {
                        "approval_id": "approval-1",
                        "title": "Run the tests?",
                        "allowed_decisions": ["approve", "reject"],
                    },
                )
            )
        )
        await pilot.press("1")
        await pilot.pause()

        assert backend.commands == [("run-1", ResolveApproval("approval-1", "approve"))]


async def test_failed_approval_command_can_be_answered_again() -> None:
    backend = RetryApprovalBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.apply_model_action(RunAccepted("run-1", "thread-1"))
        app.apply_model_action(
            EventReceived(
                event(
                    1,
                    "approval.requested",
                    {
                        "approval_id": "approval-1",
                        "title": "Run the tests?",
                        "allowed_decisions": ["approve", "reject"],
                    },
                )
            )
        )

        await pilot.press("1")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()

        assert backend.commands == [("run-1", ResolveApproval("approval-1", "approve"))]


async def test_a_view_switch_does_not_steal_focus_from_the_composer() -> None:
    """Kept from the work map's own focus test: the composer is never taken away mid-sentence."""

    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.invoke_command("review")
        await pilot.pause()

        composer = app.query_one(Composer)
        composer.focus()
        await pilot.press("h", "i")
        await pilot.pause()

    assert composer.text == "hi"
    assert app.model.composer_draft == "hi"


async def test_help_overlay_owns_escape_before_the_application_shell() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.invoke_command("help")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)

        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, HelpScreen)
        assert app.model.view is ViewId.CONVERSATION


async def test_help_overlay_scrolls_on_a_short_terminal() -> None:
    app = OrcaApp(FakeBackend())

    async with app.run_test(size=(40, 20)) as pilot:
        await pilot.pause()
        app.invoke_command("help")
        await pilot.pause()

        card = app.screen.query_one("#help-card")
        assert card.max_scroll_y > 0
        card.scroll_end(animate=False)
        await pilot.pause()

        assert card.scroll_y == card.max_scroll_y


async def test_an_approval_is_asked_in_the_transcript_and_said_once_when_decided() -> None:

    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.apply_model_action(RunAccepted("run-1", "thread-1"))
        app.apply_model_action(
            EventReceived(
                event(
                    1,
                    "approval.requested",
                    {
                        "approval_id": "approval-1",
                        "title": "Run the tests?",
                        "arguments": {"argv": ["/bin/sh", "-c", "pytest -q"]},
                        "allowed_decisions": ["approve", "approve_bash_always", "reject"],
                    },
                )
            )
        )
        await pilot.pause()

        assert len(app.screen_stack) == 1  # no modal
        assert app.model.interaction is not None and app.model.interaction.kind == "approval"
        assert app.query_one(Composer).approval_keys["2"] == "approve_bash_always"

        await pilot.press("escape")
        await pilot.pause()
        assert backend.commands == [("run-1", ResolveApproval("approval-1", "reject"))]
        assert app.model.interaction is not None and app.model.interaction.sending

        app.apply_model_action(
            EventReceived(
                event(2, "approval.resolved", {"approval_id": "approval-1", "decision": "deny"})
            )
        )
        await pilot.pause()

        assert app.model.interaction is None
        assert app.model.notices[-1].message == "Rejected: Run the tests?"
        assert app.query_one(Composer).approval_keys == {}


async def test_inspector_restarts_follow_with_developer_visibility() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.apply_model_action(RunAccepted("run-1", "thread-1"))
        app.invoke_command("inspect")
        await pilot.pause()
        await pilot.pause()

        assert backend.streams[-1] == ("run-1", 0, True)
        assert app.model.developer_events == ("   3  control.transition",)
        assert app.model.view is ViewId.INSPECTOR


async def test_thread_picker_continues_the_selected_conversation() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.invoke_command("threads")
        await pilot.pause()
        assert isinstance(app.screen, ThreadPickerScreen)

        await pilot.press("enter")
        await pilot.pause()

        assert not isinstance(app.screen, ThreadPickerScreen)
        assert app.model.thread_id == "thread-recent"
        assert app.model.turns[-1].answer == "Polished it."
        assert app.model.notices[-1].message == "Continuing Polish the terminal"


async def test_explicit_thread_replays_on_boot() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend, initial=AppState(thread_id="thread-recent"))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()

        assert app.model.thread_id == "thread-recent"
        assert app.model.turns[-1].request == "Polish the terminal"
        assert app.model.turns[-1].answer == "Polished it."


async def test_add_widens_the_conversation_and_names_the_folder_in_the_header() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.load_text("/add /srv/lib")
        composer.focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert backend.widened == [(None, "/srv/lib")]
        assert app.model.thread_id == "thread-made"
        assert app.model.folders == ("/Users/murr/Code/orca", "/srv/lib")
        assert app.model.notices[-1].message.startswith("folder added:")


def test_the_picker_nests_a_delegated_thread_under_its_parent() -> None:
    rows = (
        ThreadSummary("child-late", parent="parent"),
        ThreadSummary("other"),
        ThreadSummary("parent"),
        ThreadSummary("orphan", parent="gone"),
    )

    assert [row.thread_id for row in nested_threads(rows)] == [
        "other",
        "parent",
        "child-late",
        "orphan",
    ]


def test_the_picker_nests_a_delegation_chain_to_any_depth() -> None:
    rows = (
        ThreadSummary("grandchild", parent="child"),
        ThreadSummary("child", parent="parent"),
        ThreadSummary("sibling", parent="parent"),
        ThreadSummary("parent"),
    )

    assert [row.thread_id for row in nested_threads(rows)] == [
        "parent",
        "child",
        "grandchild",
        "sibling",
    ]


async def test_typing_a_slash_opens_a_menu_that_enter_runs_and_tab_completes() -> None:
    app = OrcaApp(FakeBackend())

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.press("/", "r", "e")
        await pilot.pause()

        menu = app.query_one("#command-menu", Static)
        assert menu.has_class("visible")

        # Down once: review, resume -> resume. Tab takes it into the composer.
        await pilot.press("down", "tab")
        await pilot.pause()
        assert composer.text == "/resume"

        composer.replace_text("/rev")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app.model.view is ViewId.REVIEW
        assert composer.text == ""
        assert not menu.has_class("visible")

        # A command that takes an argument is put in the composer to be finished.
        app.query_one(Composer).focus()
        await pilot.press("escape")
        await pilot.pause()
        composer.replace_text("/mo")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == "/mode "


async def test_naming_a_command_offers_the_values_the_backend_accepts() -> None:
    app = OrcaApp(FakeBackend())

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.replace_text("/perm")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        # The command is in the composer to be finished, and the menu has moved on to
        # what a policy can be. Down once: ask, edits -> edits. Enter sets it.
        assert composer.text == "/permissions "
        menu = app.query_one("#command-menu", Static)
        assert menu.has_class("visible")
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.model.policy == "edits"
        assert composer.text == ""
        assert not menu.has_class("visible")

        # A value the backend did not offer is refused, and the notice says what it offers.
        composer.replace_text("/permissions yolo")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.model.policy == "edits"
        assert "yolo" in app.model.notices[-1].message
        assert "full-access" in app.model.notices[-1].message


async def test_a_turn_that_did_not_change_is_not_rendered_again() -> None:
    """One widget per turn, and a settled turn keeps its lines: a tick of the clock renders
    the turn the run is going in and nothing else. The whole transcript used to be one
    widget, rendered on every tick, and typing waited behind it."""
    from orca.app.model import Narration, ProgressItem, RunStatus, TurnState
    from orca.tui.render.conversation import render_live_turn as real_live
    from orca.tui.render.conversation import render_turn as real
    from orca.tui.views import conversation as view_module
    from orca.tui.views.conversation import ConversationView

    rendered: list[str] = []

    def counting(state: AppState, turn: TurnState, *, width: int) -> object:
        rendered.append(turn.run_id)
        return real(state, turn, width=width)

    def counting_live(state: AppState, turn: TurnState, *, width: int) -> object:
        rendered.append(turn.run_id)
        return real_live(state, turn, width=width)

    settled = TurnState("run-1", request="first", answer="done", status="completed")
    live = TurnState(
        "run-2",
        request="second",
        progress=(ProgressItem("t", "Reading", "active", kind="read"),),
        timeline=(Narration("Looking."),),
    )
    state = AppState(
        booting=False,
        connected=True,
        turns=(settled, live),
        active_run_id="run-2",
        run_status=RunStatus.RUNNING,
        clock=1.0,
    )
    app = OrcaApp(FakeBackend())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        view = app.query_one(ConversationView)
        with (
            patch.object(view_module, "render_turn", counting),
            patch.object(view_module, "render_live_turn", counting_live),
        ):
            view.update_state(state)
            await pilot.pause()
            assert rendered == ["run-1", "run-2"]

            view.update_state(replace(state, clock=2.0))
            await pilot.pause()
            assert rendered == ["run-1", "run-2", "run-2"]

            # Folding or unfolding the tool rows is a change to every turn.
            view.update_state(replace(state, clock=3.0, tools_expanded=True))
            await pilot.pause()
            assert rendered[-2:] == ["run-1", "run-2"]


async def test_the_conversation_follows_new_output_unless_the_person_scrolled_up() -> None:
    from orca.app.model import RunStatus, TurnState
    from orca.tui.views.conversation import ConversationView

    def turns(count: int) -> tuple[TurnState, ...]:
        return tuple(
            TurnState(f"run-{n}", request=f"ask {n}", answer="line\n" * 6, status="completed")
            for n in range(count)
        )

    app = OrcaApp(FakeBackend())
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        view = app.query_one(ConversationView)
        state = AppState(booting=False, connected=True, run_status=RunStatus.COMPLETED)

        view.update_state(replace(state, turns=turns(4)))
        await pilot.pause()
        await pilot.pause()
        assert view.max_scroll_y > 0
        assert view.scroll_y == view.max_scroll_y

        # Reading back up the transcript, new output does not pull the view down.
        view.scroll_to(y=0, animate=False)
        await pilot.pause()
        view.update_state(replace(state, turns=turns(6)))
        await pilot.pause()
        await pilot.pause()
        assert view.scroll_y == 0

        # Back at the end, the view follows again.
        view.scroll_end(animate=False)
        await pilot.pause()
        view.update_state(replace(state, turns=turns(8)))
        await pilot.pause()
        await pilot.pause()
        assert view.scroll_y == view.max_scroll_y


async def test_the_composer_grows_with_a_draft_and_shrinks_back_after_a_send() -> None:
    app = OrcaApp(FakeBackend())

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.press("o", "n", "e", "shift+enter", "t", "w", "o", "shift+enter", "3")
        await pilot.pause()
        assert composer.text == "one\ntwo\n3"
        assert composer.size.height == 3

        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert composer.text == ""
        assert composer.size.height == 1


async def test_the_composer_keeps_the_focus_after_a_send() -> None:
    """It is disabled while the send is in flight, and a disabled widget loses focus;
    the shell gives it back, so the next message can be typed at once."""
    app = OrcaApp(FakeBackend())

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.replace_text("first")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()

        assert app.focused is composer
        assert not composer.disabled


async def test_up_brings_back_earlier_messages_and_down_returns_to_the_draft() -> None:
    app = OrcaApp(FakeBackend())

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        for message in ("first", "second"):
            composer.replace_text(message)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
        assert composer.text == ""

        composer.replace_text("a draft")
        await pilot.pause()
        await pilot.press("up")
        assert composer.text == "second"
        await pilot.press("up")
        assert composer.text == "first"
        # Nothing earlier: Up stays put.
        await pilot.press("up")
        assert composer.text == "first"
        await pilot.press("down")
        assert composer.text == "second"
        # Past the newest, the draft that was being typed comes back.
        await pilot.press("down")
        assert composer.text == "a draft"

        # Inside a draft of several lines, Up moves the cursor until the first line.
        composer.replace_text("one\ntwo")
        await pilot.pause()
        await pilot.press("up")
        assert composer.text == "one\ntwo"
        await pilot.press("up")
        assert composer.text == "second"


async def test_a_skill_from_the_menu_is_sent_as_a_message_for_the_backend_to_read() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.replace_text("/dep")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert composer.text == "/deploy "

        await pilot.press("s", "t", "a", "g", "i", "n", "g", "enter")
        await pilot.pause()
        await pilot.pause()

    assert backend.started == ["/deploy staging"]


async def test_resume_opens_the_thread_picker_once_connected() -> None:
    app = OrcaApp(FakeBackend(), resume=True)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert isinstance(app.screen, ThreadPickerScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ThreadPickerScreen)


async def test_a_burst_of_events_is_rendered_once() -> None:
    """What the backend sends is rendered when the loop is idle, however many events
    arrived; what the person does is rendered as it happens."""
    app = OrcaApp(FakeBackend())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        renders: list[int] = []

        def fake(**_: object) -> None:
            renders.append(1)

        with patch.object(app, "_render_model", side_effect=fake):
            for sequence in range(1, 6):
                app.apply_model_action(
                    EventReceived(
                        event(
                            sequence,
                            "run.progress",
                            {"update_id": f"s{sequence}", "text": "step", "status": "active"},
                        )
                    )
                )
            assert renders == []
            await pilot.pause()
            assert renders == [1]

            app.apply_model_action(CommandInvoked("status"))
            assert renders == [1, 1]


async def test_the_shell_is_redrawn_only_when_what_it_shows_changed() -> None:
    """A delta changes the transcript and nothing around it; a person's command is always
    drawn in full."""
    from orca.tui import app as app_module
    from orca.tui.render import render_header as real

    app = OrcaApp(FakeBackend())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.apply_model_action(RunAccepted("run-1", "thread-1", 1.0))
        app.apply_model_action(
            EventReceived(event(1, "run.created", {"message": "go"})),
        )
        await pilot.pause()
        headers: list[int] = []

        def counting(state: AppState, *, width: int) -> object:
            headers.append(1)
            return real(state, width=width)

        with patch.object(app_module, "render_header", counting):
            for sequence in range(2, 5):
                app.apply_model_action(
                    EventReceived(
                        event(
                            sequence,
                            "answer.delta",
                            {"effect_id": "e", "model_call_id": "m", "text": "word "},
                        )
                    )
                )
                await pilot.pause()
            assert headers == []

            app.apply_model_action(CommandInvoked("status"))
            assert headers == [1]


async def test_a_render_between_a_keystroke_and_its_notice_keeps_the_text() -> None:
    """The clock renders many times a second while a run goes. A keystroke the widget has
    and the model has not yet heard about used to be overwritten by the model's stale
    draft on the next render, with the cursor sent to the start."""
    app = OrcaApp(FakeBackend())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.insert("abc")  # the widget has it; its Changed notice is still queued
        app._render_model()  # pyright: ignore[reportPrivateUsage]

        assert composer.text == "abc"
        assert composer.cursor_location == (0, 3)

        # And a submit that clears the draft still clears the widget.
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        assert composer.text == ""


async def test_an_approval_is_answered_from_anywhere_and_digits_still_type_otherwise() -> None:
    backend = FakeBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)

        # No approval waiting: a 1 is a character.
        composer.focus()
        await pilot.press("1")
        await pilot.pause()
        assert composer.text == "1"
        composer.replace_text("")

        app.apply_model_action(RunAccepted("run-1", "thread-1"))
        app.apply_model_action(
            EventReceived(
                event(
                    1,
                    "approval.requested",
                    {
                        "approval_id": "approval-1",
                        "title": "Run the tests?",
                        "allowed_decisions": ["approve", "reject"],
                    },
                )
            )
        )
        await pilot.pause()

        # Focus is not on the input, and there is no third decision for 3 to send.
        app.set_focus(None)
        await pilot.press("3")
        await pilot.pause()
        assert backend.commands == []
        await pilot.press("1")
        await pilot.pause()
        assert backend.commands == [("run-1", ResolveApproval("approval-1", "approve"))]


class DownBackend(FakeBackend):
    """A backend whose connection fails in the backend's own words."""

    @override
    async def connect(self) -> SessionInfo:
        raise BackendError("The server is not running.")


class BrokenConnectBackend(FakeBackend):
    """A backend whose connection dies of a fault orca did not foresee."""

    @override
    async def connect(self) -> SessionInfo:
        raise RuntimeError("socket blew up")


class BrokenStartBackend(FakeBackend):
    @override
    async def start_run(self, request: RunRequest) -> RunInfo:
        raise ValueError("bad request shape")


class ForeignVocabularyBackend(FakeBackend):
    """A backend whose decisions are `allow` and `deny`, not `approve` and `reject`."""


def _approval(sequence: int, *decisions: str) -> EventReceived:
    return EventReceived(
        event(
            sequence,
            "approval.requested",
            {
                "approval_id": "approval-1",
                "title": "Run the tests?",
                "allowed_decisions": list(decisions),
            },
        )
    )


async def test_a_notice_made_before_the_first_tick_is_not_gone_by_the_next() -> None:
    """The clock starts at zero, and a notice made before it was read was stamped with
    zero: on the first tick it was already long expired, so a failed boot flashed its
    error for a frame and left an empty line. The clock is read before anything runs."""
    app = OrcaApp(DownBackend())

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        assert app.model.clock > 0
        assert app.model.notices and app.model.notices[-1].shown_at >= app.model.clock - 1
        await pilot.pause(0.7)
        assert app.model.notices and "not running" in app.model.notices[-1].message
        assert app.query_one("#notice", Static).has_class("visible")
        assert not app.model.booting


async def test_a_worker_that_dies_of_an_unforeseen_error_still_reports_it() -> None:
    """The bodies catch the backend's own errors; anything else used to die in silence
    with `booting` or `submitting` left set and the input disabled for good."""
    app = OrcaApp(BrokenConnectBackend())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        await pilot.pause(0.2)
        assert not app.model.booting
        assert not app.query_one(Composer).disabled
        assert app.model.notices and "RuntimeError: socket blew up" in app.model.notices[-1].message

    app = OrcaApp(BrokenStartBackend())
    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        composer.replace_text("hello")
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause(0.2)
        assert not app.model.submitting
        assert not composer.disabled
        assert app.model.notices and "ValueError: bad request shape" in app.model.notices[-1].message


async def test_the_approval_keys_follow_the_backends_own_vocabulary() -> None:
    """A backend that offers `allow` and `deny` used to get keys that sent `approve` and
    `reject`, which the reducer refused: nothing could answer it. The digits send the
    decisions in the backend's order; y, n, Enter and Esc find the yes and the no."""
    backend = ForeignVocabularyBackend()
    app = OrcaApp(backend)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.apply_model_action(RunAccepted("run-1", "thread-1"))
        app.apply_model_action(_approval(1, "allow", "allow_always", "deny"))
        await pilot.pause()
        assert app.query_one(Composer).approval_keys == {
            "1": "allow",
            "2": "allow_always",
            "3": "deny",
            "y": "allow",
            "enter": "allow",
            "n": "deny",
            "escape": "deny",
        }
        await pilot.press("y")
        await pilot.pause()
        assert backend.commands[-1] == ("run-1", ResolveApproval("approval-1", "allow"))

        app.apply_model_action(EventReceived(event(2, "approval.resolved", {"decision": "allow"})))
        app.apply_model_action(_approval(3, "allow", "deny"))
        await pilot.pause()
        app.set_focus(None)
        await pilot.press("escape")
        await pilot.pause()
        assert backend.commands[-1] == ("run-1", ResolveApproval("approval-1", "deny"))

        # A vocabulary orca has never seen has its digits and nothing else; Escape does
        # not guess at a refusal.
        app.apply_model_action(EventReceived(event(4, "approval.resolved", {"decision": "deny"})))
        app.apply_model_action(_approval(5, "proceed", "abort"))
        await pilot.pause()
        assert app.query_one(Composer).approval_keys == {"1": "proceed", "2": "abort"}
        await pilot.press("escape")
        await pilot.pause()
        assert app.model.interaction is not None and not app.model.interaction.sending
        await pilot.press("2")
        await pilot.pause()
        assert backend.commands[-1] == ("run-1", ResolveApproval("approval-1", "abort"))


async def test_a_question_with_a_dozen_options_shows_the_last_of_them() -> None:
    """The strip was a fixed fourteen rows that did not scroll, so options past the
    ninth were cut off; and a single-cell number column drew the tenth as `…`."""
    app = OrcaApp(FakeBackend())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.apply_model_action(RunAccepted("run-1", "thread-1"))
        app.apply_model_action(EventReceived(event(1, "run.created", {"message": "hi"})))
        options = [f"option {index}" for index in range(1, 13)]
        app.apply_model_action(
            EventReceived(
                event(
                    2,
                    "question.requested",
                    {"question_id": "q1", "prompt": "Pick one", "options": options},
                )
            )
        )
        await pilot.pause()
        pane = app.query_one("#interaction")
        body = app.query_one("#interaction-body", Static)
        assert pane.has_class("visible")
        # Everything fits at this height, so the last option is on screen as drawn.
        assert pane.max_scroll_y == 0
        assert body.region.height >= 12
        drawn = "\n".join(
            "".join(segment.text for segment in body.render_line(row))
            for row in range(body.region.height)
        )
        assert "12  option 12" in drawn
        assert "…" not in drawn

        # Forty options do not fit; the pane scrolls rather than cutting them off.
        app.apply_model_action(
            EventReceived(
                event(
                    3,
                    "question.requested",
                    {
                        "question_id": "q2",
                        "prompt": "Pick one",
                        "options": [f"option {index}" for index in range(1, 41)],
                    },
                )
            )
        )
        await pilot.pause()
        assert pane.max_scroll_y > 0
        pane.scroll_end(animate=False)
        await pilot.pause()
        assert pane.scroll_y == pane.max_scroll_y


@pytest.mark.parametrize(
    "value",
    (
        "not-a-date",
        "9999-12-31T23:59:59+00:00",
        "0001-01-01T00:00:00+00:00",
        "0001-01-01T00:00:00+14:00",
        "9999-12-31T23:59:59-12:00",
    ),
)
def test_a_timestamp_that_cannot_be_shown_is_left_out_rather_than_shown_raw(value: str) -> None:
    """`not-a-date` was displayed as it came, and a date at either end of the calendar
    raised from `astimezone` and took the picker down with it."""
    shown = _display_timestamp(value)
    assert isinstance(shown, str)
    assert shown != value


def test_a_timestamp_that_is_not_a_date_is_left_out() -> None:
    assert _display_timestamp("not-a-date") == ""
    assert _display_timestamp("") == ""
    assert _display_timestamp("2026-09-04T12:00:00+00:00")
