"""Portable, endpoint-bound connection and authentication for the ``orch`` client."""

from __future__ import annotations

import re
from functools import partial
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.auth import create_auth_app
from orca.connection import (
    ConfigRepository,
    ConnectionConfigError,
    CredentialBackendUnavailable,
    CredentialSource,
    EndpointSource,
    InsecureEndpointError,
    canonical_endpoint,
    resolve_connection,
)


class _Credentials:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get(self, profile: str, endpoint: str) -> str | None:
        return self.values.get((profile, endpoint))

    def set(self, profile: str, endpoint: str, token: str) -> None:
        self.values[(profile, endpoint)] = token

    def delete(self, profile: str, endpoint: str) -> bool:
        return self.values.pop((profile, endpoint), None) is not None


class _UnavailableCredentials:
    def get(self, profile: str, endpoint: str) -> str | None:
        del profile, endpoint
        raise CredentialBackendUnavailable("no system credential backend is available")

    def set(self, profile: str, endpoint: str, token: str) -> None:
        del profile, endpoint, token
        raise CredentialBackendUnavailable("no system credential backend is available")

    def delete(self, profile: str, endpoint: str) -> bool:
        del profile, endpoint
        raise CredentialBackendUnavailable("no system credential backend is available")


def test_endpoint_canonicalization_makes_credential_binding_stable(tmp_path: Path) -> None:
    credentials = _Credentials()
    credentials.set("work", "https://example.com/orch", "correct-token")

    connection = resolve_connection(
        profile="work",
        url="HTTPS://EXAMPLE.COM:443/orch/",
        environ={},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=credentials,
    )

    assert canonical_endpoint("HTTPS://EXAMPLE.COM:443/orch/") == "https://example.com/orch"
    assert connection.endpoint == "https://example.com/orch"
    assert connection.token == "correct-token"
    assert connection.credential_source is CredentialSource.KEYRING
    assert "correct-token" not in repr(connection)


def test_stored_token_is_never_reused_for_a_different_endpoint(tmp_path: Path) -> None:
    credentials = _Credentials()
    credentials.set("work", "https://one.example", "one-token")

    connection = resolve_connection(
        profile="work",
        url="https://two.example",
        environ={},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=credentials,
    )

    assert connection.token == ""
    assert connection.credential_source is CredentialSource.NONE


def test_config_is_non_secret_and_honors_config_home_override(tmp_path: Path) -> None:
    config_home = tmp_path / "portable-config"
    repository = ConfigRepository(environ={"ORCA_CONFIG_HOME": str(config_home)})

    repository.upsert_profile("team", "https://orch.example", activate=True)

    contents = (config_home / "config.toml").read_text(encoding="utf-8")
    assert 'active_profile = "team"' in contents
    assert 'url = "https://orch.example"' in contents
    assert "token" not in contents.lower()
    assert repository.load().profiles["team"].endpoint == "https://orch.example"


def test_config_refuses_fields_that_could_become_a_plaintext_secret(tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.toml").write_text(
        'version = 1\ntoken = "must-not-live-here"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConnectionConfigError, match="must not be stored"):
        ConfigRepository(home=config_home).load()


def test_exported_token_cannot_be_combined_with_a_worktree_dotenv_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "ORCA_URL=https://attacker.example\nORCA_AUTH_TOKEN=worktree-token\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    connection = resolve_connection(
        environ={"ORCA_AUTH_TOKEN": "exported-token"},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=_Credentials(),
    )

    assert connection.endpoint == "http://127.0.0.1:8080"
    assert connection.token == "exported-token"
    assert connection.credential_source is CredentialSource.ENVIRONMENT


def test_runtime_resolution_never_reads_a_worktree_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "ORCA_URL=https://legacy.example/\nORCA_AUTH_TOKEN=legacy-token\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    connection = resolve_connection(
        environ={},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=_Credentials(),
    )

    assert connection.endpoint == "http://127.0.0.1:8080"
    assert connection.token == ""
    assert connection.credential_source is CredentialSource.NONE


def test_a_credential_is_never_sent_to_a_remote_http_endpoint(tmp_path: Path) -> None:
    """The rule is about sending a secret in the clear, so it needs a secret to be about."""
    resolve = partial(
        resolve_connection,
        url="http://orch.example:8420",
        environ={"ORCA_AUTH_TOKEN": "secret"},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=_Credentials(),
    )
    with pytest.raises(InsecureEndpointError, match="in the clear"):
        resolve()

    allowed = resolve(allow_insecure_http=True)
    assert allowed.endpoint == "http://orch.example:8420"
    assert allowed.token == "secret"


def test_an_anonymous_remote_http_endpoint_is_allowed(tmp_path: Path) -> None:
    """This test used to assert the opposite, with an empty credential store -- so it was
    pinning a refusal to send credentials that did not exist, which is how the bug survived.

    Reported from a real setup on 2026-08-31: a profile pointing at a LAN harness over plain
    HTTP, no token anywhere, refused with "refusing to send credentials". The check ran before
    the credential was resolved and could not know there was none. An unauthenticated
    connection over HTTP leaks nothing of the user's; a token over HTTP leaks the token.
    """
    connection = resolve_connection(
        url="http://192.168.1.237:8000",
        environ={},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=_Credentials(),
    )

    assert connection.endpoint == "http://192.168.1.237:8000"
    assert connection.token == ""
    assert connection.credential_source is CredentialSource.NONE


