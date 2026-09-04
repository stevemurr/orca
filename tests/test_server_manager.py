"""orca owns one local backend without confusing it with remote deployments."""

from __future__ import annotations

import asyncio
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from orca import entrypoint as cli
from orca.connection import Connection, CredentialSource
from orca.process import process_exists
from orca.server_manager import (
    LocalServerManager,
    ManagedServerError,
    _launch_argv,  # pyright: ignore[reportPrivateUsage]
    _Receipt,  # pyright: ignore[reportPrivateUsage]
    can_manage,
    child_environment,
)

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


def test_launch_argv_fills_only_the_two_named_tokens() -> None:
    """`str.format` on every item raised `KeyError` for any other brace: a JSON literal or a
    regex in the command could not be started at all. (found 2026-09-04)"""
    argv = ["serve", "--bind", "{host}:{port}", "--opts", '{"a":1}', "--match", "^a{2,3}$"]

    assert _launch_argv(argv, host="127.0.0.1", port=8420) == [
        "serve",
        "--bind",
        "127.0.0.1:8420",
        "--opts",
        '{"a":1}',
        "--match",
        "^a{2,3}$",
    ]
    assert _launch_argv(["serve"], host="::1", port=1) == ["serve", "--host", "::1", "--port", "1"]


def _long_lived() -> subprocess.Popen[bytes]:
    """A process standing in for a backend orca started earlier; its own session, like one."""
    return subprocess.Popen(["sleep", "30"], start_new_session=True)


def _orphaned_worker() -> tuple[int, int]:
    """A group whose leader forked a worker and exited: `(leader pid, worker pid)`."""
    leader: subprocess.Popen[str] = subprocess.Popen(
        ["sh", "-c", "sleep 30 & echo $!; exit 0"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    with leader:
        assert leader.stdout is not None
        # `Popen.stdout` is typed `IO[Any]` whatever the text mode, so the read is cast.
        worker = int(cast(str, leader.stdout.read()).strip())
    return leader.pid, worker


def _manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalServerManager:
    monkeypatch.setenv("ORCA_CONFIG_HOME", str(tmp_path / "config"))
    manager = LocalServerManager(_connection("default", "http://localhost:9911"))

    async def unreachable() -> None:
        return None

    # A health probe that never gets through: the transient miss every case here is about.
    monkeypatch.setattr(manager, "_probe", unreachable)
    return manager


async def test_a_live_but_unhealthy_server_is_not_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A transient probe miss read as "not running", so `start()` launched a second server,
    wrote its receipt over the first one's, and on failure deleted it -- leaving the live
    original with no receipt and nothing able to stop it. (found 2026-09-04)"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_SERVER_COMMAND", "sh -c 'exit 1'")
    original = _long_lived()
    try:
        receipt = _Receipt(
            endpoint=manager.connection.endpoint,
            instance_id="original",
            pid=original.pid,
            pgid=original.pid,
            started_at="2026-09-04T00:00:00+00:00",
        )
        manager._write_receipt(receipt)  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(ManagedServerError, match=f"pid {original.pid} still exists"):
            _ = await manager.start(timeout_s=1)

        assert manager._read_receipt() == receipt  # pyright: ignore[reportPrivateUsage]
        assert original.poll() is None
    finally:
        original.kill()
        _ = original.wait()


async def test_a_failed_start_signals_the_whole_group_after_the_leader_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A launcher that forks a worker and exits leaves the worker in the leader's group.
    Signalling only when the leader was alive left that worker holding the port."""
    manager = _manager(monkeypatch, tmp_path)
    marker = tmp_path / "worker.pid"
    monkeypatch.setenv(
        "ORCA_SERVER_COMMAND",
        f"sh -c 'sleep 30 & echo $! > {shlex.quote(str(marker))}; exit 0'",
    )

    with pytest.raises(ManagedServerError, match="did not start"):
        _ = await manager.start(timeout_s=1)

    worker = int(marker.read_text().strip())
    # SIGKILL is delivered asynchronously; give the kernel a moment before checking.
    for _ in range(50):
        if not process_exists(worker):
            break
        await asyncio.sleep(0.02)
    assert not process_exists(worker)
    assert not manager.state_path.exists()


async def test_stop_signals_the_group_when_only_the_leader_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _manager(monkeypatch, tmp_path)
    leader, worker = _orphaned_worker()
    manager._write_receipt(  # pyright: ignore[reportPrivateUsage]
        _Receipt(
            endpoint=manager.connection.endpoint,
            instance_id="orphaned",
            pid=leader,
            pgid=leader,
            started_at="2026-09-04T00:00:00+00:00",
        )
    )

    status = await manager.stop(timeout_s=1)

    assert not status.running
    for _ in range(50):
        if not process_exists(worker):
            break
        await asyncio.sleep(0.02)
    assert not process_exists(worker)


async def test_a_missing_executable_is_a_managed_server_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`start_detached` raised `ProcessError`, which nothing on the CLI path caught, so a
    typo in `ORCA_SERVER_COMMAND` was a traceback rather than a sentence. (found 2026-09-04)"""
    manager = _manager(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCA_SERVER_COMMAND", "/nonexistent/harness serve")

    with pytest.raises(ManagedServerError, match="executable not found: /nonexistent/harness"):
        _ = await manager.start(timeout_s=1)


def test_the_cli_reports_a_missing_executable_as_a_sentence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ORCA_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("ORCA_SERVER_COMMAND", "/nonexistent/harness serve")

    result = CliRunner().invoke(cli.app, ["--url", "http://localhost:9913", "server", "start"])

    assert result.exit_code == 1
    assert "executable not found: /nonexistent/harness" in result.output
    assert "Traceback" not in result.output
