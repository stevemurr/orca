"""The bundled HTTP implementation of the terminal application's backend port.

One implementation of `orca.backend.TerminalBackend`, speaking the wire contract in
`docs/backend-contract.md`. It is the reference, not the definition: a harness that cannot
serve that contract implements `TerminalBackend` directly instead.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Protocol, assert_never

from orca.app.model import TaskEvent, ThreadReplay
from orca.backend import (
    Answer,
    BackendError,
    Cancel,
    Command,
    CommandOutcome,
    Pause,
    ResolveApproval,
    Resume,
    RunInfo,
    RunRequest,
    SessionInfo,
    Steer,
    ThreadFolders,
    ThreadHistoryInfo,
    ThreadSummary,
)
from orca.client import ApiError, HttpApiClient, SSEEvent
from orca.connection import Connection
from orca.json_types import JsonObject, JsonValue
from orca.server_manager import ManagedServerError, ensure_local_server
from orca.workspace_context import (
    WorkspaceBinding,
    WorkspaceContextError,
    resolve_workspace_binding,
)


class _BackendHttpClient(Protocol):
    async def capabilities(self) -> JsonObject: ...

    async def create_thread(
        self, workspace_id: str | None = None, title: str = ""
    ) -> JsonObject: ...

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
    ) -> JsonObject: ...

    def stream_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        visibility: str = "user",
    ) -> AsyncGenerator[SSEEvent, None]: ...

    async def send_command(self, run_id: str, command: JsonObject) -> JsonObject: ...

    async def add_folder(self, thread_id: str, path: str) -> JsonObject: ...

    async def list_threads(self, **params: str | int | None) -> JsonObject: ...

    async def get_thread(self, thread_id: str) -> JsonObject: ...

    async def list_runs(self, **params: str | int | None) -> JsonObject: ...

    async def read_events(
        self,
        run_id: str,
        *,
        visibility: str = "all",
        ticks: int = 1,
    ) -> list[SSEEvent]: ...

    async def aclose(self) -> None: ...


ServerEnsurer = Callable[[Connection], Awaitable[object]]
WorkspaceResolver = Callable[..., Awaitable[WorkspaceBinding]]


def _wire(command: Command) -> dict[str, str]:
    """One command as the HTTP contract's body.

    The wire vocabulary is unchanged by typing the Python side -- `type` is still the same
    six strings a backend already matches on -- so this is a translation, not a protocol
    change. Exhaustive by construction: a new member of the union that is not handled here
    is a type error at `assert_never` rather than a body sent with no `type`.
    """
    match command:
        case Pause():
            return {"type": "pause"}
        case Resume():
            return {"type": "resume"}
        case Cancel():
            return {"type": "cancel"}
        case Steer(content=content):
            return {"type": "steer", "content": content}
        case Answer(question_id=question_id, content=content):
            return {"type": "answer", "question_id": question_id, "content": content}
        case ResolveApproval(approval_id=approval_id, decision=decision):
            return {
                "type": "resolve_approval",
                "approval_id": approval_id,
                "decision": decision,
            }
    assert_never(command)


class HttpBackend:
    """Translate application effects into the contract's public HTTP operations."""

    def __init__(
        self,
        connection: Connection,
        *,
        workspace_selector: str = "",
        launch_path: Path | None = None,
        client: _BackendHttpClient | None = None,
        server_ensurer: ServerEnsurer = ensure_local_server,
        workspace_resolver: WorkspaceResolver = resolve_workspace_binding,
    ) -> None:
        self.connection: Connection = connection
        self._workspace_selector: str = workspace_selector
        self._launch_path: Path = (launch_path or Path.cwd()).expanduser().resolve()
        self._client: _BackendHttpClient = client or HttpApiClient(
            connection.endpoint,
            connection.token,
        )
        self._server_ensurer: ServerEnsurer = server_ensurer
        self._workspace_resolver: WorkspaceResolver = workspace_resolver
        self._binding: WorkspaceBinding | None = None
        self._protocol_version: str = ""

    async def connect(self) -> SessionInfo:
        try:
            await self._server_ensurer(self.connection)
            discovery = await self._capabilities_or_say_why()
            self._binding = await self._workspace_resolver(
                self._client,
                selector=self._workspace_selector,
                path=None if self._workspace_selector else self._launch_path,
            )
        except (ApiError, ManagedServerError, WorkspaceContextError) as exc:
            raise BackendError(str(exc)) from exc

        self._protocol_version = str(discovery.get("protocol_version") or "?")
        return self._session_info()

    async def _capabilities_or_say_why(self) -> JsonObject:
        """The first request of every session, and the one that says what is on the far end.

        A 404 here means something is listening and it is not a harness. Reporting the raw
        status sends a person to look at their credentials or their token, which is what
        happened: a profile pointed at an OpenAI-compatible model gateway -- a real server,
        answering, on the port they had in mind -- and the failure they saw was about a
        keyring entry. Naming the endpoint and what was expected there points at the actual
        mistake. (2026-08-31)
        """
        try:
            return await self._client.capabilities()
        except ApiError as exc:
            if exc.status in {404, 405}:
                raise BackendError(
                    f"{self.connection.endpoint} answered, but it is not a harness: "
                    + f"GET /capabilities returned {exc.status}. Check the URL points at the "
                    + "backend rather than at a model endpoint or another service."
                ) from exc
            raise

    async def start_run(self, request: RunRequest) -> RunInfo:
        binding = self._require_binding()
        try:
            thread_id = request.thread_id
            if not thread_id:
                created_thread = await self._client.create_thread(
                    workspace_id=binding.workspace_id,
                    title=request.message[:120],
                )
                thread_id = str(created_thread["thread_id"])
            created = await self._client.create_run(
                thread_id,
                binding.workspace_id,
                request.message,
                mode=request.mode or None,
                approval_policy=request.policy or None,
                idempotency_key=_new_identity("idem"),
            )
        except (ApiError, KeyError) as exc:
            raise BackendError(str(exc)) from exc
        return RunInfo(str(created["run_id"]), str(created.get("thread_id") or thread_id))

    async def stream(
        self,
        run_id: str,
        *,
        after_seq: int,
        developer: bool,
    ) -> AsyncGenerator[TaskEvent, None]:
        try:
            async for event in self._client.stream_events(
                run_id,
                after_seq=after_seq,
                visibility="all" if developer else "user",
            ):
                normalized = normalize_event(event)
                if normalized is not None:
                    yield normalized
        except ApiError as exc:
            raise BackendError(str(exc)) from exc

    async def send_command(self, run_id: str, command: Command) -> CommandOutcome:
        body: dict[str, JsonValue] = {"command_id": _new_identity("cmd"), **_wire(command)}
        try:
            response = await self._client.send_command(run_id, body)
        except ApiError as exc:
            raise BackendError(str(exc)) from exc
        return CommandOutcome(str(response.get("status") or ""))

    async def switch_workspace(self, selector: str) -> SessionInfo:
        try:
            self._binding = await self._workspace_resolver(
                self._client,
                selector=selector,
                path=None,
            )
        except (ApiError, WorkspaceContextError) as exc:
            raise BackendError(str(exc)) from exc
        return self._session_info()

    async def add_folder(self, thread_id: str | None, path: str) -> ThreadFolders:
        binding = self._require_binding()
        try:
            if not thread_id:
                # The route widens a thread, so a widening before the first message needs
                # one. The backend derives the title from the first message later.
                created = await self._client.create_thread(workspace_id=binding.workspace_id)
                thread_id = str(created["thread_id"])
            body = await self._client.add_folder(thread_id, path)
        except ApiError as exc:
            raise BackendError(str(exc)) from exc
        folders = body.get("folders")
        return ThreadFolders(
            thread_id,
            tuple(str(item) for item in folders) if isinstance(folders, list) else (),
        )

    async def recent_threads(self) -> tuple[ThreadSummary, ...]:
        binding = self._require_binding()
        try:
            body = await self._client.list_threads(
                workspace_id=binding.workspace_id,
                limit=50,
            )
        except ApiError as exc:
            raise BackendError(str(exc)) from exc
        rows = body.get("threads")
        if not isinstance(rows, list):
            return ()
        # A row without a thread_id cannot be continued, so it is dropped here rather than
        # rendered as an entry that does nothing when chosen.
        return tuple(
            ThreadSummary(
                thread_id=str(row["thread_id"]),
                title=str(row.get("title") or ""),
                latest_run_status=str(row.get("latest_run_status") or ""),
                updated_at=str(row.get("updated_at") or ""),
                parent=str(row.get("parent") or ""),
                folder=str(row.get("folder") or ""),
                root_path=str(row.get("root_path") or ""),
            )
            for row in rows
            if isinstance(row, dict) and row.get("thread_id")
        )

    async def load_thread(self, thread_id: str) -> ThreadHistoryInfo:
        """Read bounded canonical history without attaching a second live cursor."""

        binding = self._require_binding()
        try:
            thread = await self._client.get_thread(thread_id)
            workspace_id = str(thread.get("workspace_id") or "")
            if workspace_id and workspace_id != binding.workspace_id:
                raise BackendError("That conversation belongs to another workspace.")
            body = await self._client.list_runs(thread_id=thread_id, limit=50)
            rows = body.get("runs")
            if not isinstance(rows, list):
                rows = []
            run_rows: list[tuple[str, str]] = []
            for row in reversed(rows):
                if not isinstance(row, Mapping):
                    continue
                run_id = str(row.get("run_id") or "")
                if not run_id:
                    continue
                run_rows.append((run_id, str(row.get("status") or "")))

            semaphore = asyncio.Semaphore(6)

            async def read_replay(run_id: str, status: str) -> ThreadReplay:
                async with semaphore:
                    wire_events = await self._client.read_events(
                        run_id,
                        visibility="user",
                        ticks=1,
                    )
                events = tuple(
                    normalized
                    for frame in wire_events
                    if (normalized := normalize_event(frame)) is not None
                )
                return ThreadReplay(run_id=run_id, status=status, events=events)

            runs = await asyncio.gather(
                *(read_replay(run_id, status) for run_id, status in run_rows)
            )
        except BackendError:
            raise
        except ApiError as exc:
            raise BackendError(str(exc)) from exc
        return ThreadHistoryInfo(
            thread_id=str(thread.get("thread_id") or thread_id),
            title=str(thread.get("title") or ""),
            runs=tuple(runs),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _require_binding(self) -> WorkspaceBinding:
        if self._binding is None:
            raise BackendError("The terminal session has not finished connecting.")
        return self._binding

    def _session_info(self) -> SessionInfo:
        binding = self._require_binding()
        return SessionInfo(
            profile=self.connection.profile,
            endpoint=self.connection.endpoint,
            protocol_version=self._protocol_version,
            workspace_id=binding.workspace_id,
            workspace_name=binding.name,
            workspace_path=_display_path(binding.root),
        )


def normalize_event(event: SSEEvent) -> TaskEvent | None:
    """Normalize a wire frame without importing any backend's event model."""

    if event.event == "stream.end":
        return None
    data = event.data
    sequence = _sequence_number(data.get("seq", event.id))
    if sequence is None or sequence < 1:
        return None
    payload = data.get("payload")
    return TaskEvent(
        sequence=sequence,
        event_id=str(data.get("event_id") or event.id or sequence),
        kind=str(data.get("type") or event.event),
        visibility=str(data.get("visibility") or "user"),
        payload=payload if isinstance(payload, Mapping) else {},
    )


def _sequence_number(value: JsonValue) -> int | None:
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _new_identity(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _display_path(path: Path) -> str:
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"
