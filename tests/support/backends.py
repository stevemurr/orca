"""A complete `TerminalBackend` for tests that drive a single run.

Most tests need a backend that connects, accepts one run, and streams a fixed log; nothing
else is ever asked of it. Writing those three methods and nothing more used to leave the
double short of the port, which type-checked only because every call site suppressed the
mismatch -- and a double that is not a `TerminalBackend` cannot catch the port growing a
method the client has started to call.

Subclass it and override `events`, or `stream` when the log needs to do something a fixed
sequence cannot. Everything else refuses loudly.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence

from orca.app.model import TaskEvent
from orca.backend import (
    Command,
    CommandOutcome,
    RunInfo,
    RunRequest,
    SessionInfo,
    ThreadHistoryInfo,
    ThreadSummary,
)


class ScriptedBackend:
    session = SessionInfo("local", "http://localhost", "1.6", "ws-1", "project", "/project")
    accepted = RunInfo("run-1", "thread-1")

    def events(self) -> Sequence[TaskEvent]:
        """The log `stream` replays, in order."""
        return ()

    async def connect(self) -> SessionInfo:
        return self.session

    async def start_run(self, request: RunRequest) -> RunInfo:
        del request
        return self.accepted

    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncGenerator[TaskEvent, None]:
        del run_id, after_seq, developer
        for item in self.events():
            yield item

    async def send_command(self, run_id: str, command: Command) -> CommandOutcome:
        raise AssertionError(f"unexpected command for {run_id}: {command!r}")

    async def switch_workspace(self, selector: str) -> SessionInfo:
        raise AssertionError(f"unexpected workspace switch: {selector!r}")

    async def recent_threads(self) -> tuple[ThreadSummary, ...]:
        return ()

    async def load_thread(self, thread_id: str) -> ThreadHistoryInfo:
        raise AssertionError(f"unexpected thread load: {thread_id!r}")

    async def close(self) -> None:
        return None
