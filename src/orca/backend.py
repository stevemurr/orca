"""Ports used by the interactive application runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from orca.app.model import TaskEvent, ThreadReplay, WorkUnitSpec


class BackendError(RuntimeError):
    """A stable user-facing failure from a terminal backend operation."""


@dataclass(frozen=True, slots=True)
class BootInfo:
    profile: str
    endpoint: str
    protocol_version: str
    workspace_id: str
    workspace_name: str
    workspace_path: str
    cwd_relative: str = "."
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RunRequest:
    message: str
    thread_id: str | None
    workspace_id: str
    cwd_relative: str
    mode: str
    policy: str


@dataclass(frozen=True, slots=True)
class RunInfo:
    run_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class WorkGraphInfo:
    graph_fingerprint: str
    units: tuple[WorkUnitSpec, ...]


@dataclass(frozen=True, slots=True)
class ThreadHistoryInfo:
    thread_id: str
    title: str
    runs: tuple[ThreadReplay, ...]


class TerminalBackend(Protocol):
    """All I/O the TUI may request, kept outside widgets and reducers."""

    async def boot(self) -> BootInfo: ...

    async def start_run(self, request: RunRequest) -> RunInfo: ...

    def stream(
        self,
        run_id: str,
        *,
        after_seq: int,
        developer: bool,
    ) -> AsyncIterator[TaskEvent]: ...

    async def send_command(
        self,
        run_id: str,
        command: str,
        fields: dict[str, str],
    ) -> dict[str, object]: ...

    async def load_work_graph(
        self,
        run_id: str,
        artifact_id: str = "",
    ) -> WorkGraphInfo: ...

    async def switch_workspace(self, selector: str) -> BootInfo: ...

    async def recent_threads(self) -> tuple[dict[str, object], ...]: ...

    async def load_thread(self, thread_id: str) -> ThreadHistoryInfo: ...

    async def close(self) -> None: ...
