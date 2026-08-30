"""A minimal client for the HTTP backend contract in `docs/backend-contract.md`.

Speaks HTTP only. It exists as much to prove the contract is complete as to be convenient:
anything orca cannot do without reaching into a particular backend's internals is something a
second implementation would also be unable to do.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Self

import httpx
import orjson

API_PREFIX = "/api/v1"

#: Reconnect pacing. The handler here used to be a bare `continue`: against a dead port a
#: following client made 4564 connect attempts in 2.0s at 81% CPU — precisely while the next
#: server instance was trying to take its lock, so the client hammered the socket of the
#: server it was waiting for. (measured 2026-08-17)
_RECONNECT_BASE_S = 0.25
_RECONNECT_CAP_S = 5.0
#: How many consecutive attempts that deliver nothing before giving up. With the schedule
#: above that is ~23s of trying, which spans a restart without pretending a gone server will
#: come back.
_RECONNECT_ATTEMPTS = 8
_IDEMPOTENT_POST_ATTEMPTS = 2

#: `stream.end` reasons that mean the *run* ended. Everything else — `server_shutdown`,
#: `tick_limit`, or a reason a later server invents — ends the connection only, and the
#: contract for that is to reconnect with `?after_seq=<final_seq>` and lose nothing. An
#: unknown reason is treated as "still going" for the same reason an unknown `status` is.
_REASONS_THAT_END_THE_RUN = frozenset({"terminal", "terminal_without_event"})


@dataclass
class SSEEvent:
    id: str | None
    event: str
    data: dict[str, Any]


def stream_end_reason(event: SSEEvent) -> str | None:
    """Why the stream stopped, for a `stream.end` frame.

    The server frames `stream.end` with its payload as the whole `data` object rather than
    nested under `payload:` like an ordinary event, so a reader that only looks at
    `data["payload"]` sees an empty dict and treats every reason alike. Both shapes are read
    here so no caller has to know which one it got. (found 2026-08-17)
    """
    if event.event != "stream.end":
        return None
    payload = event.data.get("payload")
    if isinstance(payload, dict) and payload.get("reason"):
        return str(payload["reason"])
    reason = event.data.get("reason")
    return str(reason) if reason else None


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        # A zero status is a client-side failure with no response behind it — the reconnect
        # ceiling below. Printing "0 stream_unreachable" would invent an HTTP status.
        prefix = f"{status} " if status else ""
        super().__init__(f"{prefix}{code}: {message}")


def _error_for(resp: httpx.Response) -> ApiError:
    """Turn any error response into an `ApiError`, including ones this server did not author.

    The contract's envelope is `{"detail": {"code", "message"}}`, and a client that assumes it
    crashes on everything else: FastAPI's own 404 is `{"detail": "Not Found"}` — a *string* — and a
    proxy or gateway in front of the server may return HTML. Transport failures must remain
    legible without inventing a second protocol interpretation. (found 2026-08-17)
    """
    detail: Any = None
    try:
        body = resp.json() if resp.content else {}
        detail = body.get("detail") if isinstance(body, dict) else body
    except ValueError:
        detail = None

    if isinstance(detail, dict):
        return ApiError(
            resp.status_code,
            str(detail.get("code", "error")),
            str(detail.get("message", resp.text[:200])),
        )
    if isinstance(detail, str) and detail:
        # FastAPI's own shape. `not_found` is a better code than `error` for the case a client
        # actually hits: asking a server for an endpoint it does not have.
        code = "not_found" if resp.status_code == 404 else "error"
        return ApiError(resp.status_code, code, detail)
    return ApiError(resp.status_code, "error", resp.text[:200] or resp.reason_phrase)


class HttpApiClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 30.0) -> None:
        self._origin = base_url.rstrip("/")
        self._base = self._origin + API_PREFIX
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._client.request(method, f"{self._base}{path}", **kw)
        except httpx.TimeoutException as exc:
            raise ApiError(
                0,
                "server_timeout",
                f"The backend at {self._origin} did not respond in time.",
            ) from exc
        except httpx.RequestError as exc:
            raise ApiError(
                0,
                "server_unreachable",
                f"Could not connect to the backend at {self._origin}.",
            ) from exc
        if resp.status_code >= 400:
            raise _error_for(resp)
        return resp.json() if resp.content else None

    async def _idempotent_post(self, path: str, **kw: Any) -> Any:
        """Retry one safely identified POST without minting a second operation.

        A connection can disappear after the server commits but before the response reaches the
        client.  Retrying the same body and idempotency identity is the only way to distinguish
        that case from a request that never arrived.  This helper is deliberately limited to the
        two contract writes that supply such an identity.
        """

        for attempt in range(_IDEMPOTENT_POST_ATTEMPTS):
            try:
                return await self._request("POST", path, **kw)
            except ApiError as exc:
                retryable = exc.status == 0 and exc.code in {
                    "server_timeout",
                    "server_unreachable",
                }
                if not retryable or attempt + 1 >= _IDEMPOTENT_POST_ATTEMPTS:
                    raise
                await asyncio.sleep(_RECONNECT_BASE_S)
        raise AssertionError("idempotent POST retry loop did not return")

    # -- workspaces --------------------------------------------------------------------

    async def create_workspace(
        self,
        name: str,
        root_path: str,
        config: dict | None = None,
        vcs: str | None = None,
        replace_existing: bool = False,
    ):
        body: dict[str, Any] = {"name": name, "root_path": root_path}
        if config is not None:
            body["config"] = config
        if vcs is not None:
            # Filesystem folders are first-class; VCS metadata is sent only when discovery
            # actually found it rather than inferring a Git checkout.
            body["vcs"] = vcs
        if replace_existing:
            body["replace_existing"] = True
        return await self._request("POST", "/workspaces", json=body)

    async def list_workspaces(self):
        return await self._request("GET", "/workspaces")

    # -- threads and runs ----------------------------------------------------------------

    async def create_thread(self, workspace_id: str | None = None, title: str = ""):
        return await self._request(
            "POST", "/threads", json={"workspace_id": workspace_id, "title": title}
        )

    async def create_run(
        self,
        thread_id: str,
        workspace_id: str | None,
        message: str,
        *,
        mode: str | None = None,
        approval_policy: str | None = None,
        client_context: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ):
        """Start a run.

        Every optional field here is one orca's own surface can express. The contract permits a
        backend to accept more, and this client deliberately does not carry parameters no view
        can set: an argument nothing sends is a contract claim nothing tests.
        """
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        # Omitted rather than sent as null when there is none: the field is optional, and a
        # run with no workspace is how this client works in a directory that is not a
        # repository.
        body: dict[str, Any] = {"message": {"content": message}}
        if workspace_id:
            body["workspace_id"] = workspace_id
        if mode:
            body["mode"] = mode
        if approval_policy:
            body["approval_policy"] = approval_policy
        if client_context:
            body["client_context"] = client_context
        path = f"/threads/{thread_id}/runs"
        if idempotency_key:
            return await self._idempotent_post(path, json=body, headers=headers)
        return await self._request("POST", path, json=body, headers=headers)

    async def list_runs(self, **params):
        return await self._request(
            "GET", "/runs", params={k: v for k, v in params.items() if v is not None}
        )

    async def get_thread(self, thread_id: str):
        return await self._request("GET", f"/threads/{thread_id}")

    async def list_threads(self, **params):
        return await self._request(
            "GET", "/threads", params={k: v for k, v in params.items() if v is not None}
        )

    async def send_command(self, run_id: str, command: dict[str, Any]):
        if command.get("command_id"):
            return await self._idempotent_post(f"/runs/{run_id}/commands", json=command)
        return await self._request("POST", f"/runs/{run_id}/commands", json=command)

    # -- discovery ---------------------------------------------------------------------

    async def capabilities(self):
        return await self._request("GET", "/capabilities")

    # -- events ----------------------------------------------------------------------------

    async def read_events(
        self, run_id: str, *, visibility: str = "all", ticks: int = 1
    ) -> list[SSEEvent]:
        """The log so far, as a list. Bounded by `ticks`, so it returns for a live run too.

        Assembled from the same stream a UI follows: there is deliberately no "give me the events
        as JSON" endpoint, because a second read path is a second thing that can disagree with the
        log.
        """
        events: list[SSEEvent] = []
        parser = _SSEParser()
        async with self._client.stream(
            "GET",
            f"{self._base}/runs/{run_id}/events",
            params={"after_seq": 0, "visibility": visibility, "ticks": ticks},
            headers={"Accept": "text/event-stream"},
            timeout=httpx.Timeout(30.0, read=None),
        ) as resp:
            if resp.status_code >= 400:
                # The body has not been read yet on a streamed response, and `_error_for`
                # needs it to find the server's `{code, message}`.
                await resp.aread()
                raise _error_for(resp)
            async for line in resp.aiter_lines():
                events.extend(parser.feed(line))
        return events

    async def stream_events(
        self, run_id: str, *, after_seq: int = 0, visibility: str = "user"
    ) -> AsyncIterator[SSEEvent]:
        """Follow a run, resuming automatically from the last seq seen.

        Reconnecting on a dropped connection is the client's job, not the server's, and it
        is safe precisely because `after_seq` makes the request idempotent: the same cursor
        always yields the same suffix of the log.

        The loop deliberately reads the response to its natural end rather than breaking out
        on `stream.end`. Breaking would abandon this generator while it is suspended inside
        `async with client.stream(...)`, and unwinding that context needs an `await` that
        generator finalization is not allowed to perform — which surfaces as
        "aclose(): asynchronous generator is already running" at interpreter shutdown.
        The server closes the connection right after `stream.end`, so letting the iterator
        run out costs nothing.

        `stream.end` is *not* the ending: only its `reason` says whether the run is over. A
        follow that stopped on any reason returned silently on `server_shutdown` while the run
        it was watching carried on, which is the one thing this loop exists to prevent.
        """
        cursor = after_seq
        attempt = 0
        while True:
            finished = False
            delivered = False
            try:
                async with self._client.stream(
                    "GET",
                    f"{self._base}/runs/{run_id}/events",
                    params={"after_seq": cursor, "visibility": visibility},
                    headers={"Accept": "text/event-stream"},
                    timeout=httpx.Timeout(10.0, read=None),
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise _error_for(resp)
                    parser = _SSEParser()
                    async for line in resp.aiter_lines():
                        for event in parser.feed(line):
                            if event.id and event.id.isdigit():
                                cursor = int(event.id)
                            if event.event == "stream.end":
                                finished = stream_end_reason(event) in _REASONS_THAT_END_THE_RUN
                            else:
                                delivered = True
                            yield event
            except (httpx.TransportError, httpx.ReadTimeout):
                pass  # reconnect from the cursor; `after_seq` makes that lossless
            if finished:
                return
            # A connection that carried events was a healthy one, whatever ended it: the
            # ceiling is for a server that is gone, not for a run that takes an hour.
            attempt = 0 if delivered else attempt + 1
            if attempt > _RECONNECT_ATTEMPTS:
                raise ApiError(
                    0,
                    "stream_unreachable",
                    f"Lost the event stream for {run_id} and could not get it back after "
                    f"{_RECONNECT_ATTEMPTS} attempts. The run may still be going; it can be "
                    f"followed again from seq {cursor} once the backend is reachable.",
                )
            await _pause_before_reconnect(attempt)


async def _pause_before_reconnect(attempt: int) -> None:
    """Capped exponential backoff, deliberately its own function so a test can replace it.

    Attempt 0 is a connection that *did* deliver events. It still pauses, for half the base
    delay, so a server that closes the stream immediately after every event cannot be turned
    back into the hot loop this replaces.
    """
    await asyncio.sleep(min(_RECONNECT_CAP_S, _RECONNECT_BASE_S * 2 ** (attempt - 1)))


class _SSEParser:
    """Incremental SSE framing.

    A blank line terminates an event and `data:` may repeat within one, which is why the
    stream has to be read line-by-line — a helper that drops empty lines silently merges
    every event into the next.
    """

    def __init__(self) -> None:
        self._id: str | None = None
        self._event = "message"
        self._data: list[str] = []

    def feed(self, line: str) -> list[SSEEvent]:
        if line.startswith(":"):
            return []  # keepalive comment
        if line == "":
            if not self._data:
                return []
            event = SSEEvent(
                id=self._id, event=self._event, data=orjson.loads("\n".join(self._data))
            )
            self._id, self._event, self._data = None, "message", []
            return [event]
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "id":
            self._id = value
        elif field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return []
