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
from textual.theme import Theme
from textual.widgets import ContentSwitcher, Static, TextArea

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
    OperationFailed,
    RunAccepted,
    ThreadLoaded,
    ThreadSelected,
    ViewportChanged,
)
from orca.app.commands import Choices, Suggestion, spec_for, suggest
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
from orca.backend import BackendError, RunRequest, TerminalBackend
from orca.tui.commands import OrcaCommands
from orca.tui.render import (
    render_command_menu,
    render_footer,
    render_header,
    render_interaction,
    render_notice,
    render_plan,
)
from orca.tui.render.theme import (
    ACCENT,
    BACKGROUND,
    ERROR,
    MUTED,
    PANEL,
    SUCCESS,
    SURFACE,
    TEXT,
    WARNING,
)
from orca.tui.screens import HelpScreen, ThreadPickerScreen
from orca.tui.views import AgentsView, ConversationView, InspectorView, ReviewView
from orca.tui.views.base import StateView
from orca.tui.widgets import Composer

#: The renderer's tokens, given to Textual as well, so everything it draws on its own --
#: scrollbars, a list's chosen row, the text cursor -- is in the same palette as the
#: transcript rather than in its stock blue.
ORCA_THEME = Theme(
    name="orca",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    warning=WARNING,
    error=ERROR,
    success=SUCCESS,
    foreground=TEXT,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    dark=True,
)


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
        # Priority, so they win over the composer while an approval waits: a person should
        # not have to click into the input to answer. `check_action` switches them off the
        # rest of the time, so a 1 typed into a message is still a 1.
        Binding("1,y", "decide('approve')", "Approve", show=False, priority=True),
        Binding("2", "decide('approve_bash_always')", "Always", show=False, priority=True),
        Binding("3,n", "decide('reject')", "Reject", show=False, priority=True),
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

    /* A line of air above the wordmark: on the terminal's first row it read as part of
       the window chrome rather than as the app's own title. */
    #app-header {
        width: 100%;
        height: 3;
        padding: 1 1 0 1;
        border-bottom: solid $surface-lighten-1;
    }

    #view-host {
        width: 100%;
        height: 1fr;
    }

    .main-view {
        width: 100%;
        height: 100%;
        /* A line of air below the last row, so the transcript does not sit on the input. */
        padding: 1 1 1 1;
        scrollbar-size-vertical: 1;
        scrollbar-color: $surface-lighten-2;
        scrollbar-color-hover: $primary;
        background: $background;
    }

    #plan {
        width: 100%;
        height: auto;
        max-height: 9;
        padding: 0 2;
        border-top: solid $surface-lighten-1;
        display: none;
    }

    #plan.visible {
        display: block;
    }

    #notice {
        width: 100%;
        height: 1;
        padding: 0 2;
        display: none;
    }

    #notice.visible {
        display: block;
    }

    #command-menu {
        width: 100%;
        height: auto;
        /* Eight rows and the line that says how many more, under the border. */
        max-height: 10;
        padding: 0 2;
        border-top: solid $surface-lighten-1;
        display: none;
    }

    #command-menu.visible {
        display: block;
    }

    #interaction {
        width: 100%;
        height: auto;
        max-height: 14;
        padding: 0 1;
        display: none;
    }

    #interaction.visible {
        display: block;
    }

    /* The input is a box, as an editor's agent draws it: the one bordered thing on the
       screen, so the eye knows where to type without a label saying so. */
    #composer-frame {
        width: 100%;
        height: auto;
        min-height: 3;
        max-height: 9;
        padding: 0 1;
        border: round $surface-lighten-2;
        background: $background;
    }

    #composer-prompt {
        width: 2;
        height: 100%;
        padding: 0 0;
        color: $primary;
        text-style: bold;
        background: $background;
    }

    Composer {
        width: 1fr;
        height: 1;
        min-height: 1;
        max-height: 7;
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
        padding: 0 2;
        color: $text-muted;
        background: $background;
    }

    HelpScreen, ThreadPickerScreen {
        align: center middle;
        background: rgba(0,0,0,0.55);
    }

    .modal-card {
        width: 92%;
        max-width: 88;
        max-height: 88%;
        padding: 1 2;
        border: round $surface-lighten-2;
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
        padding: 0;
        background: $surface;
        scrollbar-size-vertical: 1;
    }

    /* The chosen row is a tint of the accent, not a block of it: the words stay in the
       text colour and the tint says which row, the way a selection does. */
    #thread-options > .option-list--option-highlighted,
    #thread-options:focus > .option-list--option-highlighted {
        background: $primary 18%;
        color: $text;
        text-style: none;
    }

    #thread-options:focus {
        background-tint: $foreground 0%;
    }

    .modal-hint {
        margin-top: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        backend: TerminalBackend,
        *,
        initial: AppState | None = None,
        resume: bool = False,
    ) -> None:
        super().__init__()
        #: Open on the recent conversations, for `orca --resume`: the picker `/threads`
        #: opens, shown as soon as the connection is up, so picking up where one left
        #: off is one flag and one Enter rather than a command typed into a fresh shell.
        self._resume_on_start: bool = resume
        self.register_theme(ORCA_THEME)
        self.theme = ORCA_THEME.name  # pyright: ignore[reportUnannotatedClassAttribute]
        self.backend: TerminalBackend = backend
        self.model: AppState = initial or AppState()
        self._closing: bool = False
        #: Whether the shell's widgets exist yet. `App.is_mounted` is a method that takes a
        #: widget, so the old `self.is_mounted` guard was a bound method and always true.
        self._shell_ready: bool = False
        #: Which row of the `/` menu is highlighted. Widget state, not model state: it is
        #: about where a cursor is, and it resets whenever the draft changes.
        self._menu_index: int = 0
        #: The last draft the composer reported. The composer is reloaded from the model
        #: only when the model changed the draft on its own -- a submit clearing it -- and
        #: never because a keystroke had not reached the model yet: a render between the
        #: two used to put the model's stale draft back and the cursor at the start.
        self._reported_draft: str = ""
        #: Whether a render is waiting for the loop to go idle; see `_render_soon`.
        self._render_queued: bool = False
        #: What the shell around the transcript was last drawn from; see `_chrome_key`.
        self._chrome_shown: tuple[object, ...] | None = None
        #: What the `/` menu was last drawn from: the rows and the highlighted one.
        self._menu_shown: tuple[tuple[str, ...], int] | None = None

    @override
    def compose(self) -> ComposeResult:
        with Vertical(id="shell"):
            yield Static(id="app-header")
            with ContentSwitcher(initial=ViewId.CONVERSATION.value, id="view-host"):
                yield ConversationView(id=ViewId.CONVERSATION.value, classes="main-view")
                yield ReviewView(id=ViewId.REVIEW.value, classes="main-view")
                yield InspectorView(id=ViewId.INSPECTOR.value, classes="main-view")
                yield AgentsView(id=ViewId.AGENTS.value, classes="main-view")
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
        if not self._shell_ready:
            pass
        elif isinstance(action, ComposerChanged):
            # Typing re-renders the menu alone; the rest of the shell has not changed.
            self._menu_index = 0
            self._render_menu()
        elif isinstance(action, EventReceived | ClockTicked):
            self._render_soon()
        else:
            self._render_model(chrome=True)
        for effect in transition.effects:
            self._perform(effect)

    def invoke_command(self, name: str, argument: str = "") -> None:
        spec = spec_for(name)
        if spec is not None and spec.argument and not argument:
            self._put_in_composer(f"/{name} ")
            return
        self.apply_model_action(CommandInvoked(name, argument))

    def _put_in_composer(self, text: str) -> None:
        """Put `text` in the composer as if the person had typed it: the widget, the model
        and the menu all see it at once. Loading text into the widget alone does not raise
        `Changed`, so the model would not know, and a menu for the draft would not open."""
        composer = self.query_one(Composer)
        composer.replace_text(text)
        composer.focus()
        self._reported_draft = text
        if text != self.model.composer_draft:
            self.apply_model_action(ComposerChanged(text))

    def _choices(self) -> Choices:
        return {"mode": self.model.modes, "permissions": self.model.policies}

    def _suggestions(self) -> tuple[Suggestion, ...]:
        return suggest(
            self.model.composer_draft,
            developer=self.model.developer,
            choices=self._choices(),
            skills=self.model.skills,
        )

    def _render_menu(self) -> None:
        commands = self._suggestions()
        self.query_one(Composer).menu_open = bool(commands)
        if commands:
            self._menu_index %= len(commands)
        # Updating a Static lays the whole screen out again, sixty turns and all, and
        # every keystroke came through here -- with the menu hidden and empty, and
        # nothing to show for it. Drawn only when what it would show has changed.
        shown = (tuple(row.insert for row in commands), self._menu_index)
        if shown == self._menu_shown:
            return
        self._menu_shown = shown
        menu = self.query_one("#command-menu", Static)
        if not commands:
            menu.set_class(False, "visible")
            menu.update("")
            return
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
            self._put_in_composer(chosen.insert)

    @on(Composer.Submitted)
    def composer_submitted(self, message: Composer.Submitted) -> None:
        commands = self._suggestions()
        if commands:
            # Enter on the menu runs the highlighted row. One that still needs a value is
            # put in the composer instead, for the person to finish -- and the menu goes on
            # to the values the backend accepts, when it has said what they are.
            chosen = commands[self._menu_index % len(commands)]
            self._reported_draft = ""
            self.apply_model_action(ComposerChanged(""))
            composer = self.query_one(Composer)
            composer.replace_text("")
            if chosen.runnable:
                self.apply_model_action(ComposerSubmitted(chosen.insert))
            else:
                self._put_in_composer(chosen.insert)
            return
        self.apply_model_action(ComposerSubmitted(message.text))

    @on(TextArea.Changed, "#composer")
    def composer_changed(self, message: TextArea.Changed) -> None:
        composer = cast(Composer, message.text_area)
        self._reported_draft = composer.text
        if composer.text != self.model.composer_draft:
            self.apply_model_action(ComposerChanged(composer.text))

    def action_context_escape(self) -> None:
        if isinstance(self.screen, HelpScreen):
            return
        asked = self.model.interaction
        if asked is not None and asked.kind == "approval":
            self.apply_model_action(ApprovalDecided("reject"))
            return
        if self.model.view is not ViewId.CONVERSATION:
            self.apply_model_action(Back())
            self.query_one(Composer).focus()
        elif self.model.active_run_id:
            self.apply_model_action(CommandInvoked("pause"))

    def action_toggle_tools(self) -> None:
        self.apply_model_action(CommandInvoked("tools"))

    def action_decide(self, decision: str) -> None:
        self.apply_model_action(ApprovalDecided(decision))

    @override
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action != "decide":
            return True
        offered = self._approval_keys().values()
        return bool(parameters) and parameters[0] in offered

    @override
    async def action_quit(self) -> None:
        self.apply_model_action(CommandInvoked("quit"))

    def _render_soon(self) -> None:
        """Render once the loop is idle, however many events arrive first.

        For what the backend and the clock send, not for what the person does: a person's
        action is rendered as it happens. A stream that handed over fifty deltas in one
        burst used to render the shell fifty times before the next keystroke could be
        read; now the keystroke waits on one render, and the model is right throughout.
        """
        if self._render_queued:
            return
        self._render_queued = True
        _ = self.call_after_refresh(self._render_queued_model)

    def _render_queued_model(self) -> None:
        self._render_queued = False
        self._render_model(chrome=False)

    def _chrome_key(self) -> tuple[object, ...]:
        """Everything the shell around the transcript is rendered from. A render caused by
        the backend or the clock redraws the shell only when this has changed: a delta or
        a tick changes the transcript, and each of the six widgets around it would
        otherwise be updated -- and laid out again -- for nothing. What a person does is
        always drawn in full."""
        model = self.model
        latest = model.turns[-1] if model.turns else None
        return (
            model.booting,
            model.connected,
            model.submitting,
            model.profile,
            model.run_status,
            model.view,
            model.workspace_path,
            model.folders,
            model.mode,
            model.policy,
            model.usage,
            model.interaction,
            model.composer_draft,
            model.active_run_id,
            model.working,
            model.developer,
            model.modes,
            model.policies,
            model.skills,
            model.viewport_width,
            latest.plan if latest is not None else None,
            latest.plan_explanation if latest is not None else None,
        )

    def _render_model(self, *, chrome: bool = True) -> None:
        width = max(1, self.model.viewport_width)
        host = self.query_one("#view-host", ContentSwitcher)
        host.current = self.model.view.value
        self.query_one(f"#{self.model.view.value}", StateView).update_state(self.model)
        # The notice reads the clock, so it is drawn every time; it is one line.
        notice = self.query_one("#notice", Static)
        shown = render_notice(self.model)
        notice.set_class(shown is not None, "visible")
        notice.update(shown or "")
        key = self._chrome_key()
        if not chrome and key == self._chrome_shown:
            return
        self._chrome_shown = key
        self.query_one("#app-header", Static).update(render_header(self.model, width=width))

        self._render_menu()
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
        if self.model.composer_draft != self._reported_draft:
            # The model changed the draft itself; the widget follows. A widget ahead of the
            # model -- a keystroke whose notice is still queued -- is left alone.
            self._reported_draft = self.model.composer_draft
            composer.replace_text(self.model.composer_draft)
        busy = self.model.booting or self.model.submitting
        if composer.disabled and not busy and self.screen is composer.screen:
            # A widget loses focus when it is disabled and does not get it back when it
            # is not: after every send, the next keystroke went nowhere until the input
            # was focused again by hand. Give it back here, unless the person has put the
            # focus somewhere else meanwhile.
            composer.disabled = False
            if self.focused is None:
                composer.focus()
        else:
            composer.disabled = busy
        composer.placeholder = (
            "Answer the agent…"
            if self.model.interaction is not None and self.model.interaction.kind == "question"
            else "Steer the active run…"
            if self.model.active_run_id
            else "Ask the agent…"
        )
        self.query_one("#app-footer", Static).update(render_footer(self.model))
        composer.approval_keys = self._approval_keys()

    def _approval_keys(self) -> dict[str, str]:
        asked = self.model.interaction
        if asked is None or asked.kind != "approval" or asked.sending:
            return {}
        keys = {"1": "approve", "y": "approve", "3": "reject", "n": "reject", "escape": "reject"}
        if "approve_bash_always" in asked.allowed_decisions:
            keys["2"] = "approve_bash_always"
        return keys

    @on(Composer.Decided)
    def composer_decided(self, message: Composer.Decided) -> None:
        self.apply_model_action(ApprovalDecided(message.decision))

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
                self.push_screen(
                    HelpScreen(
                        developer=self.model.developer,
                        choices=self._choices(),
                        skills=self.model.skills,
                    )
                )
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
                modes=info.modes,
                policies=info.policies,
                skills=info.skills,
            )
        )
        if self.model.thread_id and not self.model.turns:
            self.apply_model_action(ThreadSelected(self.model.thread_id))
        elif self._resume_on_start:
            self.apply_model_action(CommandInvoked("threads"))
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
            # A failed decision is offered again: the reducer clears `sending` on failure.
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
                modes=info.modes,
                policies=info.policies,
                skills=info.skills,
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
