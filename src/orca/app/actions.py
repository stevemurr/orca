"""Typed inputs to the application reducer."""

from __future__ import annotations

from dataclasses import dataclass

from orca.app.model import Choice, TaskEvent, ThreadReplay, ViewId
from orca.backend import CommandOutcome


@dataclass(frozen=True, slots=True)
class Connected:
    profile: str
    endpoint: str
    protocol_version: str
    workspace_id: str
    workspace_name: str
    workspace_path: str
    reset_conversation: bool = False
    modes: tuple[Choice, ...] = ()
    policies: tuple[Choice, ...] = ()


@dataclass(frozen=True, slots=True)
class ConnectFailed:
    message: str


@dataclass(frozen=True, slots=True)
class Navigate:
    view: ViewId


@dataclass(frozen=True, slots=True)
class Back:
    pass


@dataclass(frozen=True, slots=True)
class ViewportChanged:
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ComposerChanged:
    text: str


@dataclass(frozen=True, slots=True)
class ComposerSubmitted:
    text: str


@dataclass(frozen=True, slots=True)
class CommandInvoked:
    name: str
    argument: str = ""


@dataclass(frozen=True, slots=True)
class RunAccepted:
    run_id: str
    thread_id: str
    #: Monotonic seconds, from the host that knows the time. Zero when it does not.
    started_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ClockTicked:
    """The host's clock, while a run is going, so elapsed time can be shown."""

    now: float


@dataclass(frozen=True, slots=True)
class OperationFailed:
    message: str


@dataclass(frozen=True, slots=True)
class EventReceived:
    event: TaskEvent


@dataclass(frozen=True, slots=True)
class ThreadSelected:
    thread_id: str
    title: str = ""


@dataclass(frozen=True, slots=True)
class ThreadLoaded:
    thread_id: str
    title: str
    runs: tuple[ThreadReplay, ...]


@dataclass(frozen=True, slots=True)
class ApprovalDecided:
    decision: str


@dataclass(frozen=True, slots=True)
class QuestionAnswered:
    answer: str


@dataclass(frozen=True, slots=True)
class FolderAdded:
    """The backend widened the conversation. `thread_id` may be new: see `add_folder`."""

    thread_id: str
    folders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandCompleted:
    command: str
    outcome: CommandOutcome


Action = (
    Connected
    | ConnectFailed
    | Navigate
    | Back
    | ViewportChanged
    | ComposerChanged
    | ComposerSubmitted
    | CommandInvoked
    | RunAccepted
    | ClockTicked
    | OperationFailed
    | EventReceived
    | ThreadSelected
    | ThreadLoaded
    | ApprovalDecided
    | QuestionAnswered
    | FolderAdded
    | CommandCompleted
)
