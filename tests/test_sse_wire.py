"""Wire-level tests for the SSE follow.

Every other test double sits *above* the framing, handing the adapter ready-made `SSEEvent`
objects. That left the client's one hard obligation — how a follow ends — covered nowhere, and
it is the part of the contract a hand-written backend gets wrong first: `stream.end` is the
only frame identified by its SSE `event:` name rather than by a `type` inside `data`, and the
connection closing behind it is what actually returns control, because the loop drains rather
than breaks.

These tests parse real bytes and pin the framing half. The closing half cannot be tested here —
a canned transport always reaches EOF, so there is no way to hold the socket open the way a
keep-alive server does — which is exactly why `docs/backend-contract.md` has to say it in words.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from orca.client import ApiError, HttpApiClient, MalformedEventError, SSEParser

TERMINAL_RUN = (
    b'id: 1\ndata: {"event_id": "evt-1", "seq": 1, "type": "run.created", '
    b'"visibility": "user", "payload": {"message": "hello"}}\n\n'
    b": keepalive\n\n"
    b'id: 2\ndata: {"event_id": "evt-2", "seq": 2, "type": "run.completed", '
    b'"visibility": "user", "payload": {"summary": "done"}}\n\n'
    b'event: stream.end\ndata: {"reason": "terminal"}\n\n'
)

#: The same log, ended the way a backend guesses from the event table alone: `stream.end` as a
#: `type` inside `data`, with no `event:` line.
MISFRAMED_END = TERMINAL_RUN.replace(
    b'event: stream.end\ndata: {"reason": "terminal"}\n\n',
    b'data: {"type": "stream.end", "reason": "terminal"}\n\n',
)


def _client(
    handler: Callable[[httpx.Request], httpx.Response], monkeypatch: pytest.MonkeyPatch
) -> HttpApiClient:
    """An `HttpApiClient` whose transport is a canned SSE responder, and no reconnect pause."""

    async def _no_pause(attempt: int) -> None:
        del attempt

    monkeypatch.setattr("orca.client._pause_before_reconnect", _no_pause)
    client = HttpApiClient("http://backend.test")
    # The private attribute is the only seam: `HttpApiClient` builds its own transport, and a
    # constructor parameter for one would be production surface that exists only for a test.
    client._client = httpx.AsyncClient(  # pyright: ignore[reportPrivateUsage]
        transport=httpx.MockTransport(handler)
    )
    return client


async def test_named_stream_end_ends_the_follow(monkeypatch: pytest.MonkeyPatch) -> None:
    cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params["after_seq"])
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=TERMINAL_RUN
        )

    client = _client(handler, monkeypatch)
    kinds = [event.data.get("type", event.event) async for event in client.stream_events("run-1")]
    await client.aclose()

    assert kinds == ["run.created", "run.completed", "stream.end"]
    assert cursors == ["0"], "a named `stream.end` with reason `terminal` must not reconnect"


async def test_stream_end_written_as_a_data_type_never_ends_the_follow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure `docs/backend-contract.md` now warns about, pinned as behaviour.

    Without the `event:` line the frame is just an unknown kind, so the follow reconnects from
    its cursor forever rather than returning. The test stops it by hand at the second attempt;
    a real backend gets a silent hang, which is why the doc says to write the `event:` line.
    """

    cursors: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params["after_seq"])
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=MISFRAMED_END
        )

    client = _client(handler, monkeypatch)
    stream = client.stream_events("run-1")
    async for _ in stream:
        if len(cursors) > 1:
            break
    await stream.aclose()
    await client.aclose()

    assert cursors[:2] == ["0", "2"], "the follow reconnected from the last seq it saw"


def test_parser_reads_comments_and_repeated_data_lines() -> None:
    parser = SSEParser()

    assert parser.feed(": keepalive") == []
    assert parser.feed("id: 7") == []
    assert parser.feed("event: stream.end") == []
    assert parser.feed('data: {"reason":') == []
    assert parser.feed('data: "terminal"}') == []

    (event,) = parser.feed("")
    assert (event.id, event.event, event.data) == ("7", "stream.end", {"reason": "terminal"})


def test_the_client_never_consults_a_proxy_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`HTTP_PROXY` made httpx route every plain-http request -- bearer token included, and
    for 127.0.0.1 too -- through a plaintext proxy. The health probe already ignored the
    environment; the client carrying the credential must as well. (found 2026-09-04)"""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")

    client = HttpApiClient("http://127.0.0.1:8080", token="secret")

    transport = client._client  # pyright: ignore[reportPrivateUsage]
    assert transport.trust_env is False
    # Belt and braces: with the environment ignored there is no proxy mount at all.
    assert transport._mounts == {}  # pyright: ignore[reportPrivateUsage]


async def test_a_dead_port_during_a_history_read_is_an_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`read_events` streams rather than going through `_send`, so the connect failure it hit
    was a raw `httpx.ConnectError` that no `except ApiError` above it caught."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler, monkeypatch)
    with pytest.raises(ApiError, match="server_unreachable"):
        await client.read_events("run-1")
    await client.aclose()


async def test_a_frame_that_is_not_json_is_an_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"id: 1\nevent: run.progress\ndata: {not json\n\n",
        )

    client = _client(handler, monkeypatch)
    with pytest.raises(MalformedEventError, match="not JSON"):
        async for _ in client.stream_events("run-1"):
            pass
    await client.aclose()


def test_parser_reports_bad_json_as_a_malformed_frame() -> None:
    parser = SSEParser()
    assert parser.feed("data: {oops") == []

    with pytest.raises(ApiError) as raised:
        parser.feed("")
    assert raised.value.code == "malformed_frame"
