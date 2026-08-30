from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orca.workspace_context import (
    LocalWorkspace,
    WorkspaceContextError,
    discover_local_workspace,
    explicit_external_path,
    resolve_workspace_binding,
)
from tests.conftest import git


class _RecordingWorkspaceClient:
    """A small API double that preserves the contract's replace-in-place semantics."""

    def __init__(self, workspaces: list[dict] | None = None) -> None:
        self.workspaces = list(workspaces or [])
        self.creations: list[dict] = []

    async def list_workspaces(self):
        return list(self.workspaces)

    async def create_workspace(
        self,
        name,
        root_path,
        config=None,
        vcs=None,
        replace_existing=False,
    ):
        root = Path(root_path).resolve()
        existing = next(
            (
                workspace
                for workspace in self.workspaces
                if Path(workspace["root_path"]).resolve() == root
            ),
            None,
        )
        call = {
            "name": name,
            "root_path": root_path,
            "config": config,
            "vcs": vcs,
            "replace_existing": replace_existing,
        }
        self.creations.append(call)
        identity = git(root, "rev-list", "--max-parents=0", "HEAD") if vcs == "git" else None
        created = {
            "workspace_id": (
                existing["workspace_id"]
                if existing is not None and replace_existing
                else f"ws_{len(self.workspaces) + 1}"
            ),
            "name": name,
            "root_path": str(root),
            "vcs": vcs,
            "repo_identity": identity,
        }
        if existing is not None and replace_existing:
            self.workspaces[self.workspaces.index(existing)] = created
        else:
            self.workspaces.append(created)
        return created


def _init_repo(root: Path, content: str) -> str:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text(content, encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "initial")
    return git(root, "rev-list", "--max-parents=0", "HEAD")


def test_discovery_uses_the_nearest_git_checkout_and_preserves_focus(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    nested = outer / "projects" / "nested"
    focus = nested / "packages" / "web"
    (outer / ".git").mkdir(parents=True)
    (nested / ".git").mkdir(parents=True)
    focus.mkdir(parents=True)

    found = discover_local_workspace(focus)

    assert found.root == nested.resolve()
    assert found.active_directory == focus.resolve()
    assert found.cwd_relative == "packages/web"
    assert found.vcs == "git"


def test_discovery_uses_an_ordinary_folder_as_its_exact_root(tmp_path: Path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()

    found = discover_local_workspace(folder)

    assert found == LocalWorkspace(
        root=folder.resolve(),
        active_directory=folder.resolve(),
        cwd_relative=".",
        vcs="none",
    )


def test_discovery_does_not_treat_a_bare_repository_as_an_ordinary_folder(
    tmp_path: Path,
) -> None:
    bare = tmp_path / "project.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n")

    with pytest.raises(WorkspaceContextError, match="bare Git repository"):
        discover_local_workspace(bare)


def test_only_a_human_authored_existing_external_path_retargets(tmp_path: Path) -> None:
    current = tmp_path / "project-a"
    sibling = tmp_path / "project-b"
    current.mkdir()
    sibling.mkdir()

    assert (
        explicit_external_path("make the same change in ../project-b", current, current)
        == sibling.resolve()
    )
    assert explicit_external_path("edit ./README.md", current, current) is None
    assert explicit_external_path("the model mentioned ../missing", current, current) is None


def test_resolver_registers_the_exact_discovered_root_instead_of_using_an_ancestor(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "outer"
    nested = outer / "nested"
    (outer / ".git").mkdir(parents=True)
    (nested / ".git").mkdir(parents=True)

    class _Client:
        def __init__(self) -> None:
            self.workspaces = [
                {
                    "workspace_id": "ws_outer",
                    "name": "outer",
                    "root_path": str(outer.resolve()),
                    "vcs": "git",
                }
            ]

        async def list_workspaces(self):
            return list(self.workspaces)

        async def create_workspace(self, name, root_path, *, vcs=None, **_kwargs):
            created = {
                "workspace_id": "ws_nested",
                "name": name,
                "root_path": root_path,
                "vcs": vcs,
            }
            self.workspaces.append(created)
            return created

    binding = asyncio.run(resolve_workspace_binding(_Client(), path=nested))

    assert binding.workspace_id == "ws_nested"
    assert binding.root == nested.resolve()
    assert binding.active_directory == nested.resolve()


def test_resolver_replaces_a_plain_registration_after_the_folder_becomes_git(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    client = _RecordingWorkspaceClient()

    plain = asyncio.run(resolve_workspace_binding(client, path=root))
    identity = _init_repo(root, "now a repository\n")
    repository = asyncio.run(resolve_workspace_binding(client, path=root))

    assert repository.workspace_id == plain.workspace_id
    assert repository.vcs == "git"
    assert client.workspaces[0]["repo_identity"] == identity
    assert client.creations[-1]["replace_existing"] is True
    assert client.creations[-1]["vcs"] == "git"


def test_resolver_replaces_a_different_checkout_that_occupies_the_same_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    first_identity = _init_repo(root, "first repository\n")
    client = _RecordingWorkspaceClient()
    first = asyncio.run(resolve_workspace_binding(client, path=root))

    root.rename(tmp_path / "archived-project")
    second_identity = _init_repo(root, "replacement repository\n")
    second = asyncio.run(resolve_workspace_binding(client, path=root))

    assert second_identity != first_identity
    assert second.workspace_id == first.workspace_id
    assert second.vcs == "git"
    assert client.workspaces[0]["repo_identity"] == second_identity
    assert client.creations[-1]["replace_existing"] is True


def test_resolver_does_not_reuse_an_unidentified_git_registration(tmp_path: Path) -> None:
    root = tmp_path / "project"
    identity = _init_repo(root, "current repository\n")
    client = _RecordingWorkspaceClient(
        [
            {
                "workspace_id": "ws_existing",
                "name": "project",
                "root_path": str(root),
                "vcs": "git",
                "repo_identity": None,
            }
        ]
    )

    binding = asyncio.run(resolve_workspace_binding(client, path=root))

    assert binding.workspace_id == "ws_existing"
    assert client.workspaces[0]["repo_identity"] == identity
    assert client.creations[-1]["replace_existing"] is True
