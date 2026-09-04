# Terminal client

`orca` is a focused client with one persistent terminal shell. It keeps the conversation primary
and makes durable run state legible without burying it.

## Interface

- `orca` or `orca chat` opens the full-screen application.
- `/review` shows the canonical result and published artifacts.
- `/threads` selects a durable conversation to continue.
- `/inspect` exposes developer-visible events without mixing them into the conversation.
- Ctrl+P and `/help` show the command catalogue. The same catalogue drives parsing, help, and the
  command palette.
- `orca run "…"` is the non-interactive path. Plain output is stable for people; `--jsonl` emits a
  versioned machine-readable stream; `--no-follow` prints the accepted run id and detaches.

Approval requests are asked inline at the end of the transcript, with explicit one-shot,
persistent-when-offered, and reject choices; once decided, the prompt leaves the transcript and
the decision is shown once as a notice by the composer. Questions stay inline above the composer.
Plain mode exits with status 2 after reporting either kind of input request so an unattended
process never appears to hang, 1 when the run failed, and 3 when it was cancelled or blocked.

## Architecture

```text
keyboard / backend event
          │
          ▼
 typed action ──▶ pure reducer ──▶ immutable AppState
                       │                    │
                       ▼                    ▼
                  I/O effect          pure Rich renderer
                       │                    │
                       ▼                    ▼
                terminal backend       Textual view
```

The application core in `app/` imports neither Textual nor a transport library. Views render state
and emit typed actions; they never start background work. The Textual host owns effects and the
single event cursor. `TerminalBackend` is the only way out, and `HttpBackend` — the bundled
implementation of `docs/backend-contract.md` — normalizes wire events into client-owned values
without importing any backend's domain models.

`tests/test_architecture.py` enforces all four boundaries, including the one that matters most for
a client meant to outlive any particular harness: only the composition root may name a backend
implementation.

Local process lifecycle and workspace discovery are host adapters behind the backend port. They
may inspect the launch machine, but they do not execute or reconstruct a harness's work. Run
creation, commands, approvals, questions, and artifacts remain backend-owned.

## Three views, and the one that left

The center surface swaps between the conversation, the review, and the developer inspector.

There used to be a fourth. `/agents` rendered a work map: a live topology of work units with
dependencies, per-unit status, agent aliases, and tool counts, laid out in three responsive
columns. It came out with the extraction (2026-08-30), because everything it drew came from one
harness's decomposition of a request into coordinator-scheduled units, and `load_work_graph` — the
port method that fetched the topology — was a question no general backend can answer.

Nothing replaced it, and that leaves a real hole. A run that does several things still narrates
through `run.progress`, and the model's own `plan.progress` checklist still renders inline, but
there is nowhere to see the shape of the work or which part is executing. A backend with parallel
structure worth showing has no way to show it. Filling that with a generic plan or todo surface
would be a design decision rather than an extraction, so it is left open and stated.
