"""Typed inputs to the application reducer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from orca.app.model import TaskEvent, ThreadReplay, ViewId


@dataclass(frozen=True, slots=True)
class BootCompleted:
    profile: str
    endpoint: str
    protocol_version: str
    workspace_id: str
    workspace_name: str
    workspace_path: str
    cwd_relative: str
    reset_conversation: bool = False


@dataclass(frozen=True, slots=True)
class BootFailed:
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
class CommandCompleted:
    command: str
    response: Mapping[str, Any]


Action = (
    BootCompleted
    | BootFailed
    | Navigate
    | Back
    | ViewportChanged
    | ComposerChanged
    | ComposerSubmitted
    | CommandInvoked
    | RunAccepted
    | OperationFailed
    | EventReceived
    | ThreadSelected
    | ThreadLoaded
    | ApprovalDecided
    | QuestionAnswered
    | CommandCompleted
)
