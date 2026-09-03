"""Thin public command surface for the state-driven orca client."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from orca.auth import auth_app
from orca.backend import BackendError, RunRequest, ThreadSummary
from orca.connection import (
    ConnectionConfigError,
    CredentialBackendUnavailable,
    current_connection_selection,
    push_connection_selection,
    reset_connection_selection,
    resolve_connection,
)
from orca.http_backend import HttpBackend
from orca.output.plain import run_once
from orca.server_manager import LocalServerManager, ManagedServerError, ManagedServerStatus
from orca.tui.app import OrcaApp


@dataclass(frozen=True, slots=True)
class GlobalOptions:
    profile: str | None
    url: str | None


app = typer.Typer(
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=False,
    help="A calm terminal interface for durable agent work.",
)
server_app = typer.Typer(help="Manage the local backend process orca was told how to start.")
app.add_typer(auth_app, name="auth")
app.add_typer(server_app, name="server")

console = Console()
error_console = Console(stderr=True)


@app.callback()
def root(
    ctx: typer.Context,
    profile: str | None = typer.Option(None, "--profile", help="Connection profile."),
    url: str | None = typer.Option(None, "--url", help="Override the backend endpoint."),
    show_version: bool = typer.Option(
        False,
        "--version",
        is_eager=True,
        help="Show the client version and exit.",
    ),
) -> None:
    """Open the interactive conversation when no subcommand is supplied."""

    if show_version:
        console.print(_version())
        raise typer.Exit()
    token = push_connection_selection(profile=profile, url=url, allow_insecure_http=None)
    ctx.call_on_close(lambda: reset_connection_selection(token))
    ctx.obj = GlobalOptions(profile, url)
    if ctx.invoked_subcommand is None:
        launch_tui(workspace="", thread="", profile=profile, url=url)


@app.command("chat")
def chat(
    ctx: typer.Context,
    workspace: str = typer.Option("", "--workspace", "-w", help="Workspace name, id, or path."),
    thread: str = typer.Option("", "--thread", "-t", help="Continue a thread by id."),
) -> None:
    """Open the interactive terminal application."""

    options = _options(ctx)
    launch_tui(
        workspace=workspace,
        thread=thread,
        profile=options.profile,
        url=options.url,
    )


@app.command("run")
def run_command(
    ctx: typer.Context,
    message: str = typer.Argument(..., help="What you want done."),
    workspace: str = typer.Option("", "--workspace", "-w", help="Workspace name, id, or path."),
    thread: str = typer.Option("", "--thread", "-t", help="Continue a thread by id."),
    mode: str = typer.Option("auto", "--mode", help="Run mode."),
    policy: str = typer.Option("safe", "--policy", help="Approval policy."),
    no_follow: bool = typer.Option(False, "--no-follow", help="Print the run id and exit."),
    jsonl: bool = typer.Option(False, "--jsonl", help="Emit versioned JSON Lines."),
) -> None:
    """Start one run without opening the full-screen interface."""

    options = _options(ctx)

    async def execute() -> int:
        connection = resolve_connection(profile=options.profile, url=options.url)
        backend = HttpBackend(connection, workspace_selector=workspace)
        try:
            return await run_once(
                backend,
                RunRequest(message, thread or None, "", ".", mode, policy),
                follow=not no_follow,
                jsonl=jsonl,
            )
        finally:
            await backend.close()

    exit_code = _run(execute())
    if exit_code:
        raise typer.Exit(exit_code)


@app.command("threads")
def threads(ctx: typer.Context) -> None:
    """List recent conversations."""

    options = _options(ctx)

    async def execute() -> tuple[ThreadSummary, ...]:
        connection = resolve_connection(profile=options.profile, url=options.url)
        backend = HttpBackend(connection)
        try:
            await backend.connect()
            return await backend.recent_threads()
        finally:
            await backend.close()

    rows = _run(execute())
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True, style="cyan")
    table.add_column(ratio=1)
    table.add_column(no_wrap=True, style="dim")
    for row in rows:
        table.add_row(row.thread_id, row.title, row.latest_run_status)
    console.print(table)


@server_app.command("status")
def server_status(ctx: typer.Context) -> None:
    """Show local backend ownership and health."""

    _server_action(ctx, "status")


@server_app.command("start")
def server_start(ctx: typer.Context) -> None:
    """Start or reuse the local backend."""

    _server_action(ctx, "start")


@server_app.command("stop")
def server_stop(ctx: typer.Context) -> None:
    """Stop the local backend only when orca owns it."""

    _server_action(ctx, "stop")


@server_app.command("restart")
def server_restart(ctx: typer.Context) -> None:
    """Restart the orca-owned local backend."""

    _server_action(ctx, "restart")


def launch_tui(
    *,
    workspace: str,
    thread: str,
    profile: str | None,
    url: str | None,
) -> None:
    """Compose and run the interactive application."""

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        error_console.print(
            '[yellow]Interactive mode needs a terminal.[/yellow] Try [bold]orca run "…"[/bold].'
        )
        raise typer.Exit(2)
    try:
        connection = resolve_connection(profile=profile, url=url)
        backend = HttpBackend(connection, workspace_selector=workspace)
        initial = None
        if thread:
            from orca.app.model import AppState

            initial = AppState(thread_id=thread)
        OrcaApp(backend, initial=initial).run()
    except (ConnectionConfigError, CredentialBackendUnavailable) as exc:
        error_console.print(f"[red]connection error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from exc


ServerAction = Literal["status", "start", "stop", "restart"]


def _server_action(ctx: typer.Context, action: ServerAction) -> None:
    options = _options(ctx)

    async def execute() -> ManagedServerStatus:
        connection = resolve_connection(profile=options.profile, url=options.url)
        manager = LocalServerManager(connection)
        match action:
            case "status":
                return await manager.status()
            case "start":
                return await manager.start()
            case "stop":
                return await manager.stop()
            case "restart":
                return await manager.restart()

    status = _run(execute())
    state = "running" if status.running else "stopped"
    owner = "managed" if status.managed else "external"
    detail = f" · pid {status.pid}" if status.pid else ""
    console.print(f"[bold]{state}[/bold] {status.endpoint} [dim]· {owner}{detail}[/dim]")


def _options(ctx: typer.Context) -> GlobalOptions:
    root_context = ctx.find_root()
    if isinstance(root_context.obj, GlobalOptions):
        return root_context.obj
    selection = current_connection_selection()
    return GlobalOptions(selection.profile, selection.url)


def _run(awaitable: Any) -> Any:
    try:
        return asyncio.run(awaitable)
    except (
        BackendError,
        ConnectionConfigError,
        CredentialBackendUnavailable,
        ManagedServerError,
    ) as exc:
        error_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


def _version() -> str:
    try:
        return version("orca")
    except PackageNotFoundError:
        return "development"
