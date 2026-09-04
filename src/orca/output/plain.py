"""Stable plain-text and JSONL execution without starting a terminal application."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from contextlib import aclosing
from typing import BinaryIO, TextIO

import orjson

from orca.app.actions import Connected, EventReceived, RunAccepted
from orca.app.model import AppState, TurnNote, TurnState
from orca.app.update import reduce
from orca.backend import RunRequest, TerminalBackend
from orca.json_types import JsonObject


async def run_once(
    backend: TerminalBackend,
    request: RunRequest,
    *,
    follow: bool = True,
    jsonl: bool = False,
    stdout: TextIO | None = None,
    binary_stdout: BinaryIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Start one durable run and write its canonical public projection.

    The exit code says how the run ended, so a script need not parse the output to find out:
    0 completed, 1 failed, 2 stopped for an approval or a question, 3 cancelled or blocked.
    """

    text_output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    byte_output = binary_stdout or getattr(text_output, "buffer", None)
    session = await backend.connect()
    state = reduce(
        AppState(thread_id=request.thread_id),
        Connected(
            profile=session.profile,
            endpoint=session.endpoint,
            protocol_version=session.protocol_version,
            workspace_id=session.workspace_id,
            workspace_name=session.workspace_name,
            workspace_path=session.workspace_path,
        ),
    ).state
    accepted = await backend.start_run(
        RunRequest(
            message=request.message,
            thread_id=request.thread_id,
            workspace_id=session.workspace_id,
            mode=request.mode,
            policy=request.policy,
        )
    )
    state = reduce(state, RunAccepted(accepted.run_id, accepted.thread_id)).state

    if jsonl:
        _json_line(
            {
                "version": 1,
                "type": "run.accepted",
                "run_id": accepted.run_id,
                "thread_id": accepted.thread_id,
            },
            text_output=text_output,
            byte_output=byte_output,
        )
    elif not follow:
        _line(text_output, accepted.run_id)

    if not follow:
        return 0

    seen_progress: dict[str, str] = {}
    #: Which steps this stream has already announced as started. The plain renderer is a
    #: transcript rather than a live view, so reprinting the whole checklist on every update
    #: would bury the run in its own plan; the useful signal in a transcript is the moment a
    #: step becomes the current one.
    announced_steps: set[str] = set()
    input_required = False
    async with aclosing(backend.stream(accepted.run_id, after_seq=0, developer=False)) as events:
        async for event in events:
            state = reduce(state, EventReceived(event)).state
            if jsonl:
                _json_line(
                    {
                        "version": 1,
                        "type": "event",
                        "seq": event.sequence,
                        "event_id": event.event_id,
                        "event": event.kind,
                        "payload": dict(event.payload),
                    },
                    text_output=text_output,
                    byte_output=byte_output,
                )
            elif event.kind == "run.progress":
                update_id = str(event.payload.get("update_id") or "")
                text = str(event.payload.get("text") or "").strip()
                if update_id and text and seen_progress.get(update_id) != text:
                    seen_progress[update_id] = text
                    _line(text_output, f"· {text}")
            elif event.kind == "plan.progress":
                for started in _newly_active_steps(event.payload, announced_steps):
                    _line(text_output, f"▸ {started}")
            elif event.kind in {"approval.requested", "question.requested"}:
                title = str(
                    event.payload.get("title") or event.payload.get("prompt") or "Input needed"
                )
                _line(text_output, f"! {title}")
            if event.kind in {"approval.requested", "question.requested"}:
                input_required = True
                break

    if input_required:
        return 2
    if not state.turns:
        return 0
    turn = state.turns[-1]
    if not jsonl:
        # What the model said stays, whichever way the run ended: a failure's partial answer
        # is the part of the transcript most worth reading.
        if turn.answer:
            if seen_progress:
                _line(text_output, "")
            _line(text_output, turn.answer.rstrip())
        if turn.status in _STOPPED:
            # The status and the reason go to stderr, where a diagnostic belongs: stdout is
            # the answer and nothing else, so `orca run … > answer.txt` stays clean and the
            # exit code, not a grep, tells the caller the run did not finish.
            reason = _ended_reason(turn)
            _line(error_output, f"{turn.status}: {reason}" if reason else turn.status)
    return _STOPPED.get(turn.status, 0)


#: Exit codes for the ways a run stops short of an answer. A failure is the backend's fault
#: and gets the conventional 1; a cancel or a block is somebody's decision, so it is told apart
#: from both a failure and the 2 that means "answer the prompt and run again".
_STOPPED: Mapping[str, int] = {"failed": 1, "cancelled": 3, "blocked": 3}


def _ended_reason(turn: TurnState) -> str:
    """The summary a terminal event gave for stopping, as the reducer noted it on the turn."""

    for item in reversed(turn.timeline):
        if isinstance(item, TurnNote) and item.kind == "ended":
            return item.text
    return ""


def _line(output: TextIO, text: str) -> None:
    """Write one line and flush it. Plain output is followed as it happens -- through a pipe as
    often as on a terminal -- and a pipe is block-buffered, so an unflushed line sat invisible
    until the run ended."""

    output.write(f"{text}\n")
    output.flush()


def _newly_active_steps(payload: JsonObject, announced: set[str]) -> list[str]:
    """Steps that have just become `in_progress`, in the order the plan lists them.

    Counts nothing and enforces nothing: the server states "at most one in progress" and does not
    check it, so a plan with three renders three lines rather than picking one and lying about
    the other two.
    """

    entries = payload.get("plan")
    # Any sequence but a string: a plan decoded from the wire is a list, one built in process
    # or replayed from history may be a tuple, and a `str` is a sequence of its own letters.
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    started: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("status") != "in_progress":
            continue
        step = str(entry.get("step") or "").strip()
        if step and step not in announced:
            announced.add(step)
            started.append(step)
    return started


def _json_line(
    value: object,
    *,
    text_output: TextIO,
    byte_output: BinaryIO | None,
) -> None:
    encoded = orjson.dumps(value, option=orjson.OPT_APPEND_NEWLINE)
    if byte_output is not None:
        byte_output.write(encoded)
        byte_output.flush()
    else:
        text_output.write(encoded.decode("utf-8"))
        text_output.flush()
