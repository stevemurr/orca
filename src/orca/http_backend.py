"""The bundled HTTP implementation of the terminal application's backend port.

One implementation of `orca.backend.TerminalBackend`, speaking the wire contract in
`docs/backend-contract.md`. It is the reference, not the definition: a harness that cannot
serve that contract implements `TerminalBackend` directly instead.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from orca.app.model import TaskEvent, ThreadReplay
from orca.backend import (
    BackendError,
    BootInfo,
    RunInfo,
    RunRequest,
    ThreadHistoryInfo,
)
from orca.client import ApiError, HttpApiClient, SSEEvent
from orca.connection import Connection
from orca.server_manager import ManagedServerError, ensure_local_server
from orca.workspace_context import (
    WorkspaceBinding,
    WorkspaceContextError,
    resolve_workspace_binding,
)


class _BackendHttpClient(Protocol):
    async def capabilities(self) -> dict[str, Any]: ...

    async def create_thread(
        self, workspace_id: str | None = None, title: str = ""
    ) -> dict[str, Any]: ...

    async def create_run(
        self,
        thread_id: str,
        workspace_id: str | None,
        message: str,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def stream_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        visibility: str = "user",
    ) -> AsyncIterator[SSEEvent]: ...

    async def send_command(self, run_id: str, command: dict[str, Any]) -> dict[str, Any]: ...

    async def list_threads(self, **params: Any) -> dict[str, Any]: ...

    async def get_thread(self, thread_id: str) -> dict[str, Any]: ...

    async def list_runs(self, **params: Any) -> dict[str, Any]: ...

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
        self.connection = connection
        self._workspace_selector = workspace_selector
        self._launch_path = (launch_path or Path.cwd()).expanduser().resolve()
        self._client: _BackendHttpClient = client or HttpApiClient(
            connection.endpoint,
            connection.token,
        )
        self._server_ensurer = server_ensurer
        self._workspace_resolver = workspace_resolver
        self._binding: WorkspaceBinding | None = None
        self._protocol_version = ""
        self._capabilities: frozenset[str] = frozenset()

    async def boot(self) -> BootInfo:
        try:
            await self._server_ensurer(self.connection)
            capabilities = await self._client.capabilities()
            self._binding = await self._workspace_resolver(
                self._client,
                selector=self._workspace_selector,
                path=None if self._workspace_selector else self._launch_path,
            )
        except (ApiError, ManagedServerError, WorkspaceContextError) as exc:
            raise BackendError(str(exc)) from exc

        self._protocol_version = str(capabilities.get("protocol_version") or "?")
        features = capabilities.get("features")
        if isinstance(features, Mapping):
            self._capabilities = frozenset(
                str(name) for name, available in features.items() if available is True
            )
        return self._boot_info()

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
                client_context={"cwd_relative": binding.cwd_relative},
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
    ) -> AsyncIterator[TaskEvent]:
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

    async def send_command(
        self,
        run_id: str,
        command: str,
        fields: dict[str, str],
    ) -> dict[str, object]:
        body: dict[str, Any] = {
            "command_id": _new_identity("cmd"),
            "type": command,
            **fields,
        }
        try:
            response = await self._client.send_command(run_id, body)
        except ApiError as exc:
            raise BackendError(str(exc)) from exc
        return cast(dict[str, object], response)

    async def switch_workspace(self, selector: str) -> BootInfo:
        try:
            self._binding = await self._workspace_resolver(
                self._client,
                selector=selector,
                path=None,
            )
        except (ApiError, WorkspaceContextError) as exc:
            raise BackendError(str(exc)) from exc
        return self._boot_info()

    async def recent_threads(self) -> tuple[dict[str, object], ...]:
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
        return tuple(cast(dict[str, object], row) for row in rows if isinstance(row, dict))

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

    def _boot_info(self) -> BootInfo:
        binding = self._require_binding()
        return BootInfo(
            profile=self.connection.profile,
            endpoint=self.connection.endpoint,
            protocol_version=self._protocol_version,
            workspace_id=binding.workspace_id,
            workspace_name=binding.name,
            workspace_path=_display_path(binding.active_directory),
            cwd_relative=binding.cwd_relative,
            capabilities=self._capabilities,
        )


def normalize_event(event: SSEEvent) -> TaskEvent | None:
    """Normalize a wire frame without importing any backend's event model."""

    if event.event == "stream.end":
        return None
    data = event.data
    raw_sequence = data.get("seq", event.id)
    try:
        sequence = int(raw_sequence)
    except (TypeError, ValueError):
        return None
    if sequence < 1:
        return None
    payload = data.get("payload")
    return TaskEvent(
        sequence=sequence,
        event_id=str(data.get("event_id") or event.id or sequence),
        kind=str(data.get("type") or event.event),
        visibility=str(data.get("visibility") or "user"),
        payload=payload if isinstance(payload, Mapping) else {},
    )


def _new_identity(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _display_path(path: Path) -> str:
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative.as_posix()}"
