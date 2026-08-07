"""Typed, validated application configuration for the Katherine Bot backend.

This module centralizes every piece of runtime configuration in one frozen,
strictly validated model. It has no dependency on FastAPI, Groq, Supabase,
embeddings, or network I/O: importing it never reads the environment, opens
sockets, or constructs clients.

Rules enforced here:

* Environments are a closed enum: ``local``, ``test``, ``staging``,
  ``production``, and ``APP_ENV`` is **required** by :meth:`Settings.from_env`
  (fail-closed: a deployment that forgets it never silently runs in local
  mode).
* The model is frozen and assignment-validated: a constructed instance can
  never be mutated into an invalid state, so the readiness ``configuration``
  check can trust the running settings.
* Numeric and boolean values are strict: no permissive string parsing, no
  bool/float substitution for ints, no silent defaults for secrets.
* Critical strings are non-empty; URLs are validated; CORS origins are
  normalized, deduplicated, and rejected when unsafe.
* Production fails closed: missing critical configuration, localhost CORS
  origins, wildcard origins, and enabled features without their dependencies
  are rejected before the application starts serving traffic.
* Secrets are excluded from ``repr``, ``str``, and sanitized error messages.
  Direct construction failures raise ``ValidationError`` with
  ``hide_input_in_errors`` so raw values (URL credentials, keys) never appear
  in rendered errors.

Environment variables are read only by :meth:`Settings.from_env`; direct
construction (``Settings(...)``) never consults the environment, which makes
tests able to build settings without globals.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .cors_policy import DEFAULT_ALLOWED_ORIGINS, parse_cors_allowed_origins
from .turn_execution import TurnExecutionConfig

__all__ = [
    "AppEnvironment",
    "Settings",
    "SettingsConfigurationError",
    "SettingsIssue",
]

# ---------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------


class AppEnvironment(str, Enum):
    """Closed set of supported deployment environments."""

    local = "local"
    test = "test"
    staging = "staging"
    production = "production"


#: Environments that require the Supabase runtime configuration up front.
_REQUIRE_SUPABASE = frozenset({AppEnvironment.staging, AppEnvironment.production})

#: Environments that require an explicit CORS allowlist.
_REQUIRE_CORS_ALLOWLIST = frozenset({AppEnvironment.staging, AppEnvironment.production})

#: Hosts that are never acceptable CORS origins in production.
_LOCALHOST_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: Lower bound for the admission HMAC secret, mirroring ``admission.py``.
ADMISSION_SECRET_MIN_BYTES = 32

#: Timeout ranges for readiness checks (milliseconds).
READINESS_DATABASE_TIMEOUT_MIN_MS = 100
READINESS_DATABASE_TIMEOUT_MAX_MS = 30_000
READINESS_PROVIDER_TIMEOUT_MIN_MS = 100
READINESS_PROVIDER_TIMEOUT_MAX_MS = 30_000


# ---------------------------------------------------------------------------
# Sanitized configuration errors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingsIssue:
    """One sanitized configuration issue.

    Only the field name and a stable code are exposed. The raw value is never
    kept, so secrets and URLs cannot leak through error messages or repr.
    """

    field: str
    code: str


class SettingsConfigurationError(Exception):
    """Raised when application configuration is invalid.

    ``str`` and ``repr`` contain only field names and stable codes, never
    input values. This is the only error type raised by
    :meth:`Settings.from_env`; direct construction raises ``ValidationError``
    with ``hide_input_in_errors`` enabled, so raw inputs are never rendered
    by this module either.
    """

    def __init__(self, issues: tuple[SettingsIssue, ...]) -> None:
        self.issues = issues
        super().__init__(self._render(issues))

    @staticmethod
    def _render(issues: tuple[SettingsIssue, ...]) -> str:
        if not issues:
            return "Invalid application configuration."
        rendered = ", ".join(f"{issue.field}:{issue.code}" for issue in issues)
        return f"Invalid application configuration: {rendered}"

    def __str__(self) -> str:
        return self._render(self.issues)

    def __repr__(self) -> str:
        return f"SettingsConfigurationError({self.issues!r})"

    def sanitized_details(self) -> tuple[SettingsIssue, ...]:
        """Return the sanitized (field, code) issues, safe for logs."""
        return self.issues


class _SettingsError(ValueError):
    """Internal validator error carrying a stable code and field name.

    Pydantic v2 keeps the original exception in ``ctx["error"]``; sanitization
    extracts only ``code`` and ``field``, never the input value.
    """

    def __init__(self, code: str, field: str) -> None:
        super().__init__(code)
        self.code = code
        self.field = field


def _sanitize_validation_error(exc: ValidationError) -> SettingsConfigurationError:
    """Convert a pydantic ``ValidationError`` into sanitized issues.

    Only field locations and stable codes/types are kept; ``input`` values are
    deliberately discarded.
    """
    issues: list[SettingsIssue] = []
    for err in exc.errors():
        ctx_error = (err.get("ctx") or {}).get("error")
        if isinstance(ctx_error, _SettingsError):
            field = ctx_error.field or _loc_to_field(err.get("loc", ()))
            issues.append(SettingsIssue(field=field, code=ctx_error.code))
        else:
            issues.append(
                SettingsIssue(
                    field=_loc_to_field(err.get("loc", ())),
                    code=str(err.get("type", "invalid")),
                )
            )
    return SettingsConfigurationError(tuple(issues))


def _loc_to_field(loc: tuple[Any, ...]) -> str:
    parts = [str(part) for part in loc if isinstance(part, (str, int))]
    return ".".join(parts) if parts else "settings"


# ---------------------------------------------------------------------------
# Strict environment parsing helpers (used only by ``from_env``)
# ---------------------------------------------------------------------------


def _env_bool(key: str, source: Mapping[str, object], default: bool) -> bool:
    """Parse a boolean environment variable strictly.

    Accepts only ``"true"``/``"false"`` (case-insensitive). Absent values fall
    back to ``default``. Empty or unrecognized values raise
    ``SettingsConfigurationError``.
    """
    raw = source.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_boolean"),))
    token = raw.strip()
    if not token:
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_boolean"),))
    lowered = token.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_boolean"),))


def _env_int(
    key: str,
    source: Mapping[str, object],
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Parse an integer environment variable strictly."""
    raw = source.get(key)
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_integer"),))
    token = raw.strip()
    if not token:
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_integer"),))
    if token.lower() in ("true", "false", "yes", "no"):
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_integer"),))
    try:
        value = int(token)
    except ValueError:
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_integer"),))
    if value < minimum or value > maximum:
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "out_of_range"),))
    return value


