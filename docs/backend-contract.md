# The HTTP backend contract

What a backend must serve for `orca.http_backend.HttpBackend` to drive the whole terminal
client. Base path `/api/v1`.

This is a client-side document. It describes the endpoints orca calls and the events orca reads,
and nothing else: a harness is free to serve fifty more endpoints, and orca will never know. The
converse also holds — if orca cannot do something without knowing how your harness is built,
that is a gap here rather than a reason to reach past it.

Speaking HTTP is optional. `orca.backend.TerminalBackend` is the actual boundary, and a Python
harness implements those nine methods directly. This document exists because most harnesses
already have a server.

---

## Rules that apply everywhere

**Errors** are `{"detail": {"code", "message"}}`. `message` is shown to a person, so write it for
one. A body of any other shape still works — a bare `{"detail": "Not Found"}`, or HTML from a
proxy that never reached you, is reported with the status code rather than crashing the client —
but the structured form is the only one that names its own failure.

**Unknown fields and unknown event types are ignored, not rejected.** Adding either is
non-breaking. The same applies to any `status`: a value orca does not know is treated as *still
going*, never as an ending.

**Authentication** is a static bearer token when the backend wants one:
`Authorization: Bearer <token>`. orca sends it on every request once a credential is configured,
and sends none when there is none.

**Idempotency.** `POST …/runs` carries an `Idempotency-Key` header and every command carries a
`command_id`. orca retries a `POST` whose connection failed before a response arrived, with the
same body and the same identity, so a backend that does not deduplicate on these will start the
same run twice.

---

## Discovery

### `GET /capabilities`

```json
{
  "protocol_version": "1",
  "modes": [
    {"name": "normal", "summary": "Read and change things, asking as the policy says."},
    {"name": "plan", "summary": "Read only, until you approve a plan."}
  ],
  "approval_policies": [
    {"name": "ask", "summary": "Ask before anything that changes the machine."},
    {"name": "edits", "summary": "Write files without asking; commands still ask."},
    {"name": "full-access", "summary": "Never ask."}
  ]
}
```

orca reads `protocol_version`, and two optional lists. `modes` and `approval_policies` are the
values `/mode` and `/permissions` accept, in your words, each a name with a summary -- a bare
string is a name with none. Listed, they are offered as a menu once the command is typed, the
summary beside each, shown in `/help`, and a value not on the list is refused with the list
before it reaches you. Omitted, whatever is typed is passed through.

Anything else you advertise is ignored: there is no feature flag orca gates a control on, because
a feature map is a promise about *your* architecture and the client has no view that could
honour one.

### `GET /health`

Only used by local process management, and only then.

```json
{"status": "ok", "detail": {"managed_instance_id": "<opaque>"}}
```

`detail.managed_instance_id` must echo the `ORCA_MANAGED_INSTANCE_ID` environment variable orca
set when it started the process, and be empty otherwise. It is an ownership marker, not
authority: orca will not signal a process that cannot prove it is the one orca started. A backend
that is never process-managed does not need this route.

A process orca starts is also told the credential, when the profile has one, as `HARNESS_TOKEN`,
and is expected to require it as the bearer token on every route. Every other `ORCA_*` variable
in orca's own environment is forwarded as it is, so a backend reads its configuration from
variables an operator exported under that prefix.

---

## Workspaces

A workspace is one registered folder the backend may work in. orca discovers the folder locally —
it is the only side that can see where the person was standing — and asks the backend to bind it.

### `GET /workspaces`

`[{"workspace_id", "name", "root_path", "vcs", "repo_identity", "skills"}, …]`. `root_path` is
matched against the resolved local folder **exactly**; a registered ancestor is deliberately
ignored, because showing the launch directory while submitting a run against a broader folder
elsewhere is the most dangerous lie a client can tell.

`skills`, optional, lists the skills the backend found beside the folder, each a name with a
summary like the modes under `/capabilities`. orca offers them in the `/` menu and in `/help`; a
message beginning `/name` for one of them is sent as written, and the backend reads the skill's
instructions as the request. Per folder, so here rather than under `/capabilities`.

