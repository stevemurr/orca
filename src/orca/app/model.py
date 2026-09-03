"""Renderer-neutral state for the terminal application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal


class ViewId(str, Enum):
    CONVERSATION = "conversation"
    REVIEW = "review"
    INSPECTOR = "inspector"


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
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ThreadReplay:
    """Bounded public history for one run in a selected thread."""

    run_id: str
    status: str
    events: tuple[TaskEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgressItem:
    update_id: str
    text: str
    status: str = "active"


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


@dataclass(frozen=True, slots=True)
class InteractionState:
    kind: Literal["approval", "question"]
    request_id: str
    title: str
    summary: str = ""
    command: str = ""
    risk: str = ""
    allowed_decisions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Notice:
    message: str
    level: NoticeLevel = "info"


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
    cwd_relative: str = "."
    thread_id: str | None = None
    active_run_id: str | None = None
    cursor: int = 0
    mode: str = "auto"
    policy: str = "safe"
    run_status: RunStatus = RunStatus.IDLE
    turns: tuple[TurnState, ...] = ()
    interaction: InteractionState | None = None
    composer_draft: str = ""
    submitting: bool = False
    developer: bool = False
    developer_cursor: int = 0
    developer_events: tuple[str, ...] = ()
    notices: tuple[Notice, ...] = ()
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
