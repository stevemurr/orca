"""Task API adapter tests for the greenfield terminal runtime."""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from orca.backend import BackendError, RunRequest
from orca.client import SSEEvent
from orca.connection import Connection, CredentialSource
from orca.http_backend import HttpBackend, normalize_event
from orca.workspace_context import WorkspaceBinding


class FakeClient:
    def __init__(self) -> None:
        self.created_threads: list[dict[str, object]] = []
        self.created_runs: list[dict[str, object]] = []
        self.commands: list[dict[str, object]] = []
        self.closed = False

    async def capabilities(self):  # type: ignore[no-untyped-def]
        return {
            "protocol_version": "1.6",
            "features": {"approvals": True, "questions": True, "skills": False},
        }

    async def create_thread(self, workspace_id=None, title=""):  # type: ignore[no-untyped-def]
        self.created_threads.append({"workspace_id": workspace_id, "title": title})
        return {"thread_id": "thread-1"}

    async def create_run(self, thread_id, workspace_id, message, **kwargs):  # type: ignore[no-untyped-def]
        self.created_runs.append(
            {
                "thread_id": thread_id,
                "workspace_id": workspace_id,
                "message": message,
                **kwargs,
            }
        )
        return {"run_id": "run-1", "thread_id": thread_id}

    async def stream_events(self, run_id, *, after_seq=0, visibility="user"):  # type: ignore[no-untyped-def]
        del run_id, after_seq, visibility
        yield SSEEvent(
            "1",
            "run.created",
            {
                "seq": 1,
                "event_id": "evt-1",
                "visibility": "user",
                "payload": {"message": "hello"},
            },
        )
        yield SSEEvent("1", "stream.end", {"reason": "terminal", "final_seq": 1})

    async def send_command(self, run_id, command):  # type: ignore[no-untyped-def]
        self.commands.append({"run_id": run_id, **command})
        return {"status": "accepted"}

    async def list_artifacts(self, run_id):  # type: ignore[no-untyped-def]
        assert run_id == "run-1"
        return [
            {"artifact_id": "plan-1", "type": "plan", "version": 1},
            {"artifact_id": "graph-1", "type": "work_graph", "version": 1},
        ]

    async def download_artifact(self, artifact_id):  # type: ignore[no-untyped-def]
        assert artifact_id == "graph-1"
        return orjson.dumps(
            {
                "schema": "control-artifact.v1",
                "kind": "work_graph",
                "payload": {
                    "graph_fingerprint": "fingerprint-1",
                    "units": [
                        {
                            "id": "contract",
                            "kind": "foundation",
                            "objective": "Map the contract",
                            "depends_on": [],
                        },
                        {
                            "id": "shell",
                            "kind": "feature",
                            "objective": "Build the shell",
                            "depends_on": ["contract"],
                        },
                    ],
                },
            }
        )

    async def list_threads(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return {"threads": [{"thread_id": "thread-1", "title": "hello"}]}

    async def get_thread(self, thread_id):  # type: ignore[no-untyped-def]
        return {
            "thread_id": thread_id,
            "workspace_id": "ws-1",
            "title": "hello",
        }

    async def list_runs(self, **kwargs):  # type: ignore[no-untyped-def]
        assert kwargs == {"thread_id": "thread-1", "limit": 50}
        return {
            "runs": [
                {"run_id": "run-2", "status": "running"},
                {"run_id": "run-1", "status": "completed"},
            ]
        }

    async def read_events(
        self,
        run_id,
        *,
        visibility="all",
        ticks=1,  # type: ignore[no-untyped-def]
    ):
        assert visibility == "user"
        assert ticks == 1
        terminal = (
            [
                SSEEvent(
                    "2",
                    "run.completed",
                    {
                        "seq": 2,
                        "event_id": f"evt-{run_id}-2",
                        "payload": {"summary": "Done."},
                    },
                )
            ]
            if run_id == "run-1"
            else []
        )
        return [
            SSEEvent(
                "1",
                "run.created",
                {
                    "seq": 1,
                    "event_id": f"evt-{run_id}-1",
                    "payload": {"message": run_id},
                },
            ),
            *terminal,
            SSEEvent("2", "stream.end", {"reason": "tick_limit"}),
        ]

    async def aclose(self) -> None:
        self.closed = True


class BareGraphClient(FakeClient):
    async def download_artifact(self, artifact_id):  # type: ignore[no-untyped-def]
        assert artifact_id == "graph-1"
        return orjson.dumps({"graph_fingerprint": "test-only", "units": []})


def binding() -> WorkspaceBinding:
    root = Path("/tmp/project")
    return WorkspaceBinding("ws-1", "project", root, root, ".", "none")


async def no_server(_connection):  # type: ignore[no-untyped-def]
    return None


async def fixed_workspace(_client, *, selector="", path=None):  # type: ignore[no-untyped-def]
    del selector, path
    return binding()


def connection() -> Connection:
    return Connection(
        "local",
        "http://127.0.0.1:8420",
        "",
        CredentialSource.NONE,
    )


def test_wire_event_is_normalized_without_server_model_imports() -> None:
    normalized = normalize_event(
        SSEEvent(
            "42",
            "run.progress",
            {
                "seq": 42,
                "event_id": "evt-42",
                "visibility": "user",
                "payload": {"update_id": "opening", "text": "Working."},
            },
        )
    )

    assert normalized is not None
    assert normalized.sequence == 42
    assert normalized.kind == "run.progress"
    assert normalized.payload["text"] == "Working."
    assert normalize_event(SSEEvent("42", "stream.end", {"reason": "terminal"})) is None


async def test_backend_boot_submit_stream_command_and_graph_are_contract_only() -> None:
    client = FakeClient()
    backend = HttpBackend(
        connection(),
        client=client,  # type: ignore[arg-type]
        server_ensurer=no_server,
        workspace_resolver=fixed_workspace,
    )

    boot = await backend.boot()
    run = await backend.start_run(
        RunRequest("Build it", None, boot.workspace_id, boot.cwd_relative, "auto", "safe")
    )
    streamed = [item async for item in backend.stream(run.run_id, after_seq=0, developer=False)]
    command = await backend.send_command(run.run_id, "pause", {})
    graph = await backend.load_work_graph(run.run_id)
    threads = await backend.recent_threads()
    history = await backend.load_thread("thread-1")
    await backend.close()

    assert boot.workspace_path == "/tmp/project"
    assert boot.capabilities == frozenset({"approvals", "questions"})
    assert run.thread_id == "thread-1"
    assert client.created_runs[0]["client_context"] == {"cwd_relative": "."}
    assert str(client.created_runs[0]["idempotency_key"]).startswith("idem_")
    assert [item.kind for item in streamed] == ["run.created"]
    assert command == {"status": "accepted"}
    assert str(client.commands[0]["command_id"]).startswith("cmd_")
    assert [unit.unit_id for unit in graph.units] == ["contract", "shell"]
    assert graph.units[1].depends_on == ("contract",)
    assert threads[0]["thread_id"] == "thread-1"
    assert [run.run_id for run in history.runs] == ["run-1", "run-2"]
    assert [event.kind for event in history.runs[0].events] == [
        "run.created",
        "run.completed",
    ]
    assert client.closed


async def test_work_graph_requires_the_public_control_artifact_envelope() -> None:
    backend = HttpBackend(
        connection(),
        client=BareGraphClient(),  # type: ignore[arg-type]
        server_ensurer=no_server,
        workspace_resolver=fixed_workspace,
    )
    await backend.boot()

    with pytest.raises(BackendError, match="malformed"):
        await backend.load_work_graph("run-1")
