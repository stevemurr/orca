"""The port every harness implements, and the values that cross it.

This is orca's whole contract with the thing doing the work. Everything above it — the reducer,
the renderers, the Textual host, the plain and JSONL output modes — is written against these
eight methods and nothing else, so a harness that satisfies them gets the entire terminal
client and orca learns nothing about how the harness is built.

`orca.http_backend.HttpBackend` is one implementation, speaking the HTTP wire contract in
`docs/backend-contract.md`. It is the first one, not the definition: a harness that cannot
serve that contract implements `TerminalBackend` directly, in process, and loses nothing.

Two rules keep the port honest, and both are worth stating because breaking either is how a
generic client stops being generic:

* **Every vocabulary that crosses this boundary is open.** Event kinds, run statuses, plan-step
  statuses, modes, approval policies, approval decisions: a value orca does not recognise is
  carried and shown, never dropped and never treated as an ending. A backend may invent event
  kinds freely; orca advances its cursor past one it cannot use and renders nothing.

* **Nothing here describes how work is organised.** There is no notion of workers, units,
  stages, phases, or a graph of any of them, because a harness that runs one model in a loop
  has none of those and would have to lie. A run produces narration, an answer, and sometimes a
  request for a decision. That is the whole model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from orca.app.model import TaskEvent, ThreadReplay


class BackendError(RuntimeError):
    """A stable user-facing failure from a terminal backend operation.

    Every method below raises this and only this for an expected failure — unreachable
    endpoint, refused request, malformed response. Its message is shown to the person as
    written, so it is prose about what went wrong, not a stack trace or an error code. An
    exception of any other type reaches the terminal as a crash.
    """


@dataclass(frozen=True, slots=True)
class BootInfo:
    """Who orca is talking to, and which folder the work happens in.

    Returned by `boot()` and again by `switch_workspace()`; the header, the welcome panel and
    every `start_run` request are built from it.
    """

    #: The connection profile in use. Cosmetic; shown in the header on wide terminals.
    profile: str
    #: Where the backend is, as a person would write it. Shown in the welcome panel.
    endpoint: str
    #: The backend's own contract version, for a client that wants to record it.
    protocol_version: str
    #: Opaque identity of the working folder, sent back on every `RunRequest`.
    workspace_id: str
    #: Human-readable name for that folder.
    workspace_name: str
    #: Display path for that folder — `~`-relative is friendlier than absolute. Shown in the
    #: header, and it must name the folder the run will actually touch. A prettier path than
    #: the real one is the client's most dangerous possible lie.
    workspace_path: str
    #: Where inside the folder the person is standing, relative and without `..`.
    cwd_relative: str = "."


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One turn a person asked for."""

    message: str
    #: Continue this conversation, or None to let the backend start one.
    thread_id: str | None
    workspace_id: str
    cwd_relative: str
    #: How much effort to spend. A backend-defined string orca passes through unread; the
    #: person sets it with `/mode` and it defaults to `auto`.
    mode: str
    #: How much to ask about. A backend-defined string orca passes through unread; the person
    #: sets it with `/permissions` and it defaults to `safe`.
    policy: str


@dataclass(frozen=True, slots=True)
class RunInfo:
    """What `start_run` accepted. Both ids must be usable immediately."""

    run_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class ThreadHistoryInfo:
    """Bounded replay of one conversation, oldest run first.

    Each `ThreadReplay` carries that run's public events in `seq` order. orca feeds them
    through the same reducer live events go through, so history and live state cannot drift
    apart; a run whose events are empty still shows its turn from `status` alone.
    """

    thread_id: str
    title: str
    runs: tuple[ThreadReplay, ...]


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """One conversation, as a listing shows it.

    Typed rather than a loose dict, which is what this was until 2026-08-30. It was the only
    method on the protocol returning `dict[str, object]`, so its docstring had to name the
    four keys it read -- a harness author learned the shape from prose in the one place they
    could not get it from the type. Every field but `thread_id` is optional and renders as a
    sensible blank, so a backend that tracks nothing but ids still lists correctly.
    """

    thread_id: str
    title: str = ""
    #: How the most recent run in this thread ended. Backend vocabulary, shown as written --
    #: orca does not interpret it, so a backend with its own statuses is not constrained.
    latest_run_status: str = ""
    #: ISO-8601 if you have it. Anything unparseable is simply not shown.
    updated_at: str = ""


