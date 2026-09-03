# orca

A terminal client for agent harnesses.

orca is one persistent full-screen shell over a durable run: the conversation stays primary,
progress and approvals are legible without burying it, and a run keeps going when you close the
terminal. What it is *not* is a harness. It runs no models, holds no state about your work, and
knows nothing about how your agent is built. It talks to a backend.

Bring your own. A backend is eight methods.

```sh
uv sync
uv run orca                    # the full-screen client
uv run orca run "…"            # one run, plain text, no UI
```

---

## The contract

`orca.backend.TerminalBackend` is the whole boundary. Everything above it — the reducer, the
renderers, the Textual host, the plain and JSONL output modes — is written against these nine
methods and nothing else.

```python
class TerminalBackend(Protocol):
    async def connect(self) -> SessionInfo: ...
    async def start_run(self, request: RunRequest) -> RunInfo: ...
    def stream(self, run_id: str, *, after_seq: int, developer: bool) -> AsyncGenerator[TaskEvent, None]: ...
    async def send_command(self, run_id: str, command: Command) -> CommandOutcome: ...
    async def switch_workspace(self, selector: str) -> SessionInfo: ...
    async def add_folder(self, thread_id: str | None, path: str) -> ThreadFolders: ...
    async def recent_threads(self) -> tuple[ThreadSummary, ...]: ...
    async def load_thread(self, thread_id: str) -> ThreadHistoryInfo: ...
    async def close(self) -> None: ...
```

| Method | What it must do |
|---|---|
| `connect()` | Connect, resolve the working folder, describe both. Called once, before anything else; everything expensive belongs here, where a failure is one legible message instead of a half-built session. |
| `start_run(request)` | Accept one turn and return its ids **immediately**. Do not wait for the work — everything after acceptance arrives through `stream`. A backend that blocks here freezes the terminal for the length of the task. |
| `stream(run_id, after_seq, developer)` | Yield that run's events in `seq` order, starting after `after_seq`. An async generator, so no `await` on the call. Return when the run reaches a terminal event. |
| `send_command(run_id, command)` | Act on a run in flight. `command` is the `Command` union — `Pause`, `Resume`, `Cancel`, `Steer(content)`, `Answer(question_id, content)`, `ResolveApproval(approval_id, decision)` — so match on it. Raise `BackendError` for one you cannot honour; a command that vanishes reads as a hang. `CommandOutcome.status` becomes a one-line notice. |
| `switch_workspace(selector)` | Rebind the session to another folder, named however a person would name it — a path, a name, an id. Resolve it or raise; orca never guesses. |
| `add_folder(thread_id, path)` | Let the conversation reach one more folder, now and on every later run. The working folder does not change. `thread_id` is `None` before the first message: make the thread and return its id in `ThreadFolders`, and orca keeps using it. |
| `recent_threads()` | List conversations a person might continue, most recent first, as `ThreadSummary` rows. Only `thread_id` is required; the rest render as sensible blanks. Return `()` if you have no history. |
| `load_thread(thread_id)` | Read one conversation's bounded history. Reading only — following a live run is `stream`'s job. |
| `close()` | Release what `connect` acquired. Called once, on the way out. Durable work outlives the client, so this closes connections; it does not cancel runs. |

Every expected failure is a `BackendError`, and its message is shown to the person as written.
Anything else reaches the terminal as a crash.

### Two rules

**Every vocabulary that crosses this boundary is open.** Event kinds, run statuses, plan-step
statuses, modes, approval policies, approval decisions. A value orca does not recognise is
carried and shown, never dropped, and never treated as an ending. Invent event kinds freely: orca
advances its cursor past one it cannot use and renders nothing.

**Nothing here describes how work is organised.** There are no workers, units, stages, phases, or
graphs of any of them, because a harness that runs one model in a loop has none of those and
would have to invent them to answer. A run produces narration, an answer, and sometimes a request
for a decision. That is the entire model.

### The cursor rule

`stream` is the only place a backend has a hard obligation: **the same `after_seq` always yields
the same suffix of the log.** Sequences start at 1, never repeat within a run, and arrive in
order. orca reconnects by calling `stream` again with the last sequence it saw and relies on
losing nothing and seeing nothing twice. Everything else the client does — surviving a dropped
connection, replaying a thread into live state, the developer inspector's separate cursor — rests
on that one property.

