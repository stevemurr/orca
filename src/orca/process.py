"""The one place orca ever creates a subprocess.

Vendored from the orchestrator's `tools/shell.py` when the terminal client was extracted
(2026-08-30). Two callers need it and nothing else does: `workspace_context` runs one bounded
`git rev-list` to identify a checkout, and `server_manager` starts, checks, and signals a local
backend process. The agent tool layer it came from — the 2 MiB output cap, the on-disk output
sink, the descendant process-group walk that survives `uv run`-style launchers, and the
shutdown hook that delivers pending SIGKILLs early — was left behind with the server, because
nothing here spawns a tree it does not control.

Three things here are load-bearing and easy to get subtly wrong:

* **`start_new_session=True`.** It does two jobs. The child becomes a session and process
  group leader, so `pgid == pid` and `os.killpg` can take down a whole process tree; and it
  drops the controlling terminal, so a tool that wants credentials cannot bypass our closed
  stdin by opening `/dev/tty` and hanging forever.

* **`read(n)`, never `readline()`.** A single minified-JS line or a base64 blob in output
  exceeds asyncio's stream limit and raises `LimitOverrunError` — a crash caused entirely by
  the shape of someone else's log line.

* **Killing without awaiting the kill.** `asyncio.CancelledError` fires at the next await
  point, so a cleanup path that awaits `proc.wait()` gets cancelled too and leaves the
  process running. SIGTERM is issued synchronously and escalation is handed to a detached
  task. (found 2026-08-17)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

#: Bytes kept in memory from the start and end of a process's output.
HEAD_LIMIT = 256 * 1024
TAIL_LIMIT = 256 * 1024

#: What a caller sees by default. Tail-weighted because summary lines live at the end, while
#: the head is usually a banner.
RENDER_HEAD = 3000
RENDER_TAIL = 5000

_ESCALATION_DELAY_S = 5.0

#: Detached kill-escalation tasks. Held in a module-level set because asyncio only keeps a
#: weak reference to running tasks and will happily garbage-collect one mid-flight.
_escalations: set[asyncio.Task[None]] = set()

_login_path: str | None = None


class ProcessError(RuntimeError):
    pass


@dataclass
class ProcessResult:
    argv: list[str]
    exit_code: int | None
    duration_ms: int
    head: bytes
    tail: bytes
    total_bytes: int
    #: None, "timeout", or "cancel". Distinct from a non-zero exit code: the caller must be
    #: able to tell "your command failed" from "we killed your command".
    killed_by: str | None = None
    pgid: int | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.killed_by is None

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self.head) + len(self.tail)

    def render(self, max_head: int = RENDER_HEAD, max_tail: int = RENDER_TAIL) -> str:
        """Head, an explicit elision marker, then tail.

        `head` and `tail` are disjoint by construction (see `_pump`), so concatenating them
        cannot duplicate a byte — the marker between them is the only thing that is ever
        missing.

        Disjointness is also why the tail has to be *reconstituted* from `head` when the tail
        buffer is empty. `_pump` fills `head` to HEAD_LIMIT before routing a byte to `tail`, so
        every process producing 256KiB or less leaves `tail` empty — which is almost all of
        them. Taking the tail as literally `self.tail` therefore returned the first 3000 bytes
        and nothing else for the entire range where real output lives. (found 2026-08-17)
        """
        head_b = self.head[:max_head]
        tail_b = self.tail[-max_tail:] if self.tail else self.head[max_head:][-max_tail:]
        elided = max(0, self.total_bytes - len(head_b) - len(tail_b))
        head = head_b.decode("utf-8", "replace")
        tail = tail_b.decode("utf-8", "replace")
        if elided == 0:
            return head + tail
        return f"{head}\n...[{elided} bytes elided]...\n{tail}"


@dataclass
class ProcessSpec:
    argv: list[str]
    cwd: Path
    timeout_s: float = 600.0
    #: Extra environment on top of the constructed base. Use sparingly; the base exists to
    #: make behaviour reproducible.
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DetachedProcess:
    """Receipt for a background process whose lifecycle belongs to another component."""

    pid: int
    pgid: int


def start_detached(
    argv: list[str],
    *,
    cwd: Path,
    output_path: Path,
    env_overrides: dict[str, str] | None = None,
) -> DetachedProcess:
    """Start a background process without tying it to the caller's terminal.

    This is intentionally a small primitive, not a daemon manager. The caller must persist
    an ownership receipt and verify it before calling :func:`stop_detached`.
    """
    if not argv:
        raise ProcessError("argv must not be empty")
    cwd.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stream = output_path.open("ab", buffering=0)
    # Both flags are accepted on every platform; each is a no-op where it does not apply.
    windows = os.name == "nt"
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=build_env(env_overrides),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
            start_new_session=not windows,
        )
    except FileNotFoundError as exc:
        raise ProcessError(f"executable not found: {argv[0]}") from exc
    finally:
        stream.close()
    return DetachedProcess(pid=proc.pid, pgid=proc.pid)


def process_exists(pid: int) -> bool:
    """Whether a recorded process id still exists."""
    if pid <= 0:
        return False
    if os.name != "nt":
        # Usually a later CLI invocation is not the parent and waitpid reports that plainly.
        # In an embedded CLI/test process it *is* the parent; reaping a finished child here
        # avoids treating its zombie receipt as a live server until the stop deadline.
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            if reaped == pid:
                return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def stop_detached(process: DetachedProcess, *, force: bool = False) -> None:
    """Signal a previously verified background process group."""
    selected = signal.SIGKILL if force and hasattr(signal, "SIGKILL") else signal.SIGTERM
    if os.name == "nt":
        selected = signal.SIGTERM if force else signal.CTRL_BREAK_EVENT
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(process.pid, selected)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pgid, selected)


def login_path() -> str:
    """PATH as the user's login shell sees it.

    A GUI-launched or systemd-launched process inherits a minimal PATH, so `uv`, `git`, and
    Homebrew binaries are simply absent and every call fails with a confusing
    "command not found". Resolved once per process.

    Blocking, and the only blocking spawn in the tree: a login shell sources the user's rc
    files, which is 0.7s with a light zshrc and seconds with nvm/pyenv/conda. Callers on the
    event loop must therefore pre-warm it off-loop. Left lazy it froze the entire API on the
    first client-triggered subprocess. (found 2026-08-17)
    """
    global _login_path
    if _login_path is None:
        try:
            out = subprocess.run(
                [os.environ.get("SHELL", "/bin/zsh"), "-lic", "echo $PATH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                # Same reasons as every other spawn here. `-i` means job control: sharing our
                # controlling terminal, the shell can be SIGTTOU-stopped in a background
                # process group and then sit there for the whole 10s, and anything its rc files
                # source can prompt on /dev/tty. Its own session with no terminal and closed
                # stdin removes both.
                start_new_session=True,
                stdin=subprocess.DEVNULL,
            )
            _login_path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        except (OSError, subprocess.SubprocessError, IndexError):
            _login_path = ""
        if not _login_path:
            _login_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return _login_path


def build_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """A deliberately constructed environment, not an inherited one.

    Inheriting orca's environment hands every child our own secrets and lets ambient config
    change behaviour between invocations. The entries below all exist to stop a command
    blocking on something interactive: there is no terminal to type into, so a prompt is an
    indefinite hang rather than a question.
    """
    env = {
        "PATH": login_path(),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        # Never prompt.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/true",
        "SSH_ASKPASS": "/usr/bin/true",
        "GIT_EDITOR": "true",
        "EDITOR": "true",
        "VISUAL": "true",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "npm_config_yes": "true",
        # Stable, parseable output.
        "CI": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
    }
    if overrides:
        env.update(overrides)
    return {k: v for k, v in env.items() if v != ""}


def _begin_kill(pgid: int | None, pid: int | None) -> None:
    """Signal the process group now, synchronously, and escalate in the background.

    Synchronous because this runs from a `finally` that may itself be unwinding a
    cancellation: any `await` here could be the one that never resumes.
    """
    if pgid is None and pid is None:
        return
    target = pgid if pgid is not None else pid
    assert target is not None
    if target <= 0:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(target, signal.SIGTERM)

    async def _escalate() -> None:
        await asyncio.sleep(_ESCALATION_DELAY_S)
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(target, signal.SIGKILL)

    with contextlib.suppress(RuntimeError):  # no running loop during interpreter shutdown
        task = asyncio.get_running_loop().create_task(_escalate())
        _escalations.add(task)
        task.add_done_callback(_escalations.discard)


async def run_process(spec: ProcessSpec) -> ProcessResult:
    """Run a command to completion, capturing bounded output.

    Never raises for a non-zero exit — that is a result, not an error. Raises only if the
    process could not be started at all.
    """
    if not spec.argv:
        raise ProcessError("argv must not be empty")

    started = time.monotonic()
    env = build_env(spec.env_overrides)

    try:
        proc = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=str(spec.cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            # Merged so interleaving is preserved: a traceback split across two pipes
            # reassembles in the wrong order and reads as two unrelated failures.
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ProcessError(f"executable not found: {spec.argv[0]}") from exc

    # start_new_session makes the child its own group leader, so this is exact.
    pgid = proc.pid
    head = bytearray()
    tail = bytearray()
    total = 0
    killed_by: str | None = None

    async def _pump() -> None:
        nonlocal total
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            total += len(chunk)
            # Fill head first, and only then start the sliding tail, so the two buffers are
            # disjoint. Feeding every chunk to both would double-count short outputs, where
            # head and tail hold the same bytes.
            rest = chunk
            if len(head) < HEAD_LIMIT:
                take = HEAD_LIMIT - len(head)
                head.extend(chunk[:take])
                rest = chunk[take:]
            if rest:
                tail.extend(rest)
                if len(tail) > TAIL_LIMIT:
                    del tail[: len(tail) - TAIL_LIMIT]

    try:
        try:
            await asyncio.wait_for(_pump(), timeout=spec.timeout_s)
        except TimeoutError:
            killed_by = "timeout"
            _begin_kill(pgid, proc.pid)
        # Reap. Bounded, because the group has already been signalled on every path that
        # gets here without the process having exited on its own.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_ESCALATION_DELAY_S + 2)
    except asyncio.CancelledError:
        killed_by = "cancel"
        _begin_kill(pgid, proc.pid)
        raise
    finally:
        if proc.returncode is None:
            _begin_kill(pgid, proc.pid)

    return ProcessResult(
        argv=list(spec.argv),
        exit_code=proc.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        head=bytes(head),
        tail=bytes(tail),
        total_bytes=total,
        killed_by=killed_by,
        pgid=pgid,
    )
