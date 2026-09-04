"""Non-interactive output consumes the same semantic event reducer as the TUI."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from io import StringIO
from typing import cast, override

import orjson

from orca.app.model import TaskEvent
from orca.backend import RunInfo, RunRequest
from orca.json_types import JsonObject
from orca.output.plain import run_once
from tests.support.backends import ScriptedBackend


class PlainBackend(ScriptedBackend):
    @override
    async def start_run(self, request: RunRequest) -> RunInfo:
        assert request.workspace_id == "ws-1"
        return self.accepted

    @override
    def events(self) -> Sequence[TaskEvent]:
        return (
            TaskEvent(1, "evt-1", "run.created", "user", {"message": "Build it"}),
            TaskEvent(
                2,
                "evt-2",
                "run.progress",
                "user",
                {"update_id": "work:one", "status": "active", "text": "Building it."},
            ),
            TaskEvent(3, "evt-3", "run.completed", "user", {"summary": "Built and verified it."}),
        )


class ApprovalBackend(PlainBackend):
    @override
    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncGenerator[TaskEvent, None]:
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
        PlainBackend(),
        RunRequest("Build it", None, "", "normal", "ask"),
        stdout=output,
    )

    assert code == 0
    assert output.getvalue() == "· Building it.\n\nBuilt and verified it.\n"


async def test_jsonl_is_versioned_and_keeps_wire_facts_structured() -> None:
    output = StringIO()
    await run_once(
        PlainBackend(),
        RunRequest("Build it", None, "", "normal", "ask"),
        stdout=output,
        jsonl=True,
    )

    rows = [cast(JsonObject, orjson.loads(line)) for line in output.getvalue().splitlines()]
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
        PlainBackend(),
        RunRequest("Build it", None, "", "normal", "ask"),
        stdout=output,
        follow=False,
    )

    assert output.getvalue() == "run-1\n"


async def test_plain_run_detaches_with_distinct_exit_when_input_is_required() -> None:
    output = StringIO()

    code = await run_once(
        ApprovalBackend(),
        RunRequest("Release it", None, "", "normal", "ask"),
        stdout=output,
    )

    assert code == 2
    assert output.getvalue() == "! Run the release command?\n"


class PlanBackend(PlainBackend):
    @override
    async def stream(
        self, run_id: str, *, after_seq: int, developer: bool
    ) -> AsyncGenerator[TaskEvent, None]:
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
        PlanBackend(),
        RunRequest("Add the endpoint", None, "", "normal", "ask"),
        stdout=output,
    )

    assert code == 0
    assert output.getvalue() == ("▸ read the router\n▸ add the handler\nAdded the handler.\n")


class StoppedBackend(PlainBackend):
    """A run that narrates, streams half an answer, and then stops for a reason."""

    def __init__(self, kind: str, *, with_answer: bool = True) -> None:
        self.kind: str = kind
        self.with_answer: bool = with_answer

    @override
    def events(self) -> Sequence[TaskEvent]:
        streamed = (
            (
                TaskEvent(
                    3,
                    "evt-3",
                    "answer.delta",
                    "user",
                    {"effect_id": "a", "model_call_id": "b", "text": "Half an answer"},
                ),
            )
            if self.with_answer
            else ()
        )
        return (
            TaskEvent(1, "evt-1", "run.created", "user", {"message": "Build it"}),
            TaskEvent(
                2,
                "evt-2",
                "run.progress",
                "user",
                {"update_id": "work:one", "status": "active", "text": "Building it."},
            ),
            *streamed,
            TaskEvent(4, "evt-4", self.kind, "user", {"summary": "The sandbox went away."}),
        )


async def test_plain_run_keeps_the_partial_answer_and_says_why_it_stopped() -> None:
    """A stopped run used to exit 0 and, once any answer had streamed, drop the status word.

    The words stay on stdout; the status and its reason go to stderr; the exit code tells a
    script which way the run ended without parsing either.
    """

    for kind, code in (("run.failed", 1), ("run.cancelled", 3), ("run.blocked", 3)):
        output, errors = StringIO(), StringIO()
        status = await run_once(
            StoppedBackend(kind),
            RunRequest("Build it", None, "", "normal", "ask"),
            stdout=output,
            stderr=errors,
        )

        assert status == code, kind
        assert output.getvalue() == "· Building it.\n\nHalf an answer\n", kind
        assert errors.getvalue() == f"{kind.removeprefix('run.')}: The sandbox went away.\n"


async def test_plain_run_reports_a_stop_with_nothing_streamed() -> None:
    output, errors = StringIO(), StringIO()
    status = await run_once(
        StoppedBackend("run.failed", with_answer=False),
        RunRequest("Build it", None, "", "normal", "ask"),
        stdout=output,
        stderr=errors,
    )

    assert status == 1
    assert output.getvalue() == "· Building it.\n"
    assert errors.getvalue() == "failed: The sandbox went away.\n"


async def test_jsonl_exit_code_says_how_the_run_ended_too() -> None:
    output = StringIO()
    status = await run_once(
        StoppedBackend("run.cancelled"),
        RunRequest("Build it", None, "", "normal", "ask"),
        stdout=output,
        jsonl=True,
    )

    assert status == 3
    assert [orjson.loads(line)["type"] for line in output.getvalue().splitlines()] == [
        "run.accepted",
        "event",
        "event",
        "event",
        "event",
    ]


class FlushRecorder(StringIO):
    """Remembers what had been written at each flush, so a test can see what a reader at the
    far end of a pipe would have seen at that moment."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[str] = []

    @override
    def flush(self) -> None:
        self.seen.append(self.getvalue())
        super().flush()


async def test_plain_run_flushes_every_line_as_it_happens() -> None:
    """Through a pipe nothing used to appear until the run ended: stdout is block-buffered off
    a terminal, and the plain renderer never flushed."""

    output = FlushRecorder()
    await run_once(
        PlainBackend(),
        RunRequest("Build it", None, "", "normal", "ask"),
        stdout=output,
    )

    assert output.seen[0] == "· Building it.\n"
    assert output.seen[-1] == output.getvalue()


async def test_no_follow_flushes_the_run_id() -> None:
    output = FlushRecorder()
    await run_once(
        PlainBackend(),
        RunRequest("Build it", None, "", "normal", "ask"),
        stdout=output,
        follow=False,
    )

    assert output.seen == ["run-1\n"]


async def test_jsonl_rows_carry_the_event_id() -> None:
    """The README names `event_id` as the fallback identity for an approval or a question, so
    a JSONL consumer must be able to read it off the row."""

    output = StringIO()
    await run_once(
        ApprovalBackend(),
        RunRequest("Release it", None, "", "normal", "ask"),
        stdout=output,
        jsonl=True,
    )

    rows = [cast(JsonObject, orjson.loads(line)) for line in output.getvalue().splitlines()]
    assert rows[1]["event"] == "approval.requested"
    assert rows[1]["event_id"] == "evt-1"
    assert rows[1]["seq"] == 1


def test_newly_active_steps_accepts_any_sequence_that_is_not_text() -> None:
    """A plan is typed as a `Sequence`; one built in process or replayed from history arrives
    as a tuple, and used to be dropped for not being a list."""

    from orca.output.plain import _newly_active_steps  # pyright: ignore[reportPrivateUsage]

    plan = ({"step": "read the router", "status": "in_progress"},)
    assert _newly_active_steps({"plan": plan}, set()) == ["read the router"]
    assert _newly_active_steps({"plan": "read the router"}, set()) == []
