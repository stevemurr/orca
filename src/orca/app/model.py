"""Renderer-neutral state for the terminal application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal, cast

from orca.json_types import JsonObject


class ViewId(str, Enum):
    CONVERSATION = "conversation"
    REVIEW = "review"
    INSPECTOR = "inspector"
    AGENTS = "agents"


class RunStatus(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_INPUT = "awaiting_input"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: How loudly a notice is shown. Closed, because it is orca's own vocabulary: a backend never
#: sends one, so there is no unknown value to carry.
NoticeLevel = Literal["info", "warning", "error"]

#: How long a notice stays, by how much it matters. An error waits to be read; a decision
#: is confirmed and gone. One table for the reducer that prunes and the renderer that
#: shows, so a notice is never drawn after it is dropped, or dropped while still drawn.
NOTICE_SECONDS: Mapping[NoticeLevel, float] = {"info": 3.0, "warning": 6.0, "error": 10.0}


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """One decoded public backend event.

    The backend adapter normalizes the wire envelope once.  Reducers and views never need to
    understand SSE framing or any backend's own Python models.
    """

    sequence: int
    event_id: str
    kind: str
    visibility: str
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class ThreadReplay:
    """Bounded public history for one run in a selected thread."""

    run_id: str
    status: str
    events: tuple[TaskEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class Snippet:
    """Code a tool call is about: the file a write creates, or the change an edit makes.

    Read from the call's raw arguments -- `content` for a write, `old` and `new` for an
    edit -- so a person sees the code, not a byte count. Shown on an approval before the
    call, and under the activity row in the transcript after it.
    """

    title: str
    #: A lexer name, or empty to guess from `title`.
    language: str
    text: str


@dataclass(frozen=True, slots=True)
class ProgressItem:
    update_id: str
    text: str
    status: str = "active"
    #: The code this call wrote or changed, when the event carried its arguments.
    snippets: tuple[Snippet, ...] = ()
    #: What sort of thing the call does -- `read`, `search`, `edit`, `execute`, `fetch`,
    #: `think`, `switch_mode` -- in the backend's words. Empty when it did not say. An
    #: open vocabulary: a kind orca has no icon for gets the plain one.
    kind: str = ""
    #: The tool's name as the backend calls it -- `read_file`, `run` -- and the one
    #: argument that says what it was pointed at: a path, a command, a query. Shown as
    #: `ReadFile src/app.py`, the name as prose and the argument beside it; `text` is
    #: the backend's own one-line summary and is what a row shows when it named no tool.
    tool: str = ""
    detail: str = ""
    #: The delegated agent whose call this is, or empty for the run's own agent. A row
    #: with one belongs to that agent, not to the turn's timeline.
    agent_id: str = ""


@dataclass(frozen=True, slots=True)
class AgentState:
    """One delegated agent, as a thing with a life of its own.

    The backend used to send a child's tool calls as rows with its id in their text, and
    its words as the parent's own answer -- so a person watching saw the parent doing the
    child's work. Now the child's rows carry its id, its words arrive as `agent.said`, and
    `agent.started` and `agent.finished` bound its life, which is what lets it be drawn
    as itself. (2026-09-03)
    """

    agent_id: str
    task: str = ""
    #: `running`, `finished`, `failed` or `stopped`: the backend's word, kept as it came.
    status: str = "running"
    #: On the same clock as `AppState.clock`; zero when it started before this process.
    started_at: float = 0.0
    seconds: float = 0.0
    turns: int = 0
    answer: str = ""
    #: What it said as it went: its narration and its reports, in order.
    said: tuple[str, ...] = ()
    progress: tuple[ProgressItem, ...] = ()

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One row of the working model's own checklist, as `plan.progress` published it.

    `status` is kept as the backend's string rather than an enum. It is an open vocabulary like
    every other public status field, and a client that closed it would drop a row it does not
    recognise instead of showing the step - which for a checklist is the worse failure.
    """

    step: str
    status: str = "pending"


@dataclass(frozen=True, slots=True)
class ArtifactOffer:
    artifact_id: str
    kind: str
    title: str
    reference: str = ""


#: Something that happened to a turn that is neither activity nor answer: the context was
#: handed off to a summary, the person steered the run, a folder was added. Orca's own
#: vocabulary, so it is closed.
TurnNoteKind = Literal["compaction", "steer", "folder", "ended", "agent"]


@dataclass(frozen=True, slots=True)
class TurnNote:
    kind: TurnNoteKind
    text: str


@dataclass(frozen=True, slots=True)
class Narration:
    """A stretch of the model's own words, between activity rows."""

    text: str


@dataclass(frozen=True, slots=True)
class Activity:
    """An activity row's place in the turn. The row itself lives in `TurnState.progress`, where
    a later event upserts it by id without moving it."""

    update_id: str


#: One turn as it happened: words, tool calls and notes in arrival order. A turn used to keep
#: its activity rows and its answer apart and render every row above the whole answer, so a
#: tool called between two paragraphs showed up before the first. (2026-09-03)
Segment = Narration | Activity | TurnNote


@dataclass(frozen=True, slots=True)
class TurnState:
    run_id: str
    request: str = ""
    mode: str = ""
    policy: str = ""
    progress: tuple[ProgressItem, ...] = ()
    provisional_answer: str = ""
    answer_stream: tuple[str, str] = ("", "")
    answer: str = ""
    status: str = "running"
    artifacts: tuple[ArtifactOffer, ...] = ()
    #: Replaced wholesale by each `plan.progress`, because that is what the event carries.
    plan: tuple[PlanStep, ...] = ()
    plan_explanation: str = ""
    notes: tuple[TurnNote, ...] = ()
    timeline: tuple[Segment, ...] = ()
    #: The agents this turn delegated to, in the order they started.
    agents: tuple[AgentState, ...] = ()