### `POST /workspaces`

```json
{"name": "myrepo", "root_path": "/Users/me/code/myrepo", "vcs": "git", "replace_existing": false}
```

`vcs` is `git` or `none`, declared by orca from what it found on disk, never detected by the
backend. `409` means another client registered the same root between the list and the create, and
orca re-reads rather than failing.

`repo_identity`, when the backend records one, is the checkout's root-commit set. orca recomputes
it and sends `replace_existing: true` when it has drifted — a plain folder became a checkout, or
one checkout replaced another. Paths are durable; the thing at a path is not.

---

## Threads and runs

A **thread** is the conversation. A **run** is one turn: durable, resumable, and able to outlive
the client that started it.

### `POST /threads` → `{"thread_id"}`

Body is `{"workspace_id", "title"}`. The title is the first 120 characters of the person's
message, which is a reasonable default and not a decision the backend has to respect.

### `GET /threads?workspace_id=&limit=`

`{"threads": [{"thread_id", "title", "latest_run_status", "updated_at", "parent", "folder",
"root_path"}, …]}`, most recent first. Only `thread_id` is required; the rest sharpen the picker.
`parent` is the thread that delegated this one, and orca nests a child under its parent rather
than listing it as a question nobody asked. `folder` is the working folder's name and `root_path`
its path.

### `GET /threads/{thread_id}` → `{"thread_id", "workspace_id", "title"}`

orca refuses to continue a thread whose `workspace_id` is not the bound one.

### `POST /threads/{thread_id}/runs`

```json
{
  "workspace_id": "ws_01J…",
  "message": {"content": "Add retry handling to the websocket client."},
  "mode": "normal",
  "approval_policy": "ask"
}
```

Return **202** immediately with `{"run_id", "thread_id"}`. Never hold the connection for the work.

`mode` and `approval_policy` are your vocabularies, not orca's. It passes through whatever `/mode`
and `/permissions` were set to and defaults to `normal` and `ask`. List the values you accept under
`/capabilities` and orca will offer them; otherwise it takes anything.

The run works in the workspace's folder and nowhere else. orca used to send a `client_context`
naming the subfolder the person launched from; no backend honoured it, and showing that subfolder
in the header named a folder the run never worked in, so both went (2026-09-03). A run that needs
to reach more than one folder is widened, below.

### `POST /threads/{thread_id}/folders`

```json
{"path": "/Users/me/code/shared-lib"}
```

Let the conversation reach one more folder, now and on every later run: **`{"folders": [...]}`**,
absolute, the working folder first. The working folder does not change — relative paths still
resolve against it and commands still run in it — and the added folder is reachable by absolute
path. A path that is not a directory is a `400` with a sentence. With a run in flight, tell the
agent and publish `folder.added`; between runs, record it so the next run reaches it.

orca sends this for `/add <path>`. A person may widen before the first message, so orca creates
the thread first when there is none and keeps using the id it was given.

### `GET /runs?thread_id=&limit=`

`{"runs": [{"run_id", "status"}, …]}`, newest first. orca reverses it to replay a thread oldest
first, and reads `status` to know whether a replayed run is still going.

This is how a conversation is resumed, so it has to answer after a restart. The harness keeps the
transcript as the durable record and rebuilds a thread's finished runs from it when the thread is
opened — the same run ids, and the same events a client would have seen live, derived rather than
stored twice. Live states do not replay: a reopened thread never shows a stale approval, question
or pause, and a run whose transcript ends without an answer replays as `failed`.

`status` is `queued`, `running`, `awaiting_approval`, `awaiting_input`, `paused`, `blocked`,
`completed`, `failed` or `cancelled`. The last four are terminal. A value orca does not recognise
leaves the run shown as still going, which is the safe direction to be wrong in: a run reported as
finished when it is not is a person walking away from live work.

---

## Events (SSE)

### `GET /runs/{run_id}/events`

```
Accept: text/event-stream
?after_seq=<int>       resume from a cursor
?visibility=user|all   `all` adds developer events
?ticks=<int>           return a bounded read instead of following
```