A Python backend ends a follow by returning from the generator. Over HTTP that same ending is two
things at once — a `stream.end` frame named on the SSE `event:` line, and the connection closing
right behind it — and `docs/backend-contract.md` spells both out, because a stream that sends
neither hangs the client with no error rather than failing it.

---

## Events

Every event is one `TaskEvent`:

```python
@dataclass(frozen=True, slots=True)
class TaskEvent:
    sequence: int                 # `seq`, 1-based, ordered, never repeated
    event_id: str                 # opaque; a fallback identity for approvals and questions
    kind: str                     # what happened. an open vocabulary
    visibility: str               # "user", or "developer" for the inspector only
    payload: Mapping[str, Any]    # everything else
```

These are the kinds orca renders. A backend that emits only `run.created`, `answer.delta` and
`run.completed` already drives the whole conversation view.

| Kind | Payload orca reads | What it does with it |
|---|---|---|
| `run.created` | `message`, `mode`, `approval_policy` | Opens the turn with the person's own request. |
| `run.progress` | `update_id`, `text`, `status` | One quiet activity row, upserted by `update_id`, so a step that changes its mind does not stack. `status` is `active`, `completed` or `failed`. |
| `answer.delta` | `effect_id`, `model_call_id`, `text` | Streams the answer as it is written. The **pair** identifies one attempt: a delta from a different pair means the previous attempt was abandoned or replayed, so what was streamed is discarded and the new one starts clean. Both strings are opaque — a backend with one attempt identity may repeat it in both. |
| `plan.progress` | `explanation`, `plan[]` of `{step, status}` | The model's own checklist, rendered above the activity rows. Each event carries the **complete** list and replaces the previous one; there is no merge rule. Nothing counts the steps, so two `in_progress` rows render as two. |
| `plan.available` | `artifact_id`, `path` | Offers an artifact in the conversation and the review view. |
| `approval.requested` | `approval_id`, `title`, `summary`, `risk`, `arguments.argv`, `allowed_decisions`, `grant` | Asks at the end of the turn and parks the run. `title` is written for a person to judge. `allowed_decisions` is what is offered — orca binds `1` to `approve`, `3` to `reject`, and `2` to `approve_bash_always`, the one persistent grant it knows by sight, when the request lists it. `grant` says in words what that grant would cover, and orca shows it on the choice. |
| `approval.resolved` | — | Dismisses the modal. |
| `question.requested` | `question_id`, `prompt` | Asks inline above the composer; the next thing typed becomes the answer. |
| `question.resolved` | — | Dismisses it. |
| `run.paused` | — | Not an ending. |
| `run.completed` / `.failed` / `.cancelled` / `.blocked` | `summary` | Terminal — exactly one, and nothing after it. `summary` replaces the streamed answer and is the canonical result. |

Anything else advances the cursor and is otherwise ignored, which is what makes adding an event
kind safe. Events with `visibility: "developer"` are routed to `/inspect` on their own cursor and
never reach the conversation.

---

## Writing a backend

Two ways in, depending on what your harness already is.

### It speaks HTTP

Implement [`docs/backend-contract.md`](docs/backend-contract.md) — a dozen endpoints under
`/api/v1` and an SSE stream — and orca needs no code at all. Point it at the endpoint:

```sh
orca --url https://harness.example          # once
ORCA_URL=https://harness.example orca       # by environment
orca auth login --url https://harness.example   # save a profile and a credential
```

Credentials live in the system credential store, bound to the exact endpoint, so changing the
endpoint can never carry a token with it. `ORCA_AUTH_TOKEN` and `ORCA_AUTH_TOKEN_FILE` are the
headless paths.

If your harness runs locally and orca should start it, say how:

```sh
export ORCA_SERVER_COMMAND="python -m myharness.serve"
orca server start        # or just `orca`, which starts it on demand
```

`{host}` and `{port}` are substituted where they appear and appended as `--host`/`--port` where
they do not. orca only ever manages a plain-HTTP loopback endpoint on the default profile, and
only signals a process whose `/api/v1/health` still echoes the `ORCA_MANAGED_INSTANCE_ID` it was
started with.

### It is Python

Implement the protocol and hand it to the app. There is no plug-in registry: a custom backend is
a launcher script you run instead of the `orca` command.