def test_a_keyring_credential_is_protected_too(tmp_path: Path) -> None:
    """Not only the environment: whichever source supplied it, it is still a secret."""
    store = _Credentials()
    store.set("default", "http://orch.example:8420", "from-keyring")

    with pytest.raises(InsecureEndpointError, match="in the clear"):
        resolve_connection(
            url="http://orch.example:8420",
            environ={},
            config=ConfigRepository(home=tmp_path / "config"),
            credentials=store,
        )


def test_token_file_is_an_explicit_headless_credential_source(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")

    connection = resolve_connection(
        url="https://orch.example",
        environ={"ORCA_AUTH_TOKEN_FILE": str(token_file)},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=_UnavailableCredentials(),
    )

    assert connection.token == "file-token"
    assert connection.credential_source is CredentialSource.TOKEN_FILE


def test_unauthenticated_loopback_runtime_does_not_require_a_keyring(tmp_path: Path) -> None:
    connection = resolve_connection(
        environ={},
        config=ConfigRepository(home=tmp_path / "config"),
        credentials=_UnavailableCredentials(),
    )

    assert connection.endpoint == "http://127.0.0.1:8080"
    assert connection.token == ""
    assert connection.credential_source is CredentialSource.NONE


def test_auth_cli_logs_in_reports_status_and_logs_out_without_leaking_token(
    tmp_path: Path,
) -> None:
    repository = ConfigRepository(home=tmp_path / "config")
    credentials = _Credentials()
    app = create_auth_app(
        config=repository,
        credentials=credentials,
        environ={},
        cwd=tmp_path,
    )
    runner = CliRunner()

    login = runner.invoke(
        app,
        ["login", "--profile", "work", "--url", "https://EXAMPLE.com/", "--token-stdin"],
        input="super-secret\n",
    )
    assert login.exit_code == 0, login.output
    assert "super-secret" not in login.output
    assert "server     https://example.com" in login.output
    assert "token      ••••••••••••••••" in login.output
    assert "✓ Authenticated" in login.output
    assert "✓ Saved to system credential store" in login.output
    assert "profile    work" in login.output
    assert "credential bound to example.com" in login.output
    assert credentials.get("work", "https://example.com") == "super-secret"
    assert "super-secret" not in repository.path.read_text(encoding="utf-8")

    status = runner.invoke(app, ["status", "--profile", "work"])
    assert status.exit_code == 0, status.output
    assert "https://example.com" in status.output
    assert "credential configured" in status.output.lower()
    assert "super-secret" not in status.output

    logout = runner.invoke(app, ["logout", "--profile", "work"])
    assert logout.exit_code == 0, logout.output
    assert credentials.get("work", "https://example.com") is None

    after = runner.invoke(app, ["status", "--profile", "work"])
    assert after.exit_code == 0, after.output
    assert "no credential configured" in after.output.lower()

    help_result = runner.invoke(app, ["login", "--help"])
    assert help_result.exit_code == 0
    assert re.search(r"--token(?:\s|$)", help_result.output) is None
    assert "--token-stdin" in help_result.output


def test_auth_cli_uses_a_hidden_prompt_and_surfaces_missing_keychain(tmp_path: Path) -> None:
    repository = ConfigRepository(home=tmp_path / "config")
    prompted_credentials = _Credentials()
    prompted_app = create_auth_app(
        config=repository,
        credentials=prompted_credentials,
        environ={},
        cwd=tmp_path,
    )

    prompted = CliRunner().invoke(
        prompted_app,
        ["login", "--url", "https://orch.example"],
        input="prompt-secret\n",
    )
    assert prompted.exit_code == 0, prompted.output
    assert "prompt-secret" not in prompted.output

    unavailable_app = create_auth_app(
        config=ConfigRepository(home=tmp_path / "other-config"),
        credentials=_UnavailableCredentials(),
        environ={},
        cwd=tmp_path,
    )
    unavailable = CliRunner().invoke(unavailable_app, ["status"])
    assert unavailable.exit_code == 1
    assert "credential backend" in unavailable.output.lower()
    assert "plaintext" not in unavailable.output.lower()


def test_the_endpoint_reports_where_it_came_from(tmp_path: Path) -> None:
    """`auth status` printed the profile name beside the endpoint and nothing else, so an
    ORCA_URL silently overrode config.toml while the output still named the profile -- a
    person reading it would check the file and find it correct. (2026-08-31)"""
    config = ConfigRepository(home=tmp_path / "config")
    config.upsert_profile("default", "http://127.0.0.1:8080")
    resolve = partial(resolve_connection, config=config, credentials=_Credentials())

    from_profile = resolve(environ={})
    assert from_profile.endpoint_source is EndpointSource.PROFILE

    from_env = resolve(environ={"ORCA_URL": "http://127.0.0.1:9999"})
    assert from_env.endpoint_source is EndpointSource.ENVIRONMENT
    assert from_env.endpoint == "http://127.0.0.1:9999"

    from_flag = resolve(url="http://127.0.0.1:7777", environ={})
    assert from_flag.endpoint_source is EndpointSource.FLAG


def test_the_built_in_default_says_it_is_one(tmp_path: Path) -> None:
    connection = resolve_connection(
        environ={}, config=ConfigRepository(home=tmp_path / "config"), credentials=_Credentials()
    )

    assert connection.endpoint_source is EndpointSource.DEFAULT
    assert connection.endpoint == "http://127.0.0.1:8080"
