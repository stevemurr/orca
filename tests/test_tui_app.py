"""Headless interaction tests for the persistent Textual shell."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import override

from textual.widgets import ContentSwitcher, Static

from orca.app.actions import EventReceived, RunAccepted
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
from orca.tui.screens import HelpScreen, ThreadPickerScreen, nested_threads
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

        # Focus is not on the input, and 2 is not offered.
        app.set_focus(None)
        await pilot.press("2")
        await pilot.pause()
        assert backend.commands == []
        await pilot.press("1")
        await pilot.pause()
        assert backend.commands == [("run-1", ResolveApproval("approval-1", "approve"))]
