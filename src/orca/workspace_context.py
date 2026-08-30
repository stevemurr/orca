"""Resolve the folder an orca session actually operates in.

The backend owns workspace policy and execution.  The client owns the local launch context: it
is the only side that knows which directory the human was standing in and which relative path
they typed.  Keeping that discovery here prevents the presentation layer from inventing a
prettier path than the one attached to the run.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from orca.client import ApiError
from orca.process import ProcessError, ProcessSpec, run_process


class WorkspaceContextError(ValueError):
    """The requested local context cannot be made into a workspace binding."""


class WorkspaceClient(Protocol):
    async def list_workspaces(self) -> list[dict[str, Any]]: ...

    async def create_workspace(
        self,
        name: str,
        root_path: str,
        config: dict | None = None,
        vcs: str | None = None,
        replace_existing: bool = False,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class LocalWorkspace:
    """A local project root plus the directory the human is focused on inside it."""

    root: Path
    active_directory: Path
    cwd_relative: str
    vcs: Literal["git", "none"]


@dataclass(frozen=True)
class WorkspaceBinding:
    """A local context bound to the server record that a run must name."""

    workspace_id: str
    name: str
    root: Path
    active_directory: Path
    cwd_relative: str
    vcs: Literal["git", "none"]


def discover_local_workspace(path: Path) -> LocalWorkspace:
    """Find the nearest normal Git checkout, or use an ordinary folder exactly.

    A home directory that happens to contain ``.git`` is not inherited by every folder below
    it.  That special case prevents an unrelated launch from turning the user's entire home
    into the run root.  A real checkout below home still wins because it is encountered first.
    """

    active = path.expanduser().resolve()
    if active.is_file():
        active = active.parent
    if not active.is_dir():
        raise WorkspaceContextError(f"{active} is not a directory")

    home = Path.home().resolve()
    for candidate in (active, *active.parents):
        if _looks_like_bare_repository(candidate):
            raise WorkspaceContextError(
                f"{candidate} is a bare Git repository and cannot be used as a working folder"
            )
        if candidate == home and active != home:
            break
        marker = candidate / ".git"
        if marker.is_dir() or marker.is_file():
            relative = active.relative_to(candidate)
            return LocalWorkspace(
                root=candidate,
                active_directory=active,
                cwd_relative=relative.as_posix() or ".",
                vcs="git",
            )

    return LocalWorkspace(
        root=active,
        active_directory=active,
        cwd_relative=".",
        vcs="none",
    )


def explicit_external_path(message: str, base_directory: Path, current_root: Path) -> Path | None:
    """Return the first existing external path the human explicitly wrote.

    Only path-shaped tokens are considered.  Project names mentioned in prose and paths the
    model might discover later do not widen access.  A file reference selects its containing
    directory, allowing requests such as ``edit ../project-b/README.md`` to attach project B.
    """

    try:
        tokens = shlex.split(message)
    except ValueError:
        tokens = message.split()

    base = base_directory.expanduser().resolve()
    root = current_root.expanduser().resolve()
    for raw in tokens:
        token = raw.strip("`'\"()[]{}<>,;:!?")
        token = token.rstrip(".") if token not in {".", ".."} else token
        if not token or not _looks_like_path(token):
            continue
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        candidate = candidate.resolve()
        if not candidate.exists():
            continue
        directory = candidate if candidate.is_dir() else candidate.parent
        if directory == root or directory.is_relative_to(root):
            continue
        return directory
    return None


def _looks_like_path(token: str) -> bool:
    return token.startswith(("./", "../", "~/", "/"))


def _looks_like_bare_repository(path: Path) -> bool:
    return (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir()


async def _repo_identity(root: Path) -> str:
    """Compute the clone-stable Git identity without importing any backend workspace policy.

    Local folder discovery belongs to the client because only it can see the directory from
    which it was launched.  The HTTP-only boundary still matters, though: reaching into a
    backend's own workspace module would couple the portable executable to its internals.
    Root commits are the complete wire fact needed here, so obtain them through the one process
    primitive and keep the policy decision on the backend side.
    """

    try:
        result = await run_process(
            ProcessSpec(
                argv=["git", "rev-list", "--max-parents=0", "HEAD"],
                cwd=root,
                timeout_s=10,
            )
        )
    except ProcessError as exc:
        raise WorkspaceContextError(
            f"Could not verify the Git repository currently at {root}: {exc}"
        ) from exc
    if not result.ok:
        detail = result.render(max_head=1_000, max_tail=1_000).strip()
        if result.killed_by:
            detail = f"git was stopped ({result.killed_by})" + (f": {detail}" if detail else "")
        elif not detail:
            detail = f"git exited with status {result.exit_code}"
        raise WorkspaceContextError(
            f"Could not verify the Git repository currently at {root}: {detail}"
        )
    roots = sorted(
        line.strip()
        for line in (result.head + result.tail).decode("utf-8", "replace").splitlines()
        if line.strip()
    )
    if not roots:
        raise WorkspaceContextError(
            f"Could not verify the Git repository currently at {root}: repository has no commits"
        )
    return ",".join(roots)


async def resolve_workspace_binding(
    client: WorkspaceClient,
    *,
    selector: str = "",
    path: Path | None = None,
) -> WorkspaceBinding:
    """Resolve a name/id or ensure an exact binding for a local path.

    Registered ancestors are intentionally ignored.  They describe another authority root and
    were the source of the CLI's most dangerous visual lie: showing the launch directory while
    submitting a run against a broad workspace elsewhere.
    """

    workspaces = await client.list_workspaces()
    if selector:
        for workspace in workspaces:
            if selector in (workspace.get("workspace_id"), workspace.get("name")):
                return _binding_from_record(workspace)

    target = path
    if target is None:
        target = Path(selector).expanduser() if selector else Path.cwd()
    if selector and not target.exists():
        raise WorkspaceContextError(
            f"No workspace matches {selector!r}; it is not an existing directory"
        )

    local = discover_local_workspace(target)
    for workspace in workspaces:
        try:
            registered = Path(str(workspace["root_path"])).expanduser().resolve()
        except (KeyError, OSError):
            continue
        if registered == local.root:
            return await _reuse_or_replace_exact_binding(client, workspace, local)

    try:
        created = await client.create_workspace(
            local.root.name or str(local.root),
            str(local.root),
            vcs=local.vcs,
        )
    except ApiError as exc:
        # Another client may have registered the same exact root between list and create.
        if exc.status != 409:
            raise
        for workspace in await client.list_workspaces():
            if Path(str(workspace.get("root_path", ""))).expanduser().resolve() == local.root:
                return await _reuse_or_replace_exact_binding(client, workspace, local)
        raise
    return _binding_from_record(created, local=local)


async def _reuse_or_replace_exact_binding(
    client: WorkspaceClient,
    workspace: dict[str, Any],
    local: LocalWorkspace,
) -> WorkspaceBinding:
    """Reuse an exact registration only while it still describes the local contents.

    A path is durable but the thing at that path is not. A plain folder can become a Git
    checkout, or one checkout can be replaced by another with a different root history. The
    workspace endpoint already owns the audited replacement transaction (including active-run
    refusal), so the client only detects drift and asks for that operation.
    """

    registered_vcs = "git" if workspace.get("vcs", "git") == "git" else "none"
    identity_changed = False
    if registered_vcs == local.vcs == "git":
        registered_identity = str(workspace.get("repo_identity") or "").strip()
        current_identity = await _repo_identity(local.root)
        # A missing recorded identity is not evidence that the current checkout matches. Current
        # servers backfill legacy rows at boot; if one remains unknown, replacing it is the only
        # deterministic way to bind this path without silently inheriting stale repository state.
        identity_changed = not registered_identity or current_identity != registered_identity

    if registered_vcs == local.vcs and not identity_changed:
        return _binding_from_record(workspace, local=local)

    replaced = await client.create_workspace(
        local.root.name or str(local.root),
        str(local.root),
        vcs=local.vcs,
        replace_existing=True,
    )
    return _binding_from_record(replaced, local=local)


def _binding_from_record(
    workspace: dict[str, Any], *, local: LocalWorkspace | None = None
) -> WorkspaceBinding:
    root = Path(str(workspace["root_path"])).expanduser().resolve()
    context = local or LocalWorkspace(
        root=root,
        active_directory=root,
        cwd_relative=".",
        vcs="git" if workspace.get("vcs", "git") == "git" else "none",
    )
    return WorkspaceBinding(
        workspace_id=str(workspace["workspace_id"]),
        name=str(workspace.get("name") or root.name),
        root=root,
        active_directory=context.active_directory,
        cwd_relative=context.cwd_relative,
        # A local discovery is the current filesystem fact. The record is authoritative only
        # for selector-only bindings whose path this client did not inspect.
        vcs=(
            context.vcs
            if local is not None
            else ("git" if workspace.get("vcs", "git") == "git" else "none")
        ),
    )