@dataclass(frozen=True, slots=True)
class InteractionState:
    kind: Literal["approval", "question"]
    request_id: str
    title: str
    summary: str = ""
    command: str = ""
    risk: str = ""
    allowed_decisions: tuple[str, ...] = ()
    #: For a question: the agent's guesses at the answer. Hints, not a closed set -- a
    #: person may pick one by number or type something else.
    options: tuple[str, ...] = ()
    #: For an approval: the code it is about, when the arguments carry any.
    snippets: tuple[Snippet, ...] = ()
    #: For an approval: a decision has been sent and not yet answered. The choices are
    #: shown again if the send fails, so the person can try again.
    sending: bool = False
    #: For an approval: what answering "always" would cover, in the backend's words --
    #: `git commands`, `file writes` -- so the choice says its own scope. Empty when the
    #: backend did not say.
    grant: str = ""


@dataclass(frozen=True, slots=True)
class Notice:
    """A line for a moment, near the composer: what a command did, what was decided.

    Not part of the transcript. It is shown from `shown_at` on the state's clock until its
    level's time is up, and then it is gone; the transcript keeps what happened, not what
    was said about it in passing.
    """

    message: str
    level: NoticeLevel = "info"
    #: Zero means the clock had not been read when the notice was made -- a boot-time
    #: error, say. Such a notice is fresh until the first tick stamps it.
    shown_at: float = 0.0

    def expired(self, now: float) -> bool:
        """Whether its time is up at `now`. A notice never stamped is still fresh."""
        return self.shown_at > 0 and now - self.shown_at >= NOTICE_SECONDS[self.level]


@dataclass(frozen=True, slots=True)
class Choice:
    """One value a setting may take, as the backend advertised it: the word, and what it
    means, so a menu can show both and the pick is made on the meaning."""

    name: str
    summary: str = ""

    @staticmethod
    def parse_all(value: object) -> tuple[Choice, ...]:
        """Choices from a wire list: a bare name, or a name with a summary. An entry of
        another shape is dropped, as every unknown thing from a backend is, and a list
        that is not a list is nothing at all."""
        if not isinstance(value, list):
            return ()
        found: list[Choice] = []
        for item in cast("list[object]", value):
            if isinstance(item, str) and item:
                found.append(Choice(item))
            elif isinstance(item, dict):
                entry = cast("dict[str, object]", item)
                name = entry.get("name")
                summary = entry.get("summary")
                if isinstance(name, str) and name:
                    found.append(Choice(name, summary if isinstance(summary, str) else ""))
        return tuple(found)


@dataclass(frozen=True, slots=True)
class Usage:
    """How full the context is, as the backend measured its last request."""

    tokens: int
    context_window: int
    #: The backend's estimate rather than the endpoint's own count.
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class AppState:
    """Complete semantic state of one interactive client process."""

    view_stack: tuple[ViewId, ...] = (ViewId.CONVERSATION,)
    connected: bool = False
    booting: bool = True
    profile: str = "default"
    endpoint: str = ""
    protocol_version: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    workspace_path: str = ""
    #: Folders this conversation reaches beyond the workspace, absolute, as the backend
    #: reports them. Per thread: reset with the conversation.
    folders: tuple[str, ...] = ()
    thread_id: str | None = None
    active_run_id: str | None = None
    cursor: int = 0
    mode: str = "normal"
    policy: str = "ask"
    #: What the backend said `mode` and `policy` may be; empty when it did not say.
    modes: tuple[Choice, ...] = ()
    policies: tuple[Choice, ...] = ()
    #: The skills the workspace offers, for the `/` menu: a `/name` that is one is sent
    #: as written, and the backend reads the skill's instructions as the request.
    skills: tuple[Choice, ...] = ()
    run_status: RunStatus = RunStatus.IDLE
    turns: tuple[TurnState, ...] = ()
    interaction: InteractionState | None = None
    composer_draft: str = ""
    submitting: bool = False
    #: An `/add` is with the backend and not yet answered. Before the first message it is
    #: also what makes the thread, so a message sent meanwhile would make a second one.
    adding_folder: bool = False
    #: A message was on its way when the conversation was reset under it, by a workspace
    #: switch or a reconnect. The run it starts belongs to the conversation that was left.
    orphaned_submission: bool = False
    developer: bool = False
    developer_cursor: int = 0
    developer_events: tuple[str, ...] = ()
    notices: tuple[Notice, ...] = ()
    #: When the active run was accepted and the clock now, both on the same monotonic
    #: scale, so a renderer can show elapsed time without reading a clock of its own. Zero
    #: when unknown -- a run picked up from history was accepted before this process began.
    run_started_at: float = 0.0
    clock: float = 0.0
    usage: Usage | None = None
    #: Whether every tool call is shown, or a run of them folds to its latest and a count.
    tools_expanded: bool = False
    viewport_width: int = 100
    viewport_height: int = 30

    @property
    def view(self) -> ViewId:
        return self.view_stack[-1]

    @property
    def working(self) -> bool:
        return self.run_status in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.AWAITING_INPUT,
            RunStatus.PAUSED,
        }

    @property
    def latest_run_id(self) -> str:
        if self.active_run_id:
            return self.active_run_id
        return self.turns[-1].run_id if self.turns else ""

    @property
    def live_notices(self) -> tuple[Notice, ...]:
        """The notices still within their time on this clock, oldest first."""
        return tuple(notice for notice in self.notices if not notice.expired(self.clock))
