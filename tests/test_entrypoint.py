"""Public command surface for the greenfield CLI cutover."""

from __future__ import annotations

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
    assert "eval-run" not in output
    assert "compactions" not in output
    assert "graph" not in output


def test_chat_launches_the_view_app_with_resolved_options(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def launch(*, workspace: str, thread: str, profile: str | None, url: str | None) -> None:
        captured.update(
            workspace=workspace,
            thread=thread,
            profile=profile,
            url=url,
        )

    monkeypatch.setattr(entrypoint, "launch_tui", launch)
    result = CliRunner().invoke(
        entrypoint.app,
        [
            "--profile",
            "staging",
            "--url",
            "https://orch.example.test",
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
        "url": "https://orch.example.test",
    }


def test_default_command_launches_chat(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(entrypoint, "launch_tui", lambda **kwargs: calls.append(kwargs))

    result = CliRunner().invoke(entrypoint.app, [])

    assert result.exit_code == 0, result.output
    assert calls == [{"workspace": "", "thread": "", "profile": None, "url": None}]


def test_plain_run_propagates_the_input_required_exit_code(monkeypatch) -> None:
    class Backend:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def close(self) -> None:
            pass

    async def input_required(*_args, **_kwargs) -> int:
        return 2

    monkeypatch.setattr(entrypoint, "resolve_connection", lambda **_kwargs: object())
    monkeypatch.setattr(entrypoint, "HttpBackend", Backend)
    monkeypatch.setattr(entrypoint, "run_once", input_required)

    result = CliRunner().invoke(entrypoint.app, ["run", "Release it"])

    assert result.exit_code == 2


def test_full_screen_mode_fails_fast_without_a_terminal(monkeypatch) -> None:
    class Pipe:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(entrypoint.sys, "stdin", Pipe())
    monkeypatch.setattr(entrypoint.sys, "stdout", Pipe())
    monkeypatch.setattr(
        entrypoint,
        "resolve_connection",
        lambda **_kwargs: pytest.fail("a non-TTY must not start a connection"),
    )

    with pytest.raises(typer.Exit) as raised:
        entrypoint.launch_tui(workspace="", thread="", profile=None, url=None)

    assert raised.value.exit_code == 2
