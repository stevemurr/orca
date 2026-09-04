"""Adapter tests for the bundled HTTP implementation of the backend port."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import override

import httpx
import pytest

from orca.app.model import Choice
from orca.backend import BackendError, CommandOutcome, Pause, RunRequest
from orca.client import ApiError, HttpApiClient, SSEEvent
from orca.connection import Connection, CredentialSource
from orca.http_backend import HttpBackend, normalize_event
from orca.json_types import JsonObject
from orca.workspace_context import WorkspaceBinding


class FakeClient:
    def __init__(self) -> None:
        self.created_threads: list[dict[str, object]] = []
        self.created_runs: list[dict[str, object]] = []
        self.commands: list[dict[str, object]] = []
        self.folders: list[tuple[str, str]] = []
        self.closed: bool = False

    async def capabilities(self) -> JsonObject:
        return {
            "protocol_version": "1.6",
            # A bare name, a name with a summary, and two things that are neither.
            "modes": ["normal", {"name": "plan", "summary": "Read only."}],
            "approval_policies": [{"name": "ask"}, "edits", 7, {"summary": "no name"}],
        }

    async def create_thread(self, workspace_id: str | None = None, title: str = "") -> JsonObject:
        self.created_threads.append({"workspace_id": workspace_id, "title": title})
        return {"thread_id": "thread-1"}

    async def create_run(
        self,
        thread_id: str,
        workspace_id: str | None,
        message: str,
        *,
        mode: str | None = None,
        approval_policy: str | None = None,
        client_context: JsonObject | None = None,
        idempotency_key: str | None = None,
    ) -> JsonObject:
        self.created_runs.append(
            {
                "thread_id": thread_id,
                "workspace_id": workspace_id,
                "message": message,
                "mode": mode,
                "approval_policy": approval_policy,
                "client_context": client_context,
                "idempotency_key": idempotency_key,
            }
        )
        return {"run_id": "run-1", "thread_id": thread_id}

    async def stream_events(
        self, run_id: str, *, after_seq: int = 0, visibility: str = "user"
    ) -> AsyncGenerator[SSEEvent, None]:
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

    async def send_command(self, run_id: str, command: JsonObject) -> JsonObject:
        self.commands.append({"run_id": run_id, **command})
        return {"status": "accepted"}

    async def add_folder(self, thread_id: str, path: str) -> JsonObject:
        self.folders.append((thread_id, path))
        return {"folders": ["/tmp/project", path]}

    async def list_threads(self, **kwargs: str | int | None) -> JsonObject:
        del kwargs
        return {"threads": [{"thread_id": "thread-1", "title": "hello"}]}

    async def get_thread(self, thread_id: str) -> JsonObject:
        return {
            "thread_id": thread_id,
            "workspace_id": "ws-1",
            "title": "hello",
        }

    async def list_runs(self, **kwargs: str | int | None) -> JsonObject:
        assert kwargs == {"thread_id": "thread-1", "limit": 50}
        return {
            "runs": [
                {"run_id": "run-2", "status": "running"},
                {"run_id": "run-1", "status": "completed"},
            ]
        }

    async def read_events(
        self, run_id: str, *, visibility: str = "all", ticks: int = 1
    ) -> list[SSEEvent]:
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


def binding() -> WorkspaceBinding:
    root = Path("/tmp/project")
    return WorkspaceBinding("ws-1", "project", root, "none")


async def no_server(_connection: Connection) -> None:
    return None


async def fixed_workspace(
    _client: object, *, selector: str = "", path: Path | None = None
) -> WorkspaceBinding:
    del selector, path
    return binding()


def connection() -> Connection:
    return Connection(
        "local",
        "http://127.0.0.1:8420",
        "",
        CredentialSource.NONE,
    )


def test_wire_event_is_normalized_without_importing_a_backend_event_model() -> None:
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


async def test_boot_submit_stream_command_and_history_are_contract_only() -> None:
    client = FakeClient()
    backend = HttpBackend(
        connection(),
        client=client,
        server_ensurer=no_server,
        workspace_resolver=fixed_workspace,
    )

    session = await backend.connect()
    run = await backend.start_run(
        RunRequest("Build it", None, session.workspace_id, "normal", "ask")
    )
    streamed = [item async for item in backend.stream(run.run_id, after_seq=0, developer=False)]
    command = await backend.send_command(run.run_id, Pause())
    threads = await backend.recent_threads()
    history = await backend.load_thread("thread-1")
    await backend.close()

    assert session.workspace_path == "/tmp/project"
    assert session.protocol_version == "1.6"
    assert session.modes == (Choice("normal"), Choice("plan", "Read only."))
    assert session.policies == (Choice("ask"), Choice("edits"))
    assert run.thread_id == "thread-1"
    assert client.created_runs[0]["client_context"] is None
    assert str(client.created_runs[0]["idempotency_key"]).startswith("idem_")
    assert [item.kind for item in streamed] == ["run.created"]
    assert command == CommandOutcome("accepted")
    assert str(client.commands[0]["command_id"]).startswith("cmd_")
    assert threads[0].thread_id == "thread-1"
    assert [run.run_id for run in history.runs] == ["run-1", "run-2"]
    assert [event.kind for event in history.runs[0].events] == [
        "run.created",
        "run.completed",
    ]
    assert client.closed


async def test_a_server_that_is_not_a_harness_says_so() -> None:
    """Reported from a real setup: a profile pointed at an OpenAI-compatible model gateway --
    a real server, answering, on the port the person had in mind -- and the failure they saw
    was about a keyring credential. A 404 on /capabilities means something is listening and
    it is not a backend, and saying that points at the actual mistake. (2026-08-31)
    """

    class NotAHarness(FakeClient):
        @override
        async def capabilities(self) -> JsonObject:
            raise ApiError(404, "not_found", "Not Found")

    backend = HttpBackend(
        connection(),
        client=NotAHarness(),
        server_ensurer=no_server,
        workspace_resolver=fixed_workspace,
    )

    with pytest.raises(BackendError, match="not a harness"):
        await backend.connect()


async def test_widening_before_the_first_message_makes_the_thread() -> None:
    """The backend widens a thread, so a folder added before anything was said needs one;
    the id comes back so the first message continues it rather than starting another."""
    client = FakeClient()
    backend = HttpBackend(
        connection(),
        client=client,
        server_ensurer=no_server,
        workspace_resolver=fixed_workspace,
    )
    _ = await backend.connect()

    first = await backend.add_folder(None, "/srv/lib")
    again = await backend.add_folder(first.thread_id, "/srv/other")

    assert first.thread_id == "thread-1"
    assert first.folders == ("/tmp/project", "/srv/lib")
    assert again.thread_id == "thread-1"
    assert client.created_threads == [{"workspace_id": "ws-1", "title": ""}]
    assert client.folders == [("thread-1", "/srv/lib"), ("thread-1", "/srv/other")]


def _over_the_wire(handler: Callable[[httpx.Request], httpx.Response]) -> HttpBackend:
    """A backend on a real `HttpApiClient` whose transport is canned, for failures that only
    exist below the `FakeClient` seam: the wire, and the framing."""
    client = HttpApiClient("http://backend.test")
    client._client = httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(handler)
    )
    return HttpBackend(
        connection(),
        client=client,
        server_ensurer=no_server,
        workspace_resolver=fixed_workspace,
    )


async def test_a_history_load_against_a_dead_port_is_a_backend_error() -> None:
    """`load_thread` caught `ApiError` and nothing else, while the event read underneath
    it raised raw httpx exceptions for a connection failure. (found 2026-09-04)"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/events"):
            raise httpx.ConnectError("refused", request=request)
        if path.endswith("/capabilities"):
            return httpx.Response(200, json={"protocol_version": "1.6"})
        if "/threads/" in path:
            return httpx.Response(200, json={"thread_id": "t1", "workspace_id": "ws-1"})
        if path.endswith("/runs"):
            return httpx.Response(200, json={"runs": [{"run_id": "r1", "status": "completed"}]})
        return httpx.Response(404, json={"detail": "Not Found"})

    backend = _over_the_wire(handler)
    _ = await backend.connect()

    with pytest.raises(BackendError, match="server_unreachable"):
        _ = await backend.load_thread("t1")
    await backend.close()


