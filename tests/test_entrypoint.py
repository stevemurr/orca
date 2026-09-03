"""The public command surface stays small."""

from __future__ import annotations

import sys

import pytest
import typer
from typer.testing import CliRunner

from orca import entrypoint


def normalized(value: str) -> str:
    return " ".join(value.split())


def test_root_help_is_the_small_product_surface() -> None:
    result = CliRunner().invoke(entrypoint.app, ["--help"])
    output = normalized(result.output)

    assert result.exit_code == 0, result.output
    assert "chat" in output
    assert "run" in output
    assert "threads" in output
    assert "auth" in output
    assert "server" in output
    # The whole surface, and no more of it. Everything a person can do lives behind these five
    # verbs or a slash command inside the shell; a sixth top-level verb is a design decision.
    assert "agents" not in output
    assert "graph" not in output
    assert "memory" not in output


def test_chat_launches_the_view_app_with_resolved_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def launch(
        *, workspace: str, thread: str, profile: str | None, url: str | None, resume: bool
    ) -> None:
        captured.update(
            workspace=workspace,
            thread=thread,
            profile=profile,
            url=url,
            resume=resume,
        )

    monkeypatch.setattr(entrypoint, "launch_tui", launch)
    result = CliRunner().invoke(
        entrypoint.app,
        [
            "--profile",
            "staging",
            "--url",
            "https://harness.example.test",
            "chat",
            "--workspace",
            "/tmp/project",
            "--thread",
            "thread-1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "workspace": "/tmp/project",
        "thread": "thread-1",
        "profile": "staging",
        "url": "https://harness.example.test",
        "resume": False,
    }


def test_default_command_launches_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def launch(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(entrypoint, "launch_tui", launch)

    result = CliRunner().invoke(entrypoint.app, [])

    assert result.exit_code == 0, result.output
    assert calls == [{"workspace": "", "thread": "", "profile": None, "url": None, "resume": False}]


def test_resume_opens_the_app_on_the_recent_conversations(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def launch(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(entrypoint, "launch_tui", launch)

    assert CliRunner().invoke(entrypoint.app, ["--resume"]).exit_code == 0
    assert CliRunner().invoke(entrypoint.app, ["chat", "-r"]).exit_code == 0
    assert [call["resume"] for call in calls] == [True, True]


def test_plain_run_propagates_the_input_required_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    class Backend:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def close(self) -> None:
            pass

    async def input_required(*_args: object, **_kwargs: object) -> int:
        return 2

    def any_connection(**_kwargs: object) -> object:
        return object()

    monkeypatch.setattr(entrypoint, "resolve_connection", any_connection)
    monkeypatch.setattr(entrypoint, "HttpBackend", Backend)
    monkeypatch.setattr(entrypoint, "run_once", input_required)

    result = CliRunner().invoke(entrypoint.app, ["run", "Release it"])

    assert result.exit_code == 2


def test_full_screen_mode_fails_fast_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class Pipe:
        @staticmethod
        def isatty() -> bool:
            return False

    def must_not_connect(**_kwargs: object) -> object:
        pytest.fail("a non-TTY must not start a connection")

    monkeypatch.setattr(sys, "stdin", Pipe())
    monkeypatch.setattr(sys, "stdout", Pipe())
    monkeypatch.setattr(entrypoint, "resolve_connection", must_not_connect)

    with pytest.raises(typer.Exit) as raised:
        entrypoint.launch_tui(workspace="", thread="", profile=None, url=None)

    assert raised.value.exit_code == 2
