"""Non-interactive output consumes the same semantic event reducer as the TUI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from io import StringIO

import orjson

from orca.app.model import TaskEvent
from orca.backend import RunInfo, RunRequest, SessionInfo
from orca.output.plain import run_once


class PlainBackend:
    async def connect(self) -> SessionInfo:
        return SessionInfo("local", "http://localhost", "1.6", "ws-1", "project", "/project")

    async def start_run(self, request: RunRequest) -> RunInfo:
        assert request.workspace_id == "ws-1"
        return RunInfo("run-1", "thread-1")

    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncIterator[TaskEvent]:
        del run_id, after_seq, developer
        yield TaskEvent(
            1,
            "evt-1",
            "run.created",
            "user",
            {"message": "Build it"},
        )
        yield TaskEvent(
            2,
            "evt-2",
            "run.progress",
            "user",
            {"update_id": "work:one", "status": "active", "text": "Building it."},
        )
        yield TaskEvent(
            3,
            "evt-3",
            "run.completed",
            "user",
            {"summary": "Built and verified it."},
        )


class ApprovalBackend(PlainBackend):
    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncIterator[TaskEvent]:
        del run_id, after_seq, developer
        yield TaskEvent(
            1,
            "evt-1",
            "approval.requested",
            "user",
            {"approval_id": "approval-1", "title": "Run the release command?"},
        )
        raise AssertionError("plain mode must detach instead of waiting forever")


async def test_plain_run_prints_progress_then_canonical_answer() -> None:
    output = StringIO()
    code = await run_once(
        PlainBackend(),  # type: ignore[arg-type]
        RunRequest("Build it", None, "", ".", "auto", "safe"),
        stdout=output,
    )

    assert code == 0
    assert output.getvalue() == "· Building it.\n\nBuilt and verified it.\n"


async def test_jsonl_is_versioned_and_keeps_wire_facts_structured() -> None:
    output = StringIO()
    await run_once(
        PlainBackend(),  # type: ignore[arg-type]
        RunRequest("Build it", None, "", ".", "auto", "safe"),
        stdout=output,
        jsonl=True,
    )

    rows = [orjson.loads(line) for line in output.getvalue().splitlines()]
    assert rows[0] == {
        "version": 1,
        "type": "run.accepted",
        "run_id": "run-1",
        "thread_id": "thread-1",
    }
    assert [row.get("event") for row in rows[1:]] == [
        "run.created",
        "run.progress",
        "run.completed",
    ]


async def test_no_follow_prints_only_copyable_run_identity() -> None:
    output = StringIO()
    await run_once(
        PlainBackend(),  # type: ignore[arg-type]
        RunRequest("Build it", None, "", ".", "auto", "safe"),
        stdout=output,
        follow=False,
    )

    assert output.getvalue() == "run-1\n"


async def test_plain_run_detaches_with_distinct_exit_when_input_is_required() -> None:
    output = StringIO()

    code = await run_once(
        ApprovalBackend(),  # type: ignore[arg-type]
        RunRequest("Release it", None, "", ".", "auto", "safe"),
        stdout=output,
    )

    assert code == 2
    assert output.getvalue() == "! Run the release command?\n"


class PlanBackend(PlainBackend):
    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncIterator[TaskEvent]:
        del run_id, after_seq, developer
        plans = (
            [
                {"step": "read the router", "status": "in_progress"},
                {"step": "add the handler", "status": "pending"},
            ],
            # Republished unchanged: a plan the model resent, or a read effect replayed after a
            # restart. Neither should print the same step twice.
            [
                {"step": "read the router", "status": "in_progress"},
                {"step": "add the handler", "status": "pending"},
            ],
            [
                {"step": "read the router", "status": "completed"},
                {"step": "add the handler", "status": "in_progress"},
            ],
        )
        for index, plan in enumerate(plans, start=1):
            yield TaskEvent(index, f"evt-{index}", "plan.progress", "user", {"plan": plan})
        yield TaskEvent(4, "evt-4", "run.completed", "user", {"summary": "Added the handler."})


async def test_plain_run_announces_a_step_when_it_starts_and_only_then() -> None:
    """A transcript, not a live view: the checklist is the wrong shape to reprint every update.

    What a transcript can carry is the moment a step becomes the current one, so a republished
    or replayed plan adds nothing.
    """

    output = StringIO()
    code = await run_once(
        PlanBackend(),  # type: ignore[arg-type]
        RunRequest("Add the endpoint", None, "", ".", "auto", "safe"),
        stdout=output,
    )

    assert code == 0
    assert output.getvalue() == (
        "▸ read the router\n▸ add the handler\nAdded the handler.\n"
    )
