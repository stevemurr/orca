"""The ``orca auth`` command group.

Only non-secret endpoint/profile metadata is written to TOML.  Login accepts a hidden prompt or
stdin, never a bearer token in process arguments where it would be visible to shell history and
process inspection.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

import typer

from orca.connection import (
    ConfigRepository,
    ConnectionConfigError,
    CredentialBackendUnavailable,
    CredentialSource,
    CredentialStore,
    EndpointSource,
    KeyringCredentialStore,
    current_connection_selection,
    resolve_connection,
)


def _read_token(*, token_stdin: bool) -> str:
    value = sys.stdin.read() if token_stdin else typer.prompt("Token", hide_input=True)
    normalized = value.strip()
    if not normalized:
        raise ConnectionConfigError("credential must not be empty")
    if "\n" in normalized or "\r" in normalized:
        raise ConnectionConfigError("credential must be a single line")
    return normalized


def _source_label(source: CredentialSource) -> str:
    return {
        CredentialSource.KEYRING: "system credential store",
        CredentialSource.ENVIRONMENT: "environment",
        CredentialSource.TOKEN_FILE: "token file",
        CredentialSource.NONE: "none",
    }[source]


def _receipt(*rows: tuple[str, str]) -> None:
    for label, value in rows:
        typer.echo(f"{label:<11}{value}")


def _credential_binding(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return parsed.netloc or endpoint


def _inherit_root_selection(
    profile: str | None,
    url: str | None,
    allow_insecure_http: bool,
) -> tuple[str | None, str | None, bool]:
    """Let ``orca --profile/--url … auth …`` mean what the root help promises.

    ``create_auth_app`` is also tested and usable on its own, where the root context has no
    selection parameters. Explicit subcommand values always win.
    """
    root = current_connection_selection()
    inherited_profile = root.profile
    inherited_url = root.url
    inherited_insecure = bool(root.allow_insecure_http)
    return (
        profile or (str(inherited_profile) if inherited_profile else None),
        url if url is not None else (str(inherited_url) if inherited_url else None),
        allow_insecure_http or inherited_insecure,
    )


def create_auth_app(
    *,
    config: ConfigRepository | None = None,
    credentials: CredentialStore | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> typer.Typer:
    """Build an injectable Typer sub-application for ``orca auth``."""
    application = typer.Typer(
        add_completion=False,
        help="Configure portable profiles and secure credentials.",
    )

    def dependencies() -> tuple[ConfigRepository, CredentialStore, Mapping[str, str], Path]:
        environment = os.environ if environ is None else environ
        repository = config if config is not None else ConfigRepository(environ=environment)
        store = credentials if credentials is not None else KeyringCredentialStore()
        directory = Path.cwd() if cwd is None else cwd
        return repository, store, environment, directory

    def fail(exc: Exception) -> NoReturn:
        typer.echo(f"Authentication configuration error: {exc}", err=True)
        raise typer.Exit(1) from exc

    def persist(
        repository: ConfigRepository,
        store: CredentialStore,
        *,
        profile: str,
        endpoint: str,
        token: str,
    ) -> None:
        previous = store.get(profile, endpoint)
        store.set(profile, endpoint, token)
        try:
            repository.upsert_profile(profile, endpoint, activate=True)
        except Exception:
            # Keep keyring/config changes transaction-like. A failed metadata write should not
            # leave an unreachable credential behind or destroy the previous one.
            if previous:
                store.set(profile, endpoint, previous)
            else:
                store.delete(profile, endpoint)
            raise

    def login(
        profile: str | None = typer.Option(None, "--profile", "-p", help="Profile name."),
        url: str | None = typer.Option(None, "--url", help="Backend base URL."),
        token_stdin: bool = typer.Option(
            False,
            "--token-stdin",
            help="Read the token from stdin (recommended for headless environments).",
        ),
        allow_insecure_http: bool = typer.Option(
            False,
            "--allow-insecure-http",
            help="Allow remote plaintext HTTP for this operation.",
        ),
    ) -> None:
        """Save an endpoint profile and credential in the system credential store."""
        repository, store, environment, _directory = dependencies()
        profile, url, allow_insecure_http = _inherit_root_selection(
            profile, url, allow_insecure_http
        )
        try:
            connection = resolve_connection(
                profile=profile,
                url=url,
                environ=environment,
                config=repository,
                credentials=store,
                allow_insecure_http=True if allow_insecure_http else None,
                require_store=True,
            )
            token = _read_token(token_stdin=token_stdin)
            persist(
                repository,
                store,
                profile=connection.profile,
                endpoint=connection.endpoint,
                token=token,
            )
        except (ConnectionConfigError, CredentialBackendUnavailable, OSError) as exc:
            fail(exc)
        _receipt(
            ("server", connection.endpoint),
            ("token", "••••••••••••••••"),
        )
        typer.secho("✓ Authenticated", fg=typer.colors.GREEN)
        typer.secho("✓ Saved to system credential store", fg=typer.colors.GREEN)
        _receipt(
            ("profile", connection.profile),
            ("credential", f"bound to {_credential_binding(connection.endpoint)}"),
        )

    def status(
        profile: str | None = typer.Option(None, "--profile", "-p", help="Profile name."),
        url: str | None = typer.Option(None, "--url", help="Inspect this exact endpoint."),
        allow_insecure_http: bool = typer.Option(
            False,
            "--allow-insecure-http",
            help="Allow remote plaintext HTTP for this operation.",
        ),
    ) -> None:
        """Show the selected endpoint and credential source without revealing the token."""
        repository, store, environment, _directory = dependencies()
        profile, url, allow_insecure_http = _inherit_root_selection(
            profile, url, allow_insecure_http
        )
        try:
            connection = resolve_connection(
                profile=profile,
                url=url,
                environ=environment,
                config=repository,
                credentials=store,
                allow_insecure_http=True if allow_insecure_http else None,
                require_store=True,
            )
        except (ConnectionConfigError, CredentialBackendUnavailable, OSError) as exc:
            fail(exc)
        # The endpoint's source, not only its value. Printing the profile name beside an
        # endpoint that came from ORCA_URL sends a person to check a file that is correct.
        origin = connection.endpoint_source
        _receipt(
            ("profile", connection.profile),
            (
                "server",
                connection.endpoint
                if origin is EndpointSource.PROFILE
                else f"{connection.endpoint}  (from {origin.value}, overriding the profile)"
                if origin is not EndpointSource.DEFAULT
                else f"{connection.endpoint}  (built-in default; no profile url set)",
            ),
        )
        if connection.authenticated:
            _receipt(
                (
                    "credential",
                    f"configured · {_source_label(connection.credential_source)}",
                )
            )
        else:
            typer.echo("No credential configured.")

    def logout(
        profile: str | None = typer.Option(None, "--profile", "-p", help="Profile name."),
        url: str | None = typer.Option(None, "--url", help="Remove only this endpoint binding."),
        allow_insecure_http: bool = typer.Option(
            False,
            "--allow-insecure-http",
            help="Allow remote plaintext HTTP for this operation.",
        ),
    ) -> None:
        """Delete the credential for the exact selected profile and endpoint."""
        repository, store, environment, _directory = dependencies()
        profile, url, allow_insecure_http = _inherit_root_selection(
            profile, url, allow_insecure_http
        )
        try:
            connection = resolve_connection(
                profile=profile,
                url=url,
                environ=environment,
                config=repository,
                credentials=store,
                allow_insecure_http=True if allow_insecure_http else None,
                require_store=True,
            )
            removed = store.delete(connection.profile, connection.endpoint)
        except (ConnectionConfigError, CredentialBackendUnavailable, OSError) as exc:
            fail(exc)
        message = "Credential removed" if removed else "No stored credential found"
        _receipt(
            ("profile", connection.profile),
            ("server", connection.endpoint),
        )
        typer.secho(
            f"{'✓' if removed else '○'} {message}",
            fg=typer.colors.GREEN if removed else typer.colors.YELLOW,
        )
        if connection.credential_source in {
            CredentialSource.ENVIRONMENT,
            CredentialSource.TOKEN_FILE,
        }:
            typer.echo(
                f"The {_source_label(connection.credential_source)} credential remains active; "
                + "unset or remove that explicit source to finish logging out."
            )

    # Registered here rather than with decorators, so each command is a name this function
    # visibly uses; a decorator alone reads as an unused nested function.
    for name, command in (("login", login), ("status", status), ("logout", logout)):
        application.command(name)(command)
    return application


auth_app = create_auth_app()
