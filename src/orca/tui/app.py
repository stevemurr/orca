"""Persistent Textual host for the state-driven orca client."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import ClassVar, assert_never, cast, override

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.command import Provider
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Static, TextArea

from orca.app.actions import (
    Action,
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
    OperationFailed,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
    ViewportChanged,
)
from orca.app.commands import CommandSpec, spec_for, suggest
from orca.app.model import Activity, AppState, ViewId
from orca.app.update import (
    AddFolder,
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
from orca.backend import BackendError, ResolveApproval, RunRequest, TerminalBackend
from orca.tui.commands import OrcaCommands
from orca.tui.render import (
    render_command_menu,
    render_footer,
    render_header,
    render_interaction,
    render_notice,
    render_plan,
)
from orca.tui.screens import ApprovalScreen, HelpScreen, ThreadPickerScreen
from orca.tui.views import ConversationView, InspectorView, ReviewView
from orca.tui.views.base import RenderedView
from orca.tui.widgets import Composer


class OrcaApp(App[None]):
    """One cursor owner, one store, and multiple quiet terminal views."""

    TITLE: str | None = "orca"
    COMMANDS: ClassVar[set[type[Provider] | Callable[[], type[Provider]]]] = App.COMMANDS | {
        OrcaCommands
    }
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "context_escape", "Back", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+t", "toggle_tools", "Tool calls", show=False),
    ]
    CSS: ClassVar[str] = """
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

    #plan {
        width: 100%;
        height: auto;
        max-height: 9;
        padding: 0 1;
        border-top: solid $surface-lighten-2;
        display: none;
    }

    #plan.visible {
        display: block;
    }

    #notice {
        width: 100%;
        height: 1;
        padding: 0 1;
        display: none;
    }

    #notice.visible {
        display: block;
    }

    #command-menu {
        width: 100%;
        height: auto;
        max-height: 9;
        padding: 0 1;
        border-top: solid $surface-lighten-2;
        display: none;
    }

    #command-menu.visible {
        display: block;
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
        self.backend: TerminalBackend = backend
        self.model: AppState = initial or AppState()
        self._approval_request_id: str = ""
        self._closing: bool = False
        #: Whether the shell's widgets exist yet. `App.is_mounted` is a method that takes a
        #: widget, so the old `self.is_mounted` guard was a bound method and always true.
        self._shell_ready: bool = False
        #: Which row of the `/` menu is highlighted. Widget state, not model state: it is
        #: about where a cursor is, and it resets whenever the draft changes.
        self._menu_index: int = 0

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(id="app-header")
            with ContentSwitcher(initial=ViewId.CONVERSATION.value, id="view-host"):
                yield ConversationView(id=ViewId.CONVERSATION.value, classes="main-view")
                yield ReviewView(id=ViewId.REVIEW.value, classes="main-view")
                yield InspectorView(id=ViewId.INSPECTOR.value, classes="main-view")
            yield Static(id="plan")
            yield Static(id="interaction")
            yield Static(id="notice")
            yield Static(id="command-menu")
            with Horizontal(id="composer-frame"):
                yield Static("›", id="composer-prompt")
                yield Composer()
            yield Static(id="app-footer")

    def on_mount(self) -> None:
        self._shell_ready = True
        self.apply_model_action(ViewportChanged(self.size.width, self.size.height))
        # Fast enough for a shine to move; `_tick` slows itself when nothing needs that.
        _ = self.set_interval(0.08, self._tick)
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

    def _tick(self) -> None:
        # While a run goes, for the spinner and the elapsed time; while a notice shows, so
        # it can go away. Otherwise nothing on screen depends on the clock. A working tool
        # row carries a shine that has to move every frame; the rest reads fine at two a
        # second, and a transcript is not re-rendered faster than it needs to be.
        if not (self.model.working or self.model.notices):
            return
        now = time.monotonic()
        if not self._shining() and now - self.model.clock < 0.5:
            return
        self.apply_model_action(ClockTicked(now))

    def _shining(self) -> bool:
        """Whether a tool row is lit right now, which is what needs the fast clock: a call
        in progress, or the working turn's latest group with nothing after it yet."""
        if not self.model.working or not self.model.turns:
            return False
        turn = self.model.turns[-1]
        if turn.run_id != self.model.active_run_id:
            return False
        if any(item.status.lower() == "active" for item in turn.progress):
            return True
        return bool(turn.timeline) and isinstance(turn.timeline[-1], Activity)

    def on_resize(self, event: events.Resize) -> None:
        self.apply_model_action(ViewportChanged(event.size.width, event.size.height))

    def apply_model_action(self, action: Action) -> None:
        transition = reduce(self.model, action)
        self.model = transition.state
        if self._shell_ready and not isinstance(action, ComposerChanged):
            self._render_model()
        elif self._shell_ready:
            # Typing re-renders the menu alone; the rest of the shell has not changed.
            self._menu_index = 0
            self._render_menu()
        for effect in transition.effects:
            self._perform(effect)

    def invoke_command(self, name: str, argument: str = "") -> None:
        spec = spec_for(name)
        if spec is not None and spec.argument and not argument:
            composer = self.query_one(Composer)
            composer.replace_text(f"/{name} ")
            composer.focus()
            return
        self.apply_model_action(CommandInvoked(name, argument))

    def _suggestions(self) -> tuple[CommandSpec, ...]:
        return suggest(self.model.composer_draft, developer=self.model.developer)

    def _render_menu(self) -> None:
        menu = self.query_one("#command-menu", Static)
        commands = self._suggestions()
        if not commands:
            menu.set_class(False, "visible")
            menu.update("")
            return
        self._menu_index %= len(commands)
        menu.set_class(True, "visible")
        menu.update(render_command_menu(commands, self._menu_index))

    @on(Composer.MenuMoved)
    def menu_moved(self, message: Composer.MenuMoved) -> None:
        commands = self._suggestions()
        if commands:
            self._menu_index = (self._menu_index + message.delta) % len(commands)
            self._render_menu()

    @on(Composer.MenuAccepted)
    def menu_accepted(self) -> None:
        commands = self._suggestions()
        if commands:
            chosen = commands[self._menu_index % len(commands)]
            self.query_one(Composer).replace_text(
                f"/{chosen.name}" + (" " if chosen.argument else "")
            )

    @on(Composer.Submitted)
    def composer_submitted(self, message: Composer.Submitted) -> None:
        commands = self._suggestions()
        if commands:
            # Enter on the menu runs the highlighted command. One that takes an argument
            # is put in the composer instead, by `invoke_command`, for the person to finish.
            chosen = commands[self._menu_index % len(commands)]
            # The model first: a render after the command reloads the draft it holds,
            # and the widget's own change notice arrives later than that.
            self.apply_model_action(ComposerChanged(""))
            self.query_one(Composer).replace_text("")
            self.invoke_command(chosen.name)
            return
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

    def action_toggle_tools(self) -> None:
        self.apply_model_action(CommandInvoked("tools"))

    @override
    async def action_quit(self) -> None:
        self.apply_model_action(CommandInvoked("quit"))

    def _render_model(self) -> None:
        width = max(1, self.model.viewport_width)
        self.query_one("#app-header", Static).update(render_header(self.model, width=width))
        host = self.query_one("#view-host", ContentSwitcher)
        host.current = self.model.view.value
        self.query_one(f"#{self.model.view.value}", RenderedView).update_state(self.model)

        self._render_menu()
        notice = self.query_one("#notice", Static)
        shown = render_notice(self.model)
        notice.set_class(shown is not None, "visible")
        notice.update(shown or "")

        plan = self.query_one("#plan", Static)
        pinned = render_plan(self.model, width=max(1, width - 2))
        plan.set_class(pinned is not None, "visible")
        plan.update(pinned or "")

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
        match effect:
            case StartRun():
                self.run_worker(
                    self._start_run(effect),
                    name="start-run",
                    group="submission",
                    exclusive=True,
                    exit_on_error=False,
                )
            case SendRunCommand():
                self.run_worker(
                    self._send_command(effect),
                    name=f"command:{effect.command}",
                    group="commands",
                    exit_on_error=False,
                )
            case OpenHelp():
                self.push_screen(HelpScreen(developer=self.model.developer))
            case OpenThreads():
                self.run_worker(
                    self._open_threads(),
                    name="recent-threads",
                    group="recent-threads",
                    exclusive=True,
                    exit_on_error=False,
                )
            case LoadThread():
                self.run_worker(
                    self._load_thread(effect),
                    name=f"thread:{effect.thread_id}",
                    group="thread-load",
                    exclusive=True,
                    exit_on_error=False,
                )
            case FollowRun():
                self.run_worker(
                    self._follow(effect.run_id, after_seq=effect.after_seq),
                    name=f"stream:{effect.run_id}",
                    group="stream",
                    exclusive=True,
                    exit_on_error=False,
                )
            case SwitchWorkspace():
                self.run_worker(
                    self._switch_workspace(effect.selector),
                    name="switch-workspace",
                    group="workspace",
                    exclusive=True,
                    exit_on_error=False,
                )
            case AddFolder():
                self.run_worker(
                    self._add_folder(effect),
                    name="add-folder",
                    group="workspace",
                    exit_on_error=False,
                )
            case ExitApplication():
                self.exit()
            case _:
                assert_never(effect)

    async def _boot(self) -> None:
        try:
            info = await self.backend.connect()
        except BackendError as exc:
            self.apply_model_action(ConnectFailed(str(exc)))
            return
        self.apply_model_action(
            Connected(
                profile=info.profile,
                endpoint=info.endpoint,
                protocol_version=info.protocol_version,
                workspace_id=info.workspace_id,
                workspace_name=info.workspace_name,
                workspace_path=info.workspace_path,
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
            mode=self.model.mode,
            policy=self.model.policy,
        )
        try:
            info = await self.backend.start_run(request)
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(RunAccepted(info.run_id, info.thread_id, time.monotonic()))
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
            outcome = await self.backend.send_command(effect.run_id, effect.command)
        except BackendError as exc:
            if isinstance(effect.command, ResolveApproval) and isinstance(
                self.screen, ApprovalScreen
            ):
                self.screen.allow_retry()
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(CommandCompleted(type(effect.command).__name__.lower(), outcome))

    async def _switch_workspace(self, selector: str) -> None:
        try:
            info = await self.backend.switch_workspace(selector)
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(
            Connected(
                profile=info.profile,
                endpoint=info.endpoint,
                protocol_version=info.protocol_version,
                workspace_id=info.workspace_id,
                workspace_name=info.workspace_name,
                workspace_path=info.workspace_path,
                reset_conversation=True,
            )
        )

    async def _add_folder(self, effect: AddFolder) -> None:
        try:
            widened = await self.backend.add_folder(effect.thread_id, effect.path)
        except BackendError as exc:
            self.apply_model_action(OperationFailed(str(exc)))
            return
        self.apply_model_action(FolderAdded(widened.thread_id, widened.folders))

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