def _env_secret(key: str, source: Mapping[str, object]) -> Optional[str]:
    """Return a trimmed secret string, or ``None`` when absent."""
    raw = source.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_secret"),))
    token = raw.strip()
    if not token:
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "empty_secret"),))
    return token


def _env_url(key: str, source: Mapping[str, object]) -> Optional[str]:
    """Return a URL string, or ``None`` when absent. Validation happens later."""
    raw = source.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_url"),))
    token = raw.strip()
    if not token:
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "empty_url"),))
    return token


def _env_cidrs(key: str, source: Mapping[str, object]) -> tuple[str, ...]:
    """Parse a comma-separated CIDR list strictly; absent means empty."""
    raw = source.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, str):
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_cidr"),))
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    return tuple(entries)


def _env_origins(
    key: str,
    source: Mapping[str, object],
    environment: AppEnvironment,
) -> tuple[str, ...]:
    """Parse CORS origins from the environment with per-environment policy.

    * Absent variable: ``local``/``test`` fall back to the documented
      development default; ``staging``/``production`` fail (explicit
      allowlist required).
    * Wildcards are rejected (credentials are enabled).
    * Full validation (scheme, host, no path/credentials) happens in the
      pydantic model; parsing here only handles absent/empty input.
    """
    raw = source.get(key)
    if raw is None:
        if environment in _REQUIRE_CORS_ALLOWLIST:
            raise SettingsConfigurationError(
                (SettingsIssue(key.lower(), "required"),)
            )
        return DEFAULT_ALLOWED_ORIGINS
    if not isinstance(raw, str):
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_origins"),))
    if not raw.strip():
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_origins"),))
    try:
        return parse_cors_allowed_origins(raw)
    except ValueError:
        raise SettingsConfigurationError((SettingsIssue(key.lower(), "invalid_origins"),))


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    """Frozen, strictly validated application configuration.

    Direct construction never reads the environment; use :meth:`from_env` for
    environment-driven startup. Secrets are never rendered by ``repr`` or
    ``str`` and never included in sanitized error messages. The model is
    frozen and assignment-validated, so a running instance can never be
    mutated into an invalid state.
    """

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        validate_default=True,
        frozen=True,
        validate_assignment=True,
        hide_input_in_errors=True,
    )

    app_env: AppEnvironment = AppEnvironment.local

    # ── Supabase ────────────────────────────────────────────────────────────
    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[SecretStr] = None

    # ── Provider ────────────────────────────────────────────────────────────
    groq_api_key: SecretStr = Field(...)
    groq_api_key_2: Optional[SecretStr] = None

    # ── Admission / ledger ──────────────────────────────────────────────────
    admission_hmac_secret: SecretStr = Field(...)
    trusted_proxy_cidrs: tuple[str, ...] = ()

    # ── CORS ────────────────────────────────────────────────────────────────
    cors_allowed_origins: tuple[str, ...] = ()
    cors_allow_credentials: bool = True

    # ── Feature flags ───────────────────────────────────────────────────────
    archival_extraction_enabled: bool = False
    #: Explicit mode for vector memory retrieval (SentenceTransformer + RPC).
    #: When enabled, the embedding model is constructed at startup and the
    #: ``embeddings`` readiness component must pass before the instance serves
    #: traffic. When disabled, the model is never constructed and retrieval
    #: returns no entries by design.
    embeddings_retrieval_enabled: bool = False

    # ── Turn execution (validated by TurnExecutionConfig) ───────────────────
    turn_config: TurnExecutionConfig = Field(default_factory=TurnExecutionConfig.defaults)

    # ── Readiness ───────────────────────────────────────────────────────────
    readiness_database_timeout_ms: int = Field(default=3000)
    readiness_provider_timeout_ms: int = Field(default=1000)
    #: Bounded timeout for the Auth service availability probe (/health).
    #: The probe is a cheap HTTP GET that never depends on a user token and
    #: never reads user data; the transport timeout is aligned to this value.
    readiness_auth_timeout_ms: int = Field(default=1000)

    # ── Validators ──────────────────────────────────────────────────────────

    @field_validator(
        "groq_api_key",
        "groq_api_key_2",
        "supabase_service_role_key",
        "admission_hmac_secret",
        mode="before",
    )
    @classmethod
    def _validate_secret(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if not isinstance(value, str):
            raise _SettingsError("secret_type", "secret")
        if not value.strip():
            raise _SettingsError("secret_empty", "secret")
        return SecretStr(value)

    @field_validator("admission_hmac_secret")
    @classmethod
    def _validate_admission_secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode("utf-8")) < ADMISSION_SECRET_MIN_BYTES:
            raise _SettingsError("secret_too_short", "admission_hmac_secret")
        return value

    @field_validator("supabase_url", mode="before")
    @classmethod
    def _validate_url(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise _SettingsError("url_type", "supabase_url")
        token = value.strip()
        if not token:
            raise _SettingsError("url_empty", "supabase_url")
        parsed = urlparse(token)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise _SettingsError("url_invalid", "supabase_url")
        if parsed.username is not None or parsed.password is not None:
            raise _SettingsError("url_credentials", "supabase_url")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise _SettingsError("url_invalid", "supabase_url")
        return token.rstrip("/")

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _validate_origins(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            raise _SettingsError("origins_type", "cors_allowed_origins")
        if not isinstance(value, (tuple, list)):
            raise _SettingsError("origins_type", "cors_allowed_origins")
        normalized: list[str] = []
        seen: set[str] = set()
        for entry in value:
            if not isinstance(entry, str):
                raise _SettingsError("origins_type", "cors_allowed_origins")
            token = entry.strip().rstrip("/")
            if not token:
                continue
            if token == "*":
                raise _SettingsError("origins_wildcard", "cors_allowed_origins")
            parsed = urlparse(token)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise _SettingsError("origins_invalid", "cors_allowed_origins")
            if parsed.username is not None or parsed.password is not None:
                raise _SettingsError("origins_credentials", "cors_allowed_origins")
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise _SettingsError("origins_invalid", "cors_allowed_origins")
            key = token.lower()
            if key not in seen:
                seen.add(key)
                normalized.append(token)
        return tuple(normalized)

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def _validate_cidrs(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = tuple(entry.strip() for entry in value.split(",") if entry.strip())
        if not isinstance(value, (tuple, list)):
            raise _SettingsError("cidr_type", "trusted_proxy_cidrs")
        result: list[str] = []
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise _SettingsError("cidr_invalid", "trusted_proxy_cidrs")
            try:
                ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError:
                raise _SettingsError("cidr_invalid", "trusted_proxy_cidrs")
            result.append(entry.strip())
        return tuple(result)

    @field_validator(
        "archival_extraction_enabled",
        "embeddings_retrieval_enabled",
        "cors_allow_credentials",
        mode="before",
    )
    @classmethod
    def _validate_strict_bool(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise _SettingsError("boolean_type", "boolean")
        return value

    @field_validator("supabase_url", mode="after")
    @classmethod
    def _validate_environment_requires_supabase_url(
        cls, value: Optional[str], info
    ) -> Optional[str]:
        environment = info.data.get("app_env")
        if environment in _REQUIRE_SUPABASE and not value:
            raise _SettingsError("required", "supabase_url")
        return value

    @field_validator("supabase_service_role_key", mode="after")
    @classmethod
    def _validate_environment_requires_service_key(
        cls, value: Optional[SecretStr], info
    ) -> Optional[SecretStr]:
        environment = info.data.get("app_env")
        if environment in _REQUIRE_SUPABASE and value is None:
            raise _SettingsError("required", "supabase_service_role_key")
        return value

    @field_validator("cors_allowed_origins", mode="after")
    @classmethod
    def _validate_environment_requires_origins(
        cls, value: tuple[str, ...], info
    ) -> tuple[str, ...]:
        environment = info.data.get("app_env")
        if environment in _REQUIRE_CORS_ALLOWLIST and not value:
            raise _SettingsError("required", "cors_allowed_origins")
        return value

    @field_validator("readiness_database_timeout_ms", mode="before")
    @classmethod
    def _validate_database_timeout(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _SettingsError("timeout_type", "readiness_database_timeout_ms")
        if not (
            READINESS_DATABASE_TIMEOUT_MIN_MS
            <= value
            <= READINESS_DATABASE_TIMEOUT_MAX_MS
        ):
            raise _SettingsError("timeout_out_of_range", "readiness_database_timeout_ms")
        return value

    @field_validator("readiness_provider_timeout_ms", mode="before")
    @classmethod
    def _validate_provider_timeout(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _SettingsError("timeout_type", "readiness_provider_timeout_ms")
        if not (
            READINESS_PROVIDER_TIMEOUT_MIN_MS
            <= value
            <= READINESS_PROVIDER_TIMEOUT_MAX_MS
        ):
            raise _SettingsError("timeout_out_of_range", "readiness_provider_timeout_ms")
        return value

    @field_validator("readiness_auth_timeout_ms", mode="before")
    @classmethod
    def _validate_auth_timeout(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _SettingsError("timeout_type", "readiness_auth_timeout_ms")
        if not (
            READINESS_PROVIDER_TIMEOUT_MIN_MS
            <= value
            <= READINESS_PROVIDER_TIMEOUT_MAX_MS
        ):
            raise _SettingsError("timeout_out_of_range", "readiness_auth_timeout_ms")
        return value

    @field_validator("turn_config")
    @classmethod
    def _validate_turn_config(cls, value: object) -> TurnExecutionConfig:
        if not isinstance(value, TurnExecutionConfig):
            raise _SettingsError("turn_config_type", "turn_config")
        return value

    @model_validator(mode="after")
    def _validate_environment_combinations(self) -> "Settings":
        environment = self.app_env

        if environment is AppEnvironment.production:
            if self.supabase_url is not None:
                url_host = (urlparse(self.supabase_url).hostname or "").lower()
                if url_host in _LOCALHOST_HOSTS or url_host.endswith(".localhost"):
                    raise _SettingsError(
                        "url_localhost_production", "supabase_url"
                    )
            for origin in self.cors_allowed_origins:
                hostname = (urlparse(origin).hostname or "").lower()
                if (
                    hostname in _LOCALHOST_HOSTS
                    or hostname.endswith(".localhost")
                    or hostname.endswith(".local")
                ):
                    raise _SettingsError(
                        "origins_localhost_production", "cors_allowed_origins"
                    )

        # A feature that is enabled must have its required dependencies.
        if self.archival_extraction_enabled and not self._has_provider_key():
            raise _SettingsError("feature_missing_dependency", "archival_extraction_enabled")

        return self

    def _has_provider_key(self) -> bool:
        for candidate in (self.groq_api_key, self.groq_api_key_2):
            if candidate is not None and candidate.get_secret_value():
                return True
        return False

    # ── Factories ───────────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, object]] = None,
    ) -> "Settings":
        """Build settings strictly from an environment mapping.

        Fails with :class:`SettingsConfigurationError` (sanitized, no input
        values) on any invalid, missing, or unsafe configuration. Defaults to
        ``os.environ``.

        ``APP_ENV`` is **required**: an absent or empty value fails closed
        instead of silently selecting the least restrictive environment, so a
        deployment that forgets to declare its mode never runs production
        traffic under development defaults.
        """
        source: Mapping[str, object] = os.environ if env is None else env

        raw_env = source.get("APP_ENV")
        if raw_env is None or not str(raw_env).strip():
            raise SettingsConfigurationError(
                (SettingsIssue("app_env", "required"),)
            )
        try:
            environment = AppEnvironment(str(raw_env).strip().lower())
        except ValueError:
            raise SettingsConfigurationError(
                (SettingsIssue("app_env", "invalid_environment"),)
            )

        try:
            turn_config = TurnExecutionConfig.from_env(source)
        except ValueError:
            raise SettingsConfigurationError(
                (SettingsIssue("turn_config", "invalid_turn_configuration"),)
            )

        data: dict[str, object] = {
            "app_env": environment,
            "supabase_url": _env_url("SUPABASE_URL", source),
            "supabase_service_role_key": _env_secret(
                "SUPABASE_SERVICE_ROLE_KEY", source
            ),
            "groq_api_key": _env_secret("GROQ_API_KEY", source),
            "groq_api_key_2": _env_secret("GROQ_API_KEY_2", source),
            "admission_hmac_secret": _env_secret("ADMISSION_HMAC_SECRET", source),
            "trusted_proxy_cidrs": _env_cidrs("TRUSTED_PROXY_CIDRS", source),
            "cors_allowed_origins": _env_origins(
                "CORS_ALLOWED_ORIGINS", source, environment
            ),
            "archival_extraction_enabled": _env_bool(
                "ARCHIVAL_EXTRACTION_ENABLED", source, False
            ),
            "embeddings_retrieval_enabled": _env_bool(
                "EMBEDDINGS_RETRIEVAL_ENABLED", source, False
            ),
            "turn_config": turn_config,
            "readiness_database_timeout_ms": _env_int(
                "READINESS_DATABASE_TIMEOUT_MS",
                source,
                3000,
                READINESS_DATABASE_TIMEOUT_MIN_MS,
                READINESS_DATABASE_TIMEOUT_MAX_MS,
            ),
            "readiness_provider_timeout_ms": _env_int(
                "READINESS_PROVIDER_TIMEOUT_MS",
                source,
                1000,
                READINESS_PROVIDER_TIMEOUT_MIN_MS,
                READINESS_PROVIDER_TIMEOUT_MAX_MS,
            ),
            "readiness_auth_timeout_ms": _env_int(
                "READINESS_AUTH_TIMEOUT_MS",
                source,
                1000,
                READINESS_PROVIDER_TIMEOUT_MIN_MS,
                READINESS_PROVIDER_TIMEOUT_MAX_MS,
            ),
        }

        try:
            return cls(**data)
        except ValidationError as exc:
            raise _sanitize_validation_error(exc)

    def ensure_valid(self) -> None:
        """Re-validate the complete settings model (cheap, no I/O).

        Rebuilds the model from the current field values so every field-level,
        range, and cross-field validator runs again. Because the model is
        frozen and assignment-validated, a mutated or replaced instance fails
        here. Used by the readiness ``configuration`` check to confirm the
        settings instance the application runs with is still valid.
        """
        values = {
            name: getattr(self, name)
            for name in type(self).model_fields
        }
        type(self).model_validate(values)

    def to_admission_values(self) -> tuple[str, Optional[str]]:
        """Return ``(admission_secret, trusted_proxy_cidrs_csv)`` for admission."""
        secret = self.admission_hmac_secret.get_secret_value()
        cidrs = ",".join(self.trusted_proxy_cidrs) if self.trusted_proxy_cidrs else None
        return secret, cidrs

    def provider_keys(self) -> tuple[str, ...]:
        """Return the configured non-empty provider keys (never logged)."""
        keys: list[str] = []
        for candidate in (self.groq_api_key, self.groq_api_key_2):
            if candidate is not None:
                keys.append(candidate.get_secret_value())
        return tuple(keys)

    # ── Representation ──────────────────────────────────────────────────────

    def __repr__(self) -> str:
        visible = self.model_dump(
            exclude={
                "groq_api_key",
                "groq_api_key_2",
                "admission_hmac_secret",
                "supabase_service_role_key",
            }
        )
        return f"Settings({visible!r})"