Each frame's `id:` is the row's `seq`, and `data:` is:

```json
{"event_id": "evt_01J…", "seq": 42, "type": "run.progress",
 "visibility": "user", "payload": {…}}
```

**The one hard obligation: `?after_seq` is exact.** The same cursor always yields the same suffix
of the log. Sequences start at 1, never repeat, and arrive in order. orca reconnects on any
transport failure — with capped backoff, and it gives up after about 23 seconds of a connection
that delivers nothing — and correctness after every reconnect rests entirely on this.

Send an SSE comment (`:` and no `data:`) every 15 seconds or so. An idle connection dies silently
otherwise, and a decoder that treats a comment as a malformed event is the first thing a
hand-written client gets wrong.

`?ticks=` bounds a read so it returns for a live run: orca uses it to replay thread history
without opening a second live cursor.

### `stream.end`

The only frame orca identifies by its SSE **`event:` name** rather than by a `type` inside
`data`, because it is transport and not a row of the log:

```
event: stream.end
data: {"reason": "terminal"}
```

`reason` sits at the top level of `data`, not under `payload`. A frame that carries
`{"type": "stream.end"}` in its `data` and no `event:` line is read as an ordinary event of an
unknown kind, so the follow never learns the run is over — orca reconnects from its cursor,
receives the same unrecognised frame, and loops silently and forever. Write the `event:` line.

**Close the connection immediately after it.** orca reads the response to its natural end
rather than breaking out of the stream — abandoning an async generator suspended inside a
streaming context manager needs an `await` that generator finalization is not allowed to
perform — so `stream.end` tells it *what happened* and EOF is what actually returns control.
A server that sends `stream.end` and then holds the socket open on keep-alive hangs orca for
as long as it holds it. There is no read timeout to rescue it: a run is allowed to think for
an hour, so an idle stream is never treated as a failure.

The reason:

| `reason` | Means |
|---|---|
| `terminal` | The run ended. The normal case. |
| `terminal_without_event` | The run ended without writing a terminal event. A defect, reported rather than hidden. |
| `tick_limit` | The bounded read finished. |
| anything else | **The run may still be going.** Reconnect from `after_seq`. |

An unrecognised reason is treated as *still going*, for the same reason an unrecognised status is.
A follow that stopped on every reason returned silently while the run it was watching carried on,
which is the one failure this rule exists to prevent.

### The events orca renders