class TerminalBackend(Protocol):
    """All I/O the terminal may request, kept outside widgets and reducers."""

    async def boot(self) -> BootInfo:
        """Connect, resolve the working folder, and describe both.

        Called once at startup, before anything else. Everything expensive — starting a local
        process, discovering a repository, registering a folder — belongs here, where a failure
        is one legible message instead of a half-built session. Raise `BackendError` and orca
        shows it in place of the conversation.
        """
        ...

    async def start_run(self, request: RunRequest) -> RunInfo:
        """Accept one turn and return immediately with its identities.

        This must not wait for the work. It returns as soon as the run is durable enough to be
        followed, and everything after that arrives through `stream`. A backend that blocks
        here freezes the terminal for the length of the task.
        """
        ...

    def stream(
        self,
        run_id: str,
        *,
        after_seq: int,
        developer: bool,
    ) -> AsyncIterator[TaskEvent]:
        """Yield this run's events in `seq` order, starting after `after_seq`.

        The one cursor rule: the same `after_seq` always yields the same suffix. orca reconnects
        by calling again with the last sequence it saw, and relies on losing nothing and seeing
        nothing twice. Sequences start at 1 and never repeat within a run.

        `developer` asks for developer-visibility events as well as user-visible ones; they are
        routed to the inspector and never mixed into the conversation. A backend with nothing
        private to show ignores the flag.

        Return when the run reaches a terminal event. Raise `BackendError` when the stream is
        lost and cannot be recovered — reconnection is the backend's job, because only it knows
        what a recoverable disconnect looks like.

        Note this is the one method that is not `async def`: it is an async *generator*
        function, so it is called without `await`.
        """
        ...

    async def send_command(
        self,
        run_id: str,
        command: str,
        fields: dict[str, str],
    ) -> dict[str, object]:
        """Act on a run in flight, and return whatever the backend says about it.

        orca sends six commands, and a backend that cannot honour one should refuse it rather
        than silently accept it:

        * `pause` / `resume` / `cancel` — no fields.
        * `steer` — `{"content": <text>}`, a further instruction for a run already going.
        * `answer` — `{"question_id": ..., "content": ...}`, replying to `question.requested`.
        * `resolve_approval` — `{"approval_id": ..., "decision": ...}`, where the decision is
          one of the values that request offered.

        The return value is shown as a one-line notice using its `status` key if it has one, so
        `{"status": "cancelling"}` reads better than `{}`. Any command may be retried: orca does
        not deduplicate, so a backend that cares must carry its own idempotency.
        """
        ...

    async def switch_workspace(self, selector: str) -> BootInfo:
        """Rebind the session to another folder, named however a person would name it.

        `selector` is whatever followed `/workspace` — a path, a name, an id. Resolve it or
        raise `BackendError`; orca never guesses on the backend's behalf. Only called while no
        run is active, and the returned `BootInfo` resets the conversation.
        """
        ...

    async def recent_threads(self) -> tuple[ThreadSummary, ...]:
        """List conversations a person might continue, most recent first.

        A backend with no conversation history returns an empty tuple and `/threads` says so.
        Rows without a `thread_id` cannot be continued and are dropped rather than shown.
        """
        ...

    async def load_thread(self, thread_id: str) -> ThreadHistoryInfo:
        """Read one conversation's bounded history without opening a second live cursor.

        Following the run, if one is still going, is orca's job through `stream`. This method
        only reads. Keep it bounded — a thread with a thousand runs should return the recent
        ones rather than everything.
        """
        ...

    async def close(self) -> None:
        """Release whatever `boot` acquired. Called exactly once, on the way out.

        Durable work is expected to outlive the client, so this closes connections; it does not
        cancel runs.
        """
        ...
