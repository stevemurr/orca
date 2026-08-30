"""Persistent Textual host for the state-driven orca client."""

from __future__ import annotations

from typing import cast

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Static, TextArea

from orca.app.actions import (
    Back,
    BootCompleted,
    BootFailed,
    CommandCompleted,
    CommandInvoked,
    ComposerChanged,
    ComposerSubmitted,
    EventReceived,
    OperationFailed,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
    ViewportChanged,
)
from orca.app.model import AppState, ViewId
from orca.app.update import (
    Effect,
    ExitApplication,
    FollowRun,
    LoadThread,
    OpenHelp,
    OpenThreads,
    SendRunCommand,
    StartRun,
    SwitchWorkspace,
    reduce,
)
from orca.backend import BackendError, RunRequest, TerminalBackend
from orca.tui.commands import OrcaCommands
from orca.tui.render import (
    render_footer,
    render_header,
    render_interaction,
)
from orca.tui.screens import ApprovalScreen, HelpScreen, ThreadPickerScreen
from orca.tui.views import ConversationView, InspectorView, ReviewView
from orca.tui.views.base import RenderedView
from orca.tui.widgets import Composer


class OrcaApp(App[None]):
    """One cursor owner, one store, and multiple quiet terminal views."""

    TITLE = "orca"
    COMMANDS = App.COMMANDS | {OrcaCommands}
    BINDINGS = (
        Binding("escape", "context_escape", "Back", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
    )
    CSS = """
    Screen {
        background: $background;
        color: $text;
    }

    #shell {
        width: 100%;
        height: 100%;
    }

    #app-header {
        width: 100%;
        height: 2;
        padding: 0 1;
        border-bottom: solid $surface-lighten-2;
    }

    #view-host {
        width: 100%;
        height: 1fr;
    }

    .main-view {
        width: 100%;
        height: 100%;
        padding: 1 1 0 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $surface-lighten-2;
        scrollbar-color-hover: $primary;
        background: $background;
    }

    #interaction {
        width: 100%;
        height: auto;
        max-height: 10;
        padding: 0 1;
        display: none;
    }

    #interaction.visible {
        display: block;
    }

    #composer-frame {
        width: 100%;
        height: auto;
        min-height: 3;
        max-height: 9;
        padding: 0 1;
        border-top: solid $surface-lighten-2;
        background: $background;
    }

    #composer-prompt {
        width: 3;
        height: 100%;
        padding: 0 0;
        color: $primary;
        text-style: bold;
        background: $background;
    }

    Composer {
        width: 100%;
        height: 3;
        min-height: 3;
        max-height: 9;
        border: none;
        padding: 0 0;
        background: $background;
        color: $text;
    }

    Composer:focus {
        border: none;
    }

    #app-footer {
        width: 100%;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $background;
    }

    HelpScreen, ApprovalScreen, ThreadPickerScreen {
        align: center middle;
        background: rgba(0,0,0,0.45);
    }

    .modal-card {
        width: 92%;
        max-width: 88;
        max-height: 88%;
        padding: 1 2;
        border: round $surface-lighten-3;
        background: $surface;
    }

    .approval-card {
        width: 92%;
        max-width: 84;
        height: auto;
    }

    .thread-card {
        width: 92%;
        max-width: 84;
        height: 75%;
    }

    .modal-title {
        height: 2;
        text-style: bold;
    }

    .empty-state {
        height: 1fr;
        color: $text-muted;
    }

    #thread-options {
        height: 1fr;
        border: none;
        background: $surface;
        scrollbar-size-vertical: 1;
    }

    .modal-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(self, backend: TerminalBackend, *, initial: AppState | None = None) -> None:
        super().__init__()
        self.backend = backend
        self.model = initial or AppState()
        self._approval_request_id = ""
        self._closing = False

    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(id="app-header")
            with ContentSwitcher(initial=ViewId.CONVERSATION.value, id="view-host"):
                yield ConversationView(id=ViewId.CONVERSATION.value, classes="main-view")
                yield ReviewView(id=ViewId.REVIEW.value, classes="main-view")
                yield InspectorView(id=ViewId.INSPECTOR.value, classes="main-view")
            yield Static(id="interaction")
            with Horizontal(id="composer-frame"):
                yield Static("›", id="composer-prompt")
                yield Composer()
            yield Static(id="app-footer")

    def on_mount(self) -> None:
        self.apply_model_action(ViewportChanged(self.size.width, self.size.height))
        self._render_model()
        self.run_worker(
            self._boot(),
            name="bootstrap",
            group="bootstrap",
            exclusive=True,
            exit_on_error=False,
        )

    async def on_unmount(self) -> None:
        if self._closing:
            return
        self._closing = True
        await self.backend.close()

    def on_resize(self, event: events.Resize) -> None:
        self.apply_model_action(ViewportChanged(event.size.width, event.size.height))

    def apply_model_action(self, action) -> None:  # type: ignore[no-untyped-def]
        transition = reduce(self.model, action)
        self.model = transition.state
        if self.is_mounted and not isinstance(action, ComposerChanged):
            self._render_model()
        for effect in transition.effects:
            self._perform(effect)

    def invoke_command(self, name: str, argument: str = "") -> None:
        spec_requires_argument = name in {"mode", "permissions", "workspace"}
        if spec_requires_argument and not argument:
            composer = self.query_one(Composer)
            composer.load_text(f"/{name} ")
            composer.focus()
            return
        self.apply_model_action(CommandInvoked(name, argument))

    @on(Composer.Submitted)
    def composer_submitted(self, message: Composer.Submitted) -> None:
        self.apply_model_action(ComposerSubmitted(message.text))

    @on(TextArea.Changed, "#composer")
    def composer_changed(self, message: TextArea.Changed) -> None:
        composer = cast(Composer, message.text_area)
        composer.fit_height()
        if composer.text != self.model.composer_draft:
            self.apply_model_action(ComposerChanged(composer.text))

    def action_context_escape(self) -> None:
        if isinstance(self.screen, (HelpScreen, ApprovalScreen)):
            return
        if self.model.view is not ViewId.CONVERSATION:
            self.apply_model_action(Back())
            self.query_one(Composer).focus()
        elif self.model.active_run_id:
            self.apply_model_action(CommandInvoked("pause"))

    def action_quit(self) -> None:
        self.apply_model_action(CommandInvoked("quit"))

    def _render_model(self) -> None:
        width = max(1, self.model.viewport_width)
        self.query_one("#app-header", Static).update(render_header(self.model, width=width))
        host = self.query_one("#view-host", ContentSwitcher)
        host.current = self.model.view.value
        self.query_one(f"#{self.model.view.value}", RenderedView).update_state(self.model)

        interaction = self.query_one("#interaction", Static)
        inline_interaction = (
            render_interaction(self.model, width=max(1, width - 2))
            if self.model.interaction is not None and self.model.interaction.kind == "question"
            else None
        )
        interaction.set_class(inline_interaction is not None, "visible")
        interaction.update(inline_interaction or "")

        composer = self.query_one(Composer)
        if composer.text != self.model.composer_draft:
            composer.load_text(self.model.composer_draft)
        composer.disabled = self.model.booting or self.model.submitting
        composer.placeholder = (
            "Answer the agent…"
            if self.model.interaction is not None and self.model.interaction.kind == "question"
            else "Steer the active run…"
            if self.model.active_run_id
            else "Ask the agent…"
        )
        self.query_one("#app-footer", Static).update(render_footer(self.model))
        self._sync_approval()

    def _sync_approval(self) -> None:
        interaction = self.model.interaction
        if interaction is not None and interaction.kind == "approval":
            if self._approval_request_id != interaction.request_id:
                self._approval_request_id = interaction.request_id
                self.push_screen(ApprovalScreen(self.model))
            return
        self._approval_request_id = ""
        if isinstance(self.screen, ApprovalScreen):
            self.screen.dismiss()

    def _perform(self, effect: Effect) -> None:
        if isinstance(effect, StartRun):
            self.run_worker(
                self._start_run(effect),
                name="start-run",
                group="submission",
                exclusive=True,
                exit_on_error=False,
            )
        elif isinstance(effect, SendRunCommand):
            self.run_worker(
                self._send_command(effect),
                name=f"command:{effect.command}",
                group="commands",
                exit_on_error=False,
            )
        elif isinstance(effect, OpenHelp):
            self.push_screen(HelpScreen(developer=self.model.developer))
        elif isinstance(effect, OpenThreads):
            self.run_worker(
                self._open_threads(),
                name="recent-threads",
                group="recent-threads",
                exclusive=True,
                exit_on_error=False,
            )
        elif isinstance(effect, LoadThread):
            self.run_worker(
                self._load_thread(effect),
                name=f"thread:{effect.thread_id}",
                group="thread-load",
                exclusive=True,
                exit_on_error=False,
            )
        elif isinstance(effect, FollowRun):
            self.run_worker(
                self._follow(effect.run_id, after_seq=effect.after_seq),
                name=f"stream:{effect.run_id}",
                group="stream",
                exclusive=True,
                exit_on_error=False,
            )
        elif isinstance(effect, SwitchWorkspace):
            self.run_worker(
                self._switch_workspace(effect.selector),
                name="switch-workspace",
                group="workspace",
                exclusive=True,
                exit_on_error=False,
            )
        elif isinstance(effect, ExitApplication):
            self.exit()

    async def _boot(self) -> None:
        try:
            info = await self.backend.boot()
        except BackendError as exc:
            self.apply_model_action(BootFailed(str(exc)))
            return
        self.apply_model_action(
            BootCompleted(
                profile=info.profile,
                endpoint=info.endpoint,
                protocol_version=info.protocol_version,
                workspace_id=info.workspace_id,
                workspace_name=info.workspace_name,
                workspace_path=info.workspace_path,
                cwd_relative=info.cwd_relative,
                capabilities=info.capabilities,
            )
        )
        if self.model.thread_id and not self.model.turns:
            self.apply_model_action(ThreadSelected(self.model.thread_id))
        self.query_one(Composer).focus()

    async def _start_run(self, effect: StartRun) -> None:
        request = RunRequest(
            message=effect.message,
            thread_id=self.model.thread_id,
            workspace_id=self.model.workspace_id,
            cwd_relative=self.model.cwd_relative,
            mode=self.model.mode,
            policy=self.model.policy,
        )
        try:
            info = await self.backend.start_run(request)
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(RunAccepted(info.run_id, info.thread_id))
        self.run_worker(
            self._follow(info.run_id, after_seq=0),
            name=f"stream:{info.run_id}",
            group="stream",
            exclusive=True,
            exit_on_error=False,
        )

    async def _follow(self, run_id: str, *, after_seq: int) -> None:
        try:
            async for event in self.backend.stream(
                run_id,
                after_seq=after_seq,
                developer=self.model.developer,
            ):
                self.apply_model_action(EventReceived(event))
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))

    async def _send_command(self, effect: SendRunCommand) -> None:
        try:
            response = await self.backend.send_command(
                effect.run_id,
                effect.command,
                dict(effect.fields),
            )
        except BackendError as exc:
            if effect.command == "resolve_approval" and isinstance(self.screen, ApprovalScreen):
                self.screen.allow_retry()
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(CommandCompleted(effect.command, response))

    async def _switch_workspace(self, selector: str) -> None:
        try:
            info = await self.backend.switch_workspace(selector)
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(
            BootCompleted(
                profile=info.profile,
                endpoint=info.endpoint,
                protocol_version=info.protocol_version,
                workspace_id=info.workspace_id,
                workspace_name=info.workspace_name,
                workspace_path=info.workspace_path,
                cwd_relative=info.cwd_relative,
                capabilities=info.capabilities,
                reset_conversation=True,
            )
        )

    async def _open_threads(self) -> None:
        try:
            rows = await self.backend.recent_threads()
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.push_screen(ThreadPickerScreen(rows), self._thread_selected)

    def _thread_selected(self, choice: tuple[str, str] | None) -> None:
        if choice is None:
            return
        thread_id, title = choice
        self.apply_model_action(ThreadSelected(thread_id, title))
        self.query_one(Composer).focus()

    async def _load_thread(self, effect: LoadThread) -> None:
        try:
            history = await self.backend.load_thread(effect.thread_id)
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(
            ThreadLoaded(history.thread_id, history.title or effect.title, history.runs)
        )
