"""Stable plain-text and JSONL execution without starting a terminal application."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from contextlib import aclosing
from typing import Any, BinaryIO, TextIO

import orjson

from orca.app.actions import BootCompleted, EventReceived, RunAccepted
from orca.app.model import AppState
from orca.app.update import reduce
from orca.backend import RunRequest, TerminalBackend


async def run_once(
    backend: TerminalBackend,
    request: RunRequest,
    *,
    follow: bool = True,
    jsonl: bool = False,
    stdout: TextIO | None = None,
    binary_stdout: BinaryIO | None = None,
) -> int:
    """Start one durable run and write its canonical public projection."""

    text_output = stdout or sys.stdout
    byte_output = binary_stdout or getattr(text_output, "buffer", None)
    boot = await backend.boot()
    state = reduce(
        AppState(thread_id=request.thread_id),
        BootCompleted(
            profile=boot.profile,
            endpoint=boot.endpoint,
            protocol_version=boot.protocol_version,
            workspace_id=boot.workspace_id,
            workspace_name=boot.workspace_name,
            workspace_path=boot.workspace_path,
            cwd_relative=boot.cwd_relative,
            capabilities=boot.capabilities,
        ),
    ).state
    accepted = await backend.start_run(
        RunRequest(
            message=request.message,
            thread_id=request.thread_id,
            workspace_id=boot.workspace_id,
            cwd_relative=boot.cwd_relative,
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
        text_output.write(f"{accepted.run_id}\n")

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
                    text_output.write(f"· {text}\n")
            elif event.kind == "plan.progress":
                for started in _newly_active_steps(event.payload, announced_steps):
                    text_output.write(f"▸ {started}\n")
            elif event.kind in {"approval.requested", "question.requested"}:
                title = str(
                    event.payload.get("title") or event.payload.get("prompt") or "Input needed"
                )
                text_output.write(f"! {title}\n")
            if event.kind in {"approval.requested", "question.requested"}:
                input_required = True
                break

    if not jsonl and state.turns:
        turn = state.turns[-1]
        answer = turn.answer
        if answer:
            if seen_progress:
                text_output.write("\n")
            text_output.write(answer.rstrip() + "\n")
        elif turn.status not in {"completed", "running", "queued"}:
            text_output.write(f"{turn.status}\n")
    return 2 if input_required else 0


def _newly_active_steps(payload: Mapping[str, Any], announced: set[str]) -> list[str]:
    """Steps that have just become `in_progress`, in the order the plan lists them.

    Counts nothing and enforces nothing: the server states "at most one in progress" and does not
    check it, so a plan with three renders three lines rather than picking one and lying about
    the other two.
    """

    entries = payload.get("plan")
    if not isinstance(entries, list):
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
