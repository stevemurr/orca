"""Portable connection profiles and endpoint-bound credentials.

No backend configuration is imported here.  This is a client boundary: profile metadata is
portable TOML, credentials live in a system credential backend (or an explicit process/file
source), and changing endpoints can never carry a stored bearer token with it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.parse import SplitResult, urlsplit, urlunsplit

#: Where a harness listens by default, so `orca` with no configuration reaches one.
#:
#: 8420 until 2026-08-31, which was the port of the project this client was extracted from.
#: That project's server is still installed on the author's machine and still answers there,
#: so an unconfigured orca connected to the wrong backend and got plausible answers from it.
#: A default that reaches something is worse than one that reaches nothing.
DEFAULT_ORIGIN = "http://127.0.0.1:8080"
DEFAULT_PROFILE = "default"
CONFIG_HOME_ENV = "ORCA_CONFIG_HOME"
PROFILE_ENV = "ORCA_PROFILE"
URL_ENV = "ORCA_URL"
TOKEN_ENV = "ORCA_AUTH_TOKEN"
TOKEN_FILE_ENV = "ORCA_AUTH_TOKEN_FILE"
ALLOW_INSECURE_HTTP_ENV = "ORCA_ALLOW_INSECURE_HTTP"

_MAX_TOKEN_BYTES = 64 * 1024
_PROFILE_CHARACTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class ConnectionConfigError(ValueError):
    """A client-side connection setting is invalid or ambiguous."""


class UnknownProfileError(ConnectionConfigError):
    """A requested named profile does not exist."""


class InsecureEndpointError(ConnectionConfigError):
    """A credential might be sent over remote plaintext HTTP."""


class CredentialBackendUnavailable(RuntimeError):
    """No usable secure credential backend is available."""


class CredentialSource(str, Enum):
    NONE = "none"
    KEYRING = "keyring"
    ENVIRONMENT = "environment"
    TOKEN_FILE = "token_file"


@dataclass(frozen=True)
class ConnectionSelection:
    """Root CLI options shared with nested command groups for one invocation."""

    profile: str | None = None
    url: str | None = None
    allow_insecure_http: bool | None = None


_CLI_SELECTION: ContextVar[ConnectionSelection | None] = ContextVar(
    "orca_connection_selection",
    default=None,
)


def push_connection_selection(
    *,
    profile: str | None,
    url: str | None,
    allow_insecure_http: bool | None,
) -> Token[ConnectionSelection | None]:
    """Install root options until the surrounding Click context closes."""

    return _CLI_SELECTION.set(ConnectionSelection(profile, url, allow_insecure_http))


def reset_connection_selection(token: Token[ConnectionSelection | None]) -> None:
    _CLI_SELECTION.reset(token)


def current_connection_selection() -> ConnectionSelection:
    return _CLI_SELECTION.get() or ConnectionSelection()


class CredentialStore(Protocol):
    """Minimal secure-store seam, deliberately injectable for headless clients and tests."""

    def get(self, profile: str, endpoint: str) -> str | None: ...

    def set(self, profile: str, endpoint: str, token: str) -> None: ...

    def delete(self, profile: str, endpoint: str) -> bool: ...


def _credential_account(profile: str, endpoint: str) -> str:
    """Produce a bounded keyring account while retaining endpoint binding."""
    _validate_profile_name(profile)
    canonical = canonical_endpoint(endpoint)
    endpoint_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{profile}:{endpoint_hash}"


class KeyringCredentialStore:
    """Cross-platform storage backed by macOS, Windows, or Linux system facilities.

    ``keyring`` is imported lazily so explicit environment and token-file credentials continue to
    work in minimal/headless builds.  There is intentionally no plaintext-file fallback.
    """

    def __init__(self, service: str = "orca") -> None:
        self._service = service

    @staticmethod
    def _keyring():
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CredentialBackendUnavailable(
                "the system credential backend is unavailable; install keyring or use "
                f"{TOKEN_ENV}, {TOKEN_FILE_ENV}, or --token-stdin"
            ) from exc
        try:
            backend = keyring.get_keyring()
            priority = getattr(backend, "priority", 0)
        except Exception as exc:
            raise CredentialBackendUnavailable(
                "the system credential backend could not be initialized"
            ) from exc
        if priority <= 0:
            raise CredentialBackendUnavailable(
                "no system credential backend is available; use an explicit environment, "
                "token-file, or stdin credential in this environment"
            )
        return keyring

    @staticmethod
    def _backend_failure(exc: Exception) -> CredentialBackendUnavailable:
        # Backend exception strings can include helper command lines and platform details.  Keep
        # output stable and, most importantly, never risk reflecting credential material.
        return CredentialBackendUnavailable("the system credential backend operation failed")

    def get(self, profile: str, endpoint: str) -> str | None:
        keyring = self._keyring()
        try:
            return keyring.get_password(self._service, _credential_account(profile, endpoint))
        except Exception as exc:
            raise self._backend_failure(exc) from exc

    def set(self, profile: str, endpoint: str, token: str) -> None:
        keyring = self._keyring()
        try:
            keyring.set_password(
                self._service,
                _credential_account(profile, endpoint),
                _validated_token(token),
            )
        except Exception as exc:
            raise self._backend_failure(exc) from exc

    def delete(self, profile: str, endpoint: str) -> bool:
        keyring = self._keyring()
        try:
            keyring.delete_password(self._service, _credential_account(profile, endpoint))
        except Exception as exc:
            password_delete_error = getattr(
                getattr(keyring, "errors", None), "PasswordDeleteError", None
            )
            if password_delete_error is not None and isinstance(exc, password_delete_error):
                return False  # absent is already logged out
            raise self._backend_failure(exc) from exc
        return True


@dataclass(frozen=True)
class Profile:
    endpoint: str


@dataclass(frozen=True)
class ClientConfig:
    active_profile: str = DEFAULT_PROFILE
    profiles: Mapping[str, Profile] = field(default_factory=lambda: MappingProxyType({}))


class ConfigRepository:
    """Read and atomically write non-secret client profiles."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        environment = os.environ if environ is None else environ
        configured_home = environment.get(CONFIG_HOME_ENV)
        self._home = (
            home
            if home is not None
            else Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".orca"
        )
        self.path = self._home / "config.toml"

    def load(self) -> ClientConfig:
        if not self.path.exists():
            return ClientConfig()
        try:
            raw = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ConnectionConfigError(f"could not read client config at {self.path}") from exc
        if not isinstance(raw, dict):
            raise ConnectionConfigError("client config must be a TOML table")
        unknown_top_level = set(raw) - {"version", "active_profile", "profiles"}
        if unknown_top_level:
            raise ConnectionConfigError(
                "client config has unsupported fields; credentials must not be stored in "
                "config.toml"
            )
        if raw.get("version", 1) != 1:
            raise ConnectionConfigError("unsupported client config version")

        active = str(raw.get("active_profile", DEFAULT_PROFILE))
        _validate_profile_name(active)
        profiles_raw = raw.get("profiles", {})
        if not isinstance(profiles_raw, dict):
            raise ConnectionConfigError("client config 'profiles' must be a table")

        profiles: dict[str, Profile] = {}
        for name, value in profiles_raw.items():
            _validate_profile_name(name)
            if not isinstance(value, dict):
                raise ConnectionConfigError(f"profile {name!r} must be a table")
            unknown = set(value) - {"url"}
            if unknown:
                raise ConnectionConfigError(
                    f"profile {name!r} has unsupported fields; credentials must not be stored "
                    "in config.toml"
                )
            url = value.get("url")
            if not isinstance(url, str) or not url.strip():
                raise ConnectionConfigError(f"profile {name!r} requires a URL")
            profiles[name] = Profile(canonical_endpoint(url))
        return ClientConfig(active, MappingProxyType(profiles))

    def save(self, config: ClientConfig) -> None:
        _validate_profile_name(config.active_profile)
        lines = [
            "version = 1",
            f"active_profile = {json.dumps(config.active_profile, ensure_ascii=False)}",
            "",
        ]
        for name in sorted(config.profiles):
            _validate_profile_name(name)
            endpoint = canonical_endpoint(config.profiles[name].endpoint)
            lines.extend(
                (
                    f"[profiles.{json.dumps(name, ensure_ascii=False)}]",
                    f"url = {json.dumps(endpoint, ensure_ascii=False)}",
                    "",
                )
            )
        rendered = "\n".join(lines)

        self._home.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", dir=self._home)
        temporary = Path(temporary_name)
        try:
            # mkstemp is owner-only on POSIX. Reinforce that mode where supported; Windows relies
            # on its user-scoped ACL rather than POSIX permission bits.
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def upsert_profile(self, name: str, endpoint: str, *, activate: bool = True) -> ClientConfig:
        _validate_profile_name(name)
        current = self.load()
        profiles = dict(current.profiles)
        profiles[name] = Profile(canonical_endpoint(endpoint))
        updated = ClientConfig(
            active_profile=name if activate else current.active_profile,
            profiles=MappingProxyType(profiles),
        )
        self.save(updated)
        return updated


