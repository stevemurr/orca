"""orca owns one local backend without confusing it with remote deployments."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from orca import entrypoint as cli
from orca.connection import Connection, CredentialSource
from orca.server_manager import LocalServerManager, can_manage, child_environment

STUB = Path(__file__).parent / "support" / "stub_backend.py"


@pytest.fixture(autouse=True)
def configured_server_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process management is configuration, so every case here has to supply it."""

    monkeypatch.setenv("ORCA_SERVER_COMMAND", f"{sys.executable} {STUB}")


def _connection(profile: str, endpoint: str) -> Connection:
    return Connection(
        profile=profile,
        endpoint=endpoint,
        credential_source=CredentialSource.NONE,
    )


def test_management_is_unavailable_until_a_launch_command_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """orca cannot guess how to start a harness, and does not pretend it can."""

    monkeypatch.delenv("ORCA_SERVER_COMMAND")

    assert not can_manage(_connection("default", "http://127.0.0.1:8420"))


def test_only_the_default_plaintext_loopback_profile_can_be_managed() -> None:
    assert can_manage(_connection("default", "http://127.0.0.1:8420"))
    assert can_manage(_connection("default", "http://localhost:8420"))
    assert can_manage(_connection("default", "http://[::1]:8420"))

    assert not can_manage(_connection("staging", "http://127.0.0.1:8420"))
    assert not can_manage(_connection("default", "https://127.0.0.1:8420"))
    assert not can_manage(_connection("default", "http://backend.example:8420"))


async def test_chat_lifecycle_check_does_not_probe_or_launch_a_remote_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalServerManager(_connection("work", "https://backend.example"))

    async def unexpected_probe():
        raise AssertionError("remote lifecycle must be left to its operator")

    monkeypatch.setattr(manager, "_probe", unexpected_probe)

    status = await manager.ensure()

    assert not status.running
    assert not status.managed


def test_server_commands_start_inspect_and_stop_one_background_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = cast(int, listener.getsockname()[1])
    endpoint = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("ORCA_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("ORCA_AUTH_TOKEN", "managed-test-token")
    runner = CliRunner()
    prefix = ["--url", endpoint, "server"]

    try:
        started = runner.invoke(cli.app, [*prefix, "start"])
        assert started.exit_code == 0, started.output
        assert "running" in started.output
        assert "managed" in started.output

        status = runner.invoke(cli.app, [*prefix, "status"])
        assert status.exit_code == 0, status.output
        assert "running" in status.output
        assert "managed" in status.output
    finally:
        stopped = runner.invoke(cli.app, [*prefix, "stop"])

    assert stopped.exit_code == 0, stopped.output
    assert "stopped" in stopped.output


def test_a_started_server_is_told_the_credential_by_the_name_it_reads() -> None:
    """The harness reads `HARNESS_TOKEN`. Forwarding the credential as `ORCA_AUTH_TOKEN`
    started a server that required no token while orca kept sending one."""
    connection = Connection(
        profile="default",
        endpoint="http://127.0.0.1:8420",
        token="secret",
        credential_source=CredentialSource.KEYRING,
    )
    environ = {
        "ORCA_SERVER_COMMAND": "harness serve",
        "ORCA_AUTH_TOKEN": "client-side",
        "ORCA_PROFILE": "default",
        "ORCA_CUSTOM": "kept",
        "PATH": "/usr/bin",
    }

    told = child_environment(connection, "inst-1", environ)

    assert told == {
        "ORCA_SERVER_COMMAND": "harness serve",
        "ORCA_CUSTOM": "kept",
        "HARNESS_TOKEN": "secret",
        "ORCA_MANAGED_INSTANCE_ID": "inst-1",
    }
