"""Architectural guardrails for the terminal client.

Three of these came across from the orchestrator's own cutover gate. The fourth one it had —
"the terminal surface must not import server internals" — is now enforced by the repository
boundary itself: there is no server in this repository to import. What replaces it is the
property that boundary was really protecting, which survives extraction intact: the application
and its views must reach a backend only through the `TerminalBackend` port, never through the
bundled HTTP implementation of it. A client that reaches for `HttpBackend` by name is a client
that only works against one harness.

The gate about retired orchestrator CLI modules and retired orchestrator event names went with
the repository it described.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "orca"

APPLICATION_SURFACES = (
    PACKAGE / "client.py",
    PACKAGE / "backend.py",
    PACKAGE / "http_backend.py",
    PACKAGE / "entrypoint.py",
    *(PACKAGE / "app").glob("*.py"),
    *(PACKAGE / "output").glob("*.py"),
    *(PACKAGE / "tui").rglob("*.py"),
)

RENDER_SURFACES = tuple((PACKAGE / "tui" / "render").glob("*.py"))

#: Everything except the composition root, which is the one place allowed to choose an
#: implementation of the port.
PORT_ONLY_SURFACES = tuple(
    path for path in APPLICATION_SURFACES if path.name not in {"entrypoint.py", "http_backend.py"}
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_only_the_composition_root_names_a_backend_implementation() -> None:
    violations: list[str] = []
    for path in PORT_ONLY_SURFACES:
        tree = _tree(path)
        for module in _imports(tree):
            if module in {"orca.http_backend", "orca.client"}:
                violations.append(f"{path.relative_to(PACKAGE)} imports {module}")
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for name in sorted(names & {"HttpApiClient", "HttpBackend"}):
            violations.append(f"{path.relative_to(PACKAGE)} names {name}")

    assert not violations, "the application bound itself to one backend:\n- " + "\n- ".join(
        sorted(violations)
    )


def test_application_core_is_renderer_and_transport_neutral() -> None:
    forbidden_roots = {"httpx", "orjson", "rich", "textual"}
    violations: list[str] = []
    for path in (PACKAGE / "app").glob("*.py"):
        for module in _imports(_tree(path)):
            if module.partition(".")[0] in forbidden_roots:
                violations.append(f"{path.name} imports {module}")

    assert not violations, "pure application core gained framework I/O:\n- " + "\n- ".join(
        sorted(violations)
    )


def test_rich_renderers_remain_pure_projections_of_application_state() -> None:
    forbidden_roots = {"asyncio", "httpx", "orjson", "textual"}
    allowed_prefixes = ("orca.app", "orca.tui.render")
    violations: list[str] = []
    for path in RENDER_SURFACES:
        for module in _imports(_tree(path)):
            if module.partition(".")[0] in forbidden_roots:
                violations.append(f"{path.name} imports framework/I/O module {module}")
            elif module.startswith("orca") and not module.startswith(allowed_prefixes):
                violations.append(f"{path.name} bypasses application state through {module}")

    assert not violations, "pure Rich rendering boundary was crossed:\n- " + "\n- ".join(
        sorted(violations)
    )


def test_views_do_not_own_transport_or_background_work() -> None:
    forbidden = {
        "HttpApiClient",
        "HttpBackend",
        "create_task",
        "run_worker",
        "stream_events",
    }
    violations: list[str] = []
    for path in (PACKAGE / "tui" / "views").glob("*.py"):
        tree = _tree(path)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for name in sorted(names & forbidden):
            violations.append(f"{path.name} owns {name}")

    assert not violations, "view-owned I/O breaks the state boundary:\n- " + "\n- ".join(violations)