async def test_a_stream_frame_that_is_not_json_is_a_backend_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"protocol_version": "1.6"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"id: 1\nevent: run.progress\ndata: {not json\n\n",
        )

    backend = _over_the_wire(handler)
    _ = await backend.connect()

    with pytest.raises(BackendError, match="malformed_frame"):
        async for _ in backend.stream("r1", after_seq=0, developer=False):
            pass
    await backend.close()


async def test_a_run_reply_without_an_id_is_a_malformed_response() -> None:
    """`created["run_id"]` sat on the return line, outside the boundary that wrapped the
    thread id, so an empty reply leaked `KeyError('run_id')`. (found 2026-09-04)"""

    class EmptyReplies(FakeClient):
        @override
        async def create_thread(
            self, workspace_id: str | None = None, title: str = ""
        ) -> JsonObject:
            del workspace_id, title
            return {}

        @override
        async def create_run(
            self,
            thread_id: str,
            workspace_id: str | None,
            message: str,
            *,
            mode: str | None = None,
            approval_policy: str | None = None,
            client_context: JsonObject | None = None,
            idempotency_key: str | None = None,
        ) -> JsonObject:
            del thread_id, workspace_id, message, mode, approval_policy
            del client_context, idempotency_key
            return {}

    backend = HttpBackend(
        connection(),
        client=EmptyReplies(),
        server_ensurer=no_server,
        workspace_resolver=fixed_workspace,
    )
    _ = await backend.connect()

    with pytest.raises(BackendError, match="malformed_response.*thread_id"):
        _ = await backend.start_run(RunRequest("Build it", None, "ws-1", "normal", "ask"))
    with pytest.raises(BackendError, match="malformed_response.*run_id"):
        _ = await backend.start_run(RunRequest("Build it", "thread-1", "ws-1", "normal", "ask"))
