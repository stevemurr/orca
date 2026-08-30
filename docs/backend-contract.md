# The HTTP backend contract

What a backend must serve for `orca.http_backend.HttpBackend` to drive the whole terminal
client. Base path `/api/v1`.

This is a client-side document. It describes the endpoints orca calls and the events orca reads,
and nothing else: a harness is free to serve fifty more endpoints, and orca will never know. The
converse also holds — if orca cannot do something without knowing how your harness is built,
that is a gap here rather than a reason to reach past it.

Speaking HTTP is optional. `orca.backend.TerminalBackend` is the actual boundary, and a Python
harness implements those eight methods directly. This document exists because most harnesses
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
{"protocol_version": "1"}
```

orca reads `protocol_version` and nothing else. Anything else you advertise is ignored: there is
no feature flag orca gates a control on, because a feature map is a promise about *your*
architecture and the client has no view that could honour one.

### `GET /health`

Only used by local process management, and only then.

```json
{"status": "ok", "detail": {"managed_instance_id": "<opaque>"}}
```

`detail.managed_instance_id` must echo the `ORCA_MANAGED_INSTANCE_ID` environment variable orca
set when it started the process, and be empty otherwise. It is an ownership marker, not
authority: orca will not signal a process that cannot prove it is the one orca started. A backend
that is never process-managed does not need this route.

---

## Workspaces

A workspace is one registered folder the backend may work in. orca discovers the folder locally —
it is the only side that can see where the person was standing — and asks the backend to bind it.

### `GET /workspaces`

`[{"workspace_id", "name", "root_path", "vcs", "repo_identity"}, …]`. `root_path` is matched
against the resolved local folder **exactly**; a registered ancestor is deliberately ignored,
because showing the launch directory while submitting a run against a broader folder elsewhere is
the most dangerous lie a client can tell.

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

`{"threads": [{"thread_id", "title", "latest_run_status", "updated_at"}, …]}`, most recent first.
Only `thread_id` is required; the rest sharpen the picker.

### `GET /threads/{thread_id}` → `{"thread_id", "workspace_id", "title"}`

orca refuses to continue a thread whose `workspace_id` is not the bound one.

### `POST /threads/{thread_id}/runs`

```json
{
  "workspace_id": "ws_01J…",
  "message": {"content": "Add retry handling to the websocket client."},
  "mode": "auto",
  "approval_policy": "safe",
  "client_context": {"cwd_relative": "Sources/App"}
}
```

Return **202** immediately with `{"run_id", "thread_id"}`. Never hold the connection for the work.

`mode` and `approval_policy` are your vocabularies, not orca's. It passes through whatever `/mode`
and `/permissions` were set to and defaults to `auto` and `safe`. `client_context.cwd_relative` is
where inside the folder the person is standing — relative, no `..` — and is advisory.

`workspace_id` is omitted rather than sent as null when there is none, which is how orca works in
a directory that is not a registered project.

### `GET /runs?thread_id=&limit=`

`{"runs": [{"run_id", "status"}, …]}`, newest first. orca reverses it to replay a thread oldest
first, and reads `status` to know whether a replayed run is still going.

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

A frame whose payload is the `data` object itself — `reason` at the top level, not under
`payload`. It is transport, not an ending:

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
| `run.progress` | `update_id`, `text`, `status` | One activity row, upserted by `update_id`. `status` is `active`, `completed` or `failed`. |
| `answer.delta` | `effect_id`, `model_call_id`, `text` | Streams the answer. The pair identifies one attempt; a delta from a different pair discards what was streamed and starts again. Both are opaque — repeat one value in both if you have a single attempt identity. |
| `plan.progress` | `explanation`, `plan[]` of `{step, status}` | A checklist above the activity rows. Each event carries the **whole** list and replaces the previous one. Nothing counts the steps. |
| `plan.available` | `artifact_id`, `path` | Offers an artifact in the conversation and the review view. |
| `approval.requested` | `approval_id`, `title`, `summary`, `risk`, `arguments.argv`, `allowed_decisions` | Modal, and the run parks. `title` is what a person judges; `arguments.argv` is shown as a shell-quoted command line. |
| `approval.resolved` | — | Dismisses it. |
| `question.requested` | `question_id`, `prompt` | Inline above the composer; the next thing typed is the answer. |
| `question.resolved` | — | Dismisses it. |
| `run.paused` | — | Not terminal. |
| `run.completed` / `.failed` / `.cancelled` / `.blocked` | `summary` | Terminal. Exactly one, nothing after it. `summary` replaces the streamed answer. |

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
- **Exactly one terminal event per run**, and nothing after it. `run.paused` and the waiting
  states are not endings.
- **Runs survive disconnection.** Closing the terminal is not cancelling.
- **A run works in the folder it was given.** orca shows that path in the header, and a run that
  quietly works somewhere else makes the client lie on its behalf.
