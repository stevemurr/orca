"""Own the optional local backend process orca was told how to start.

Remote endpoints are never process-managed. A local server also remains usable when it was
started by a person or another service; orca only stops processes carrying its persisted
instance identity.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self, cast
from urllib.parse import urlsplit

import httpx

from orca.connection import (
    CONFIG_HOME_ENV,
    DEFAULT_PROFILE,
    PROFILE_ENV,
    TOKEN_ENV,
    TOKEN_FILE_ENV,
    URL_ENV,
    Connection,
)
from orca.json_types import JsonValue
from orca.process import (
    DetachedProcess,
    ProcessError,
    process_exists,
    start_detached,
    stop_detached,
)

API_PREFIX = "/api/v1"

#: How to start the local backend. orca cannot know this — it is the one fact about a harness
#: that is not on the wire — so it is configuration, and without it local process management is
#: simply unavailable rather than guessed at. `{host}` and `{port}` are substituted where they
#: appear; a command that names neither gets `--host <host> --port <port>` appended, which is
#: what uvicorn-shaped servers want.
SERVER_COMMAND_ENV = "ORCA_SERVER_COMMAND"

#: The instance identity orca mints for a server it started and expects back from `/health` as
#: `detail.managed_instance_id`. It is an ownership marker, not authority: without it orca will
#: not signal a process, because it cannot prove the process at that port is the one it started.
INSTANCE_ID_ENV = "ORCA_MANAGED_INSTANCE_ID"

#: How the credential reaches a server orca starts. The harness reads `HARNESS_TOKEN` (or a
#: `--token` flag, which would put the secret in the process table), and nothing else; until
#: 2026-09-03 orca forwarded it under its own client name, which the harness never read, so a
#: profile with a credential started a server that required none.
MANAGED_TOKEN_ENV = "HARNESS_TOKEN"

_START_TIMEOUT_S = 15.0
_STOP_TIMEOUT_S = 15.0
_POLL_S = 0.1


class ManagedServerError(RuntimeError):
    """The CLI could not safely complete a local server lifecycle operation."""


@dataclass(frozen=True)
class ManagedServerStatus:
    endpoint: str
    running: bool
    managed: bool
    pid: int | None = None
    started_here: bool = False
    health: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class _Receipt:
    endpoint: str
    instance_id: str
    pid: int
    pgid: int
    started_at: str

    @classmethod
    def from_json(cls, value: JsonValue) -> _Receipt | None:
        if not isinstance(value, Mapping):
            return None
        try:
            receipt = cls(
                endpoint=str(value["endpoint"]),
                instance_id=str(value["instance_id"]),
                pid=_as_int(value["pid"]),
                pgid=_as_int(value["pgid"]),
                started_at=str(value["started_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if receipt.pid <= 0 or receipt.pgid <= 0 or not receipt.instance_id:
            return None
        return receipt


@dataclass(frozen=True)
class _Probe:
    status_code: int
    health: str | None = None
    instance_id: str = ""


def server_command(environ: Mapping[str, str] | None = None) -> list[str] | None:
    """The configured launch command, or None when this machine has not named one."""
    raw = (os.environ if environ is None else environ).get(SERVER_COMMAND_ENV, "").strip()
    if not raw:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError:
        return None
    return argv or None


def child_environment(
    connection: Connection,
    instance_id: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """What a server orca starts is told, on top of the constructed base environment.

    `ORCA_*` is the whole forwarding rule: the child gets a constructed environment, so a
    backend reads its own configuration from variables an operator deliberately exported
    under that prefix. The client-only keys are removed because they describe how *this
    process* found the server, not how the server should run. The credential goes out under
    the name the harness reads, and the instance id under the name it echoes.
    """
    source = os.environ if environ is None else environ
    forwarded = {key: value for key, value in source.items() if key.startswith("ORCA_")}
    for client_key in (CONFIG_HOME_ENV, PROFILE_ENV, URL_ENV, TOKEN_ENV, TOKEN_FILE_ENV):
        forwarded.pop(client_key, None)
    if connection.token:
        forwarded[MANAGED_TOKEN_ENV] = connection.token
    forwarded[INSTANCE_ID_ENV] = instance_id
    return forwarded


def _launch_argv(argv: list[str], *, host: str, port: int) -> list[str]:
    """The configured command with `{host}` and `{port}` filled in.

    Only those two literal tokens, by plain replacement: `str.format` treated every brace in
    every item as a placeholder, so a command carrying a JSON literal (`{"a":1}`) or a regex
    (`^a{2,3}$`) raised `KeyError` before anything was started. (found 2026-09-04)
    """
    if any("{host}" in item or "{port}" in item for item in argv):
        return [item.replace("{host}", host).replace("{port}", str(port)) for item in argv]
    return [*argv, "--host", host, "--port", str(port)]


def _signal_group(pgid: int, *, force: bool) -> None:
    """Signal a recorded process group whether or not its leader is still alive.

    Processes are started with `start_new_session=True`, so the group id is the leader's pid
    and outlives the leader: a launcher that forks a worker and exits leaves the worker in
    the group. Checking `process_exists(pid)` first, as `start()` and `stop()` used to, meant
    that worker was never signalled and kept the port. `stop_detached` already suppresses
    the "no such group" case, so this is safe to call blind. (found 2026-09-04)
    """
    stop_detached(DetachedProcess(pid=pgid, pgid=pgid), force=force)


def can_manage(connection: Connection) -> bool:
    """Whether the connection names the one kind of process orca may own."""
    if server_command() is None:
        return False
    parts = urlsplit(connection.endpoint)
    hostname = (parts.hostname or "").rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        loopback = True
    else:
        try:
            import ipaddress

            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    return (
        connection.profile == DEFAULT_PROFILE
        and parts.scheme == "http"
        and parts.path in {"", "/"}
        and not parts.query
        and not parts.fragment
        and loopback
    )


def _as_int(value: JsonValue) -> int:
    if isinstance(value, (int, float, str)):
        return int(value)
    raise ValueError(f"not a number: {value!r}")


def _management_home() -> Path:
    configured = os.environ.get(CONFIG_HOME_ENV, "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".orca"


class _LifecycleLock:
    """A small cross-process lock around state inspection and process creation."""

    def __init__(self, path: Path, timeout_s: float = _START_TIMEOUT_S) -> None:
        self.path: Path = path
        self.timeout_s: float = timeout_s
        self._stream: BinaryIO | None = None

    async def __aenter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        if os.name == "nt":
            self._stream.seek(0, os.SEEK_END)
            if self._stream.tell() == 0:
                self._stream.write(b"0")
                self._stream.flush()
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise ManagedServerError("timed out waiting for another CLI server command")
                await asyncio.sleep(_POLL_S)

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._stream is None:
            return
        if os.name == "nt":
            import msvcrt

            self._stream.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None


class LocalServerManager:
    def __init__(self, connection: Connection) -> None:
        self.connection: Connection = connection
        self.home: Path = _management_home()
        self.runtime: Path = self.home / "cli"
        self.state_path: Path = self.runtime / "managed-server.json"
        self.log_path: Path = self.runtime / "server.log"
        self.lock_path: Path = self.runtime / "server.lock"

    def _require_eligible(self) -> None:
        if not can_manage(self.connection):
            if server_command() is None:
                raise ManagedServerError(
                    f"set {SERVER_COMMAND_ENV} to the command that starts your backend before "
                    + "asking orca to manage it"
                )
            raise ManagedServerError(
                "only the default local http://localhost profile can be process-managed"
            )

    async def _probe(self) -> _Probe | None:
        headers = (
            {"Authorization": f"Bearer {self.connection.token}"} if self.connection.token else {}
        )
        try:
            async with httpx.AsyncClient(timeout=1.5, trust_env=False, headers=headers) as client:
                response = await client.get(
                    self.connection.endpoint.rstrip("/") + API_PREFIX + "/health"
                )
        except httpx.RequestError:
            return None
        if response.status_code != 200:
            return _Probe(status_code=response.status_code)
        try:
            body = cast(JsonValue, response.json())
        except ValueError:
            return _Probe(status_code=response.status_code)
        if not isinstance(body, Mapping):
            return _Probe(status_code=response.status_code)
        detail = body.get("detail")
        return _Probe(
            status_code=response.status_code,
            health=str(body.get("status")),
            instance_id=str(detail.get("managed_instance_id", ""))
            if isinstance(detail, Mapping)
            else "",
        )

    def _read_receipt(self) -> _Receipt | None:
        try:
            recorded = cast(JsonValue, json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return _Receipt.from_json(recorded)

    def _write_receipt(self, receipt: _Receipt) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".managed-server-", dir=self.runtime)
        temporary = Path(name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(asdict(receipt), stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _remove_receipt(self, instance_id: str) -> None:
        receipt = self._read_receipt()
        if receipt is not None and receipt.instance_id == instance_id:
            self.state_path.unlink(missing_ok=True)

    async def status(self) -> ManagedServerStatus:
        probe = await self._probe()
        receipt = self._read_receipt()
        if probe is None:
            return ManagedServerStatus(
                endpoint=self.connection.endpoint,
                running=False,
                managed=False,
                pid=receipt.pid if receipt and process_exists(receipt.pid) else None,
            )
        if probe.status_code != 200:
            return ManagedServerStatus(
                endpoint=self.connection.endpoint,
                running=True,
                managed=False,
                detail=f"health endpoint returned HTTP {probe.status_code}",
            )
        managed = bool(
            receipt
            and receipt.endpoint == self.connection.endpoint
            and receipt.instance_id == probe.instance_id
            and process_exists(receipt.pid)
        )
        return ManagedServerStatus(
            endpoint=self.connection.endpoint,
            running=True,
            managed=managed,
            pid=receipt.pid if managed and receipt else None,
            health=probe.health,
            detail="managed by this CLI" if managed else "running outside CLI management",
        )

    async def ensure(self) -> ManagedServerStatus:
        if not can_manage(self.connection):
            # This is a lifecycle gate, not a second network request. The normal client call
            # that follows owns remote reachability and error reporting; process management
            # must leave named and remote deployments completely untouched.
            return ManagedServerStatus(
                endpoint=self.connection.endpoint,
                running=False,
                managed=False,
                detail="not eligible for local process management",
            )
        current = await self.status()
        return current if current.running else await self.start()

    async def start(self, *, timeout_s: float = _START_TIMEOUT_S) -> ManagedServerStatus:
        self._require_eligible()
        async with _LifecycleLock(self.lock_path):
            current = await self.status()
            if current.running:
                return current
            # A receipt whose process is still alive is a server, whether or not the probe
            # got through to it just now. Launching another here overwrote that receipt with
            # the new instance's, and the failure path then deleted it -- so a transient
            # health miss orphaned a live server orca could no longer stop. (found 2026-09-04)
            recorded = self._read_receipt()
            if (
                recorded is not None
                and recorded.endpoint == self.connection.endpoint
                and process_exists(recorded.pid)
            ):
                raise ManagedServerError(
                    f"a server with pid {recorded.pid} still exists but is not healthy; "
                    + f"wait for it, or `orca server stop` it, then inspect {self.log_path}"
                )

            parts = urlsplit(self.connection.endpoint)
            host = parts.hostname or "127.0.0.1"
            port = parts.port or 80
            instance_id = uuid.uuid4().hex
            argv = server_command()
            if argv is None:
                raise ManagedServerError(
                    f"set {SERVER_COMMAND_ENV} to the command that starts your backend before "
                    + "asking orca to manage it"
                )
            try:
                process = await asyncio.to_thread(
                    start_detached,
                    _launch_argv(argv, host=host, port=port),
                    cwd=self.runtime,
                    output_path=self.log_path,
                    env_overrides=child_environment(self.connection, instance_id),
                )
            except ProcessError as exc:
                # A missing executable is a configuration mistake, and the CLI reports those
                # as `ManagedServerError`; left as `ProcessError` it was a traceback.
                raise ManagedServerError(str(exc)) from exc
            receipt = _Receipt(
                endpoint=self.connection.endpoint,
                instance_id=instance_id,
                pid=process.pid,
                pgid=process.pgid,
                started_at=datetime.now(UTC).isoformat(),
            )
            self._write_receipt(receipt)

            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                probe = await self._probe()
                if probe is not None and probe.instance_id == instance_id:
                    return ManagedServerStatus(
                        endpoint=self.connection.endpoint,
                        running=True,
                        managed=True,
                        pid=receipt.pid,
                        started_here=True,
                        health=probe.health,
                        detail="managed by this CLI",
                    )
                if not process_exists(receipt.pid):
                    break
                await asyncio.sleep(_POLL_S)

            _signal_group(process.pgid, force=True)
            self._remove_receipt(instance_id)
            detail = self._log_tail()
            suffix = f"\n{detail}" if detail else f"; see {self.log_path}"
            raise ManagedServerError(f"the local backend did not start{suffix}")

    async def stop(self, *, timeout_s: float = _STOP_TIMEOUT_S) -> ManagedServerStatus:
        self._require_eligible()
        async with _LifecycleLock(self.lock_path):
            receipt = self._read_receipt()
            if receipt is None:
                current = await self.status()
                if current.running:
                    raise ManagedServerError(
                        "the local server is running but was not started by this CLI"
                    )
                return current

            probe = await self._probe()
            if probe is None:
                if not process_exists(receipt.pid):
                    # The leader is gone, but the group it led may not be.
                    _signal_group(receipt.pgid, force=True)
                    self._remove_receipt(receipt.instance_id)
                    return ManagedServerStatus(self.connection.endpoint, False, False)
                raise ManagedServerError(
                    "the recorded server is unreachable, so its ownership cannot be verified; "
                    + f"inspect {self.log_path}"
                )
            if probe.instance_id != receipt.instance_id:
                raise ManagedServerError(
                    "the server at this address is not the process recorded by the CLI"
                )

            process = DetachedProcess(pid=receipt.pid, pgid=receipt.pgid)
            stop_detached(process)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if await self._probe() is None and not process_exists(receipt.pid):
                    self._remove_receipt(receipt.instance_id)
                    return ManagedServerStatus(self.connection.endpoint, False, False)
                await asyncio.sleep(_POLL_S)
            stop_detached(process, force=True)
            self._remove_receipt(receipt.instance_id)
            return replace(
                ManagedServerStatus(self.connection.endpoint, False, False),
                detail="forced the local server to stop after its graceful deadline",
            )

    async def restart(self) -> ManagedServerStatus:
        current = await self.status()
        if current.running:
            await self.stop()
        return await self.start()

    def _log_tail(self) -> str:
        try:
            data = self.log_path.read_bytes()[-16_384:].decode("utf-8", "replace")
        except OSError:
            return ""
        lines = [line for line in data.splitlines() if line.strip()][-12:]
        return "\n".join(lines)


async def ensure_local_server(connection: Connection) -> ManagedServerStatus:
    """Ensure an eligible local server exists; leave every other endpoint untouched."""
    return await LocalServerManager(connection).ensure()