@dataclass(frozen=True)
class Connection:
    """An immutable resolved connection whose credential is bound to ``endpoint``."""

    profile: str
    endpoint: str
    token: str = field(default="", repr=False)
    credential_source: CredentialSource = CredentialSource.NONE

    @property
    def authenticated(self) -> bool:
        return bool(self.token)


def _validate_profile_name(name: str) -> str:
    if (
        not name
        or len(name) > 64
        or any(character not in _PROFILE_CHARACTERS for character in name)
    ):
        raise ConnectionConfigError(
            "profile names must be 1-64 letters, numbers, dots, underscores, or hyphens"
        )
    return name


def canonical_endpoint(value: str) -> str:
    """Canonicalize a base URL for comparison and credential lookup."""
    raw = value.strip()
    if not raw or any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in raw
    ):
        raise ConnectionConfigError(
            "endpoint must be a non-empty URL without whitespace or control characters"
        )
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ConnectionConfigError("endpoint has an invalid host or port") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ConnectionConfigError("endpoint URL must use http or https")
    if not parts.hostname:
        raise ConnectionConfigError("endpoint URL must include a host")
    if parts.username is not None or parts.password is not None:
        raise ConnectionConfigError("endpoint URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ConnectionConfigError("endpoint URL must not contain a query or fragment")

    hostname = parts.hostname.rstrip(".").lower()
    if not hostname:
        raise ConnectionConfigError("endpoint URL must include a valid host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConnectionConfigError("endpoint contains an invalid hostname") from exc
    else:
        hostname = f"[{address.compressed}]" if address.version == 6 else address.compressed

    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parts.path.rstrip("/")
    canonical_parts = SplitResult(scheme, netloc, path, "", "")
    return urlunsplit(canonical_parts)


def _is_loopback(endpoint: str) -> bool:
    hostname = urlsplit(endpoint).hostname
    if hostname is None:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _truthy_environment(value: str | None, *, name: str) -> bool:
    if value is None or not value.strip():
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConnectionConfigError(f"{name} must be true or false")


def _validated_token(token: str) -> str:
    normalized = token.strip()
    if not normalized:
        raise ConnectionConfigError("credential must not be empty")
    if "\n" in normalized or "\r" in normalized:
        raise ConnectionConfigError("credential must be a single line")
    return normalized


def _token_from_file(path_value: str) -> str:
    path = Path(path_value).expanduser()
    try:
        if not path.is_file():
            raise ConnectionConfigError(f"credential file does not exist: {path}")
        if path.stat().st_size > _MAX_TOKEN_BYTES:
            raise ConnectionConfigError("credential file is unexpectedly large")
        value = path.read_text(encoding="utf-8")
    except ConnectionConfigError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ConnectionConfigError(f"could not read credential file: {path}") from exc
    return _validated_token(value)


def resolve_connection(
    *,
    profile: str | None = None,
    url: str | None = None,
    environ: Mapping[str, str] | None = None,
    config: ConfigRepository | None = None,
    credentials: CredentialStore | None = None,
    allow_insecure_http: bool | None = None,
    require_store: bool = False,
) -> Connection:
    """Resolve one immutable connection from explicit current configuration sources.

    Precedence is explicit argument, environment, selected profile, then the loopback default.
    Credentials use environment, explicit token file, then the keyring entry for the exact
    canonical endpoint and profile. Runtime resolution never inspects a project ``.env`` file.
    """
    environment = os.environ if environ is None else environ
    repository = config if config is not None else ConfigRepository(environ=environment)
    credential_store = credentials if credentials is not None else KeyringCredentialStore()
    loaded = repository.load()

    env_profile = environment.get(PROFILE_ENV, "").strip()
    selected_profile = profile or env_profile or loaded.active_profile or DEFAULT_PROFILE
    _validate_profile_name(selected_profile)
    selected = loaded.profiles.get(selected_profile)

    env_url = environment.get(URL_ENV, "").strip()
    env_token = environment.get(TOKEN_ENV, "")
    token_file = environment.get(TOKEN_FILE_ENV, "").strip()
    if env_token.strip() and token_file:
        raise ConnectionConfigError(
            f"set only one of {TOKEN_ENV} and {TOKEN_FILE_ENV}; credential source is ambiguous"
        )
    if selected is None and selected_profile != DEFAULT_PROFILE and url is None and not env_url:
        raise UnknownProfileError(
            f"profile {selected_profile!r} does not exist; provide --url when creating it"
        )

    endpoint_value = (
        url
        if url is not None
        else env_url or (selected.endpoint if selected is not None else "") or DEFAULT_ORIGIN
    )
    endpoint = canonical_endpoint(endpoint_value)

    insecure_allowed = (
        allow_insecure_http
        if allow_insecure_http is not None
        else _truthy_environment(
            environment.get(ALLOW_INSECURE_HTTP_ENV), name=ALLOW_INSECURE_HTTP_ENV
        )
    )
    if env_token.strip():
        token = _validated_token(env_token)
        source = CredentialSource.ENVIRONMENT
    elif token_file:
        token = _token_from_file(token_file)
        source = CredentialSource.TOKEN_FILE
    else:
        try:
            stored = credential_store.get(selected_profile, endpoint)
        except CredentialBackendUnavailable:
            if require_store:
                raise
            # Anonymous runtime access remains usable in headless Linux environments. Credential
            # management commands opt into strict mode, and no plaintext persistence is invented.
            stored = None
        token = _validated_token(stored) if stored else ""
        source = CredentialSource.KEYRING if token else CredentialSource.NONE

    # Checked here, after the credential is resolved, because the rule is about sending a
    # secret in the clear and until this point nothing knows whether there is one. It used
    # to run before the lookup and refused every plain-HTTP endpoint off the loopback --
    # including anonymous ones, where the message "refusing to send credentials" named
    # something that did not exist. Reported from a real setup, 2026-08-31.
    #
    # An unauthenticated connection over HTTP leaks nothing of the user's, so it is theirs
    # to make. A token over HTTP leaks the token, which is not.
    if (
        token
        and urlsplit(endpoint).scheme == "http"
        and not _is_loopback(endpoint)
        and not insecure_allowed
    ):
        raise InsecureEndpointError(
            f"refusing to send the {source.value} credential to {endpoint} in the clear. "
            f"Use HTTPS, or set {ALLOW_INSECURE_HTTP_ENV}=true to accept the risk."
        )

    return Connection(
        profile=selected_profile,
        endpoint=endpoint,
        token=token,
        credential_source=source,
    )