```python
"""echoharness.py — a complete backend. `python echoharness.py` gives you the whole client."""

import asyncio
import itertools
from collections.abc import AsyncGenerator

from orca.app.model import TaskEvent
from orca.backend import (
    CommandOutcome,
    RunInfo,
    RunRequest,
    SessionInfo,
    ThreadFolders,
    ThreadHistoryInfo,
    ThreadSummary,
)
from orca.tui.app import OrcaApp


class EchoBackend:
    def __init__(self) -> None:
        self._runs = itertools.count(1)
        self._pending: dict[str, str] = {}

    async def connect(self) -> SessionInfo:
        return SessionInfo(
            profile="echo",
            endpoint="in-process",
            protocol_version="1",
            workspace_id="ws-1",
            workspace_name="here",
            workspace_path="~/Code/example",
        )

    async def start_run(self, request: RunRequest) -> RunInfo:
        run_id = f"run-{next(self._runs)}"
        self._pending[run_id] = request.message
        return RunInfo(run_id, request.thread_id or "thread-1")

    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncGenerator[TaskEvent, None]:
        message = self._pending.pop(run_id, "")
        events = [
            ("run.created", {"message": message}),
            ("run.progress", {"update_id": "think", "text": "Thinking about it.", "status": "active"}),
            ("run.completed", {"summary": f"You said: {message}"}),
        ]
        for sequence, (kind, payload) in enumerate(events, start=1):
            if sequence <= after_seq:
                continue          # the cursor rule: the same after_seq, the same suffix
            await asyncio.sleep(0.3)
            yield TaskEvent(sequence, f"evt-{sequence}", kind, "user", payload)

    async def send_command(self, run_id, command) -> CommandOutcome:
        return CommandOutcome("accepted")

    async def switch_workspace(self, selector: str) -> SessionInfo:
        return await self.connect()

    async def add_folder(self, thread_id: str | None, path: str) -> ThreadFolders:
        return ThreadFolders(thread_id or "thread-1", ("~/Code/example", path))

    async def recent_threads(self) -> tuple[ThreadSummary, ...]:
        return ()

    async def load_thread(self, thread_id: str) -> ThreadHistoryInfo:
        return ThreadHistoryInfo(thread_id, "", ())

    async def close(self) -> None:
        return None


if __name__ == "__main__":
    OrcaApp(EchoBackend()).run()
```

The same object drives the non-interactive path, which shares the reducer rather than
reimplementing it:

```python
import asyncio
from orca.backend import RunRequest
from orca.output.plain import run_once

exit_code = asyncio.run(
    run_once(EchoBackend(), RunRequest("hello", None, "ws-1", ".", "normal", "ask"))
)
```

---

## Using it

`orca` or `orca chat` opens the full-screen client. Inside it:

| | |
|---|---|
| `/chat` `/review` `/threads` `/new` | conversation, the result and its artifacts, pick a conversation, start one |
| `/resume` `/pause` `/cancel` | act on the run in flight |
| `/mode <m>` `/permissions <p>` | set the two strings passed to the backend on the next turn; the menu lists the values the backend advertises |
| `/workspace <path>` `/add <path>` `/status` `/help` | rebind the folder, reach one more folder from this conversation, report, list everything |
| `/inspect` | developer events, on their own cursor, never mixed into the conversation |
| Enter · Shift+Enter · Esc · Ctrl+P | send · newline · back, or pause from the conversation · command palette |

Approvals open a modal with explicit one-shot, persistent-when-offered, and reject choices.
Questions stay inline above the composer.

`orca run "…"` is the non-interactive path: stable plain text for people, `--jsonl` for a
versioned machine-readable stream, `--no-follow` to print the accepted run id and detach. It
exits **2** after reporting an approval or a question, so an unattended process never appears to
hang.

A `/name` the menu lists as a skill of the workspace is sent as written, and the backend reads the
skill's instructions as the request; anything after the name is what to apply them to.

`orca --resume` opens on the recent conversations, to pick one up; `orca --thread <id>` opens one
by id. `orca threads`, `orca auth login|status|logout`, and `orca server status|start|stop|restart`
are the rest of the surface.

---

## Design

See [`docs/terminal-client.md`](docs/terminal-client.md). The short version: typed actions reduce
into immutable state, pure Rich functions render that state, and the Textual host owns every
effect and the single event cursor. `src/orca/app/` imports neither Textual nor a transport
library, `tests/test_architecture.py` keeps it that way, and the reducer is the reason live
events and replayed history cannot disagree.

## Provenance

Extracted from the `orchestrator` project's `orch` client, which is where the dated findings in
the comments come from. The work-graph view came out on the way: it described one harness's
decomposition of a task into units, which is not a thing a general client can ask a backend for.
Nothing replaced it.