| Type | Payload read | What orca does |
|---|---|---|
| `run.created` | `message`, `mode`, `approval_policy` | Opens the turn with the person's request. |
| `run.progress` | `update_id`, `text`, `status`, `arguments`, `tool`, `kind`, `agent_id` | One activity row, upserted by `update_id`. With `agent_id`, the row is a delegated agent's and is shown under that agent rather than in the turn. `status` is `active`, `completed` or `failed`. `arguments` are the call's own, on every event for the row; a `path` with `content`, or with `old` and `new`, is shown as the code under the row. `kind` picks the row's glyph: `read`, `search`, `edit`, `execute`, `fetch`, `think`, `switch_mode`, `skill`; anything else gets a plain mark. |
| `answer.delta` | `effect_id`, `model_call_id`, `text` | Streams the answer. The pair identifies one attempt; a delta from a different pair discards what was streamed and starts again. Both are opaque — repeat one value in both if you have a single attempt identity. |
| `plan.progress` | `explanation`, `plan[]` of `{step, status}` | A checklist above the activity rows. Each event carries the **whole** list and replaces the previous one. Nothing counts the steps. |
| `plan.available` | `artifact_id`, `path` | Offers an artifact in the conversation and the review view. |
| `approval.requested` | `approval_id`, `title`, `summary`, `risk`, `arguments`, `allowed_decisions`, `grant` | Inline at the end of the transcript with its choices, and the run parks. `1`, `y` or Enter approve; `2` allows always when offered; `3`, `n` or Esc reject. `title` is what a person judges. `grant` is what "always" would cover, in words -- `git commands`, `file writes` -- and orca puts it on that choice. From `arguments`: `argv` is shown as a shell-quoted command line; `path` with `content` is shown as the file, highlighted by its extension; `path` with `old` and `new` is shown as a diff. |
| `approval.resolved` | `decision` | The prompt becomes its answer, kept in the transcript where it was decided. A run paused under the modal stays paused: send `run.paused` again after this, since orca reads a resolution as running. |
| `question.requested` | `question_id`, `prompt`, `options[]` | Inline above the composer; the next thing typed is the answer. `options` are the agent's guesses, shown numbered — a number picks one, anything else is sent as typed, and an empty answer is sent as "I am not answering". |
| `question.resolved` | — | Dismisses it. |
| `context.compacted` | `summary` | A note on the turn: the agent now works from a summary. User-visible on purpose; it is the honest explanation for a change in behaviour. |
| `run.steered` | `content` | A note on the turn with the instruction the person sent. |
| `context.usage` | `tokens`, `context_window`, `estimated` | How full the context is after the last model call, in the footer. `estimated` says the backend counted characters rather than being told. Live only; not replayed. |
| `folder.added` | `path` | A note on the turn, and the folder joins the header. Absolute. |
| `agent.started` | `agent_id`, `task` | A delegated agent began. A note on the turn, and a row in the agents strip while it runs. |
| `agent.said` | `agent_id`, `text`, `report` | What a delegated agent said as it went -- its narration, or a report when `report` is true. Kept with the agent, never shown as the parent's answer. |
| `agent.finished` | `agent_id`, `turns`, `stop`, `answer`, `seconds` | The agent's answer, as a note on the turn and in `/agents`. |
| `agent.failed` / `agent.stopped` | `agent_id`, `error` | The agent ended without an answer. A note on the turn. |
| `run.paused` | — | Not terminal. |
| `run.resumed` | — | Not terminal. Undoes `run.paused`; without it a paused run reads as paused until it ends. |
| `run.completed` | `summary` | Terminal. Exactly one, nothing after it. `summary` replaces the streamed answer; when it *is* the streamed answer, the turn keeps its shape. |
| `run.failed` / `.cancelled` / `.blocked` | `summary` | Terminal. `summary` is why the run stopped, shown where it stopped; what the model said before it stays. |

Anything else advances the cursor and is otherwise ignored — which is what makes adding an event
type safe rather than merely permitted.

Events with `"visibility": "developer"` arrive only under `?visibility=all`, are routed to
`/inspect` on their own cursor, and never reach the conversation. A backend with nothing private
to show emits none.

---

## Commands

### `POST /runs/{run_id}/commands`

```json
{"command_id": "cmd_01J…", "type": "cancel"}
```

`command_id` is minted by orca and is the idempotency key. The six types it sends:

| `type` | Extra fields |
|---|---|
| `pause` / `resume` / `cancel` | — |
| `steer` | `content` — a further instruction for a run already going |
| `answer` | `question_id`, `content` |
| `resolve_approval` | `approval_id`, `decision` |

`decision` is one of the values that request's `allowed_decisions` offered. orca binds `1` to
`approve` and `3` to `reject`, and offers a second affirmative on `2` when the request lists
`approve_bash_always` — the one decision name orca knows by sight, because a persistent grant
needs its own key. A backend with a different vocabulary still gets `1` and `3`.

Any response is accepted; a `status` key is shown as a one-line notice. Refuse a command you
cannot honour rather than accepting it silently — the person is watching for the run to change.

---

## What a backend must guarantee

- **`?after_seq` is exact.** Everything else is a convenience; this one is load-bearing.
- **`stream.end` carries an `event:` line, and the connection closes right after it.** Those two
  together are what end a follow; either one missing is a silent hang rather than an error.
- **Exactly one terminal event per run**, and nothing after it. `run.paused` and the waiting
  states are not endings.
- **Runs survive disconnection.** Closing the terminal is not cancelling.
- **A run works in the folder it was given.** orca shows that path in the header, and a run that
  quietly works somewhere else makes the client lie on its behalf.
