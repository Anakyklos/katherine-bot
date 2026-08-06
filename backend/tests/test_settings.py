"""Tests for the validated application settings model (#275)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.settings import (
    ADMISSION_SECRET_MIN_BYTES,
    AppEnvironment,
    Settings,
    SettingsConfigurationError,
    SettingsIssue,
)
from backend.turn_execution import TurnExecutionConfig

SECRET = "s" * ADMISSION_SECRET_MIN_BYTES


def _valid_kwargs(**overrides):
    kwargs = {
        "groq_api_key": "groq-key",
        "admission_hmac_secret": SECRET,
    }
    kwargs.update(overrides)
    return kwargs


def _production_kwargs(**overrides):
    kwargs = _valid_kwargs(
        app_env=AppEnvironment.production,
        supabase_url="https://db.example.com",
        supabase_service_role_key="service-key",
        cors_allowed_origins=("https://app.example.com",),
    )
    kwargs.update(overrides)
    return kwargs


# ─── 1. Valid settings per environment ──────────────────────────────────────


def test_valid_settings_accepted_for_every_environment():
    for environment in AppEnvironment:
        if environment in (AppEnvironment.staging, AppEnvironment.production):
            settings = _production_kwargs(app_env=environment)
        else:
            settings = _valid_kwargs(app_env=environment)
        assert Settings(**settings).app_env is environment


def test_valid_settings_from_env_for_local_environment(monkeypatch):
    env = {
        "APP_ENV": "local",
        "GROQ_API_KEY": "k",
        "GROQ_API_KEY_2": "k2",
        "ADMISSION_HMAC_SECRET": SECRET,
        "TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 192.168.0.0/16",
        "CORS_ALLOWED_ORIGINS": "https://a.example, https://a.example, http://localhost:3000",
        "ARCHIVAL_EXTRACTION_ENABLED": "false",
    }
    settings = Settings.from_env(env)
    assert settings.app_env is AppEnvironment.local
    assert settings.provider_keys() == ("k", "k2")
    assert settings.cors_allowed_origins == (
        "https://a.example",
        "http://localhost:3000",
    )  # normalized + deduplicated
    assert settings.trusted_proxy_cidrs == ("10.0.0.0/8", "192.168.0.0/16")
    assert settings.archival_extraction_enabled is False
    assert isinstance(settings.turn_config, TurnExecutionConfig)


def test_missing_app_env_fails_closed():
    """A deployment that forgets APP_ENV never silently runs in local mode."""
    env = {
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
    }
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    assert SettingsIssue("app_env", "required") in exc_info.value.issues

    env["APP_ENV"] = "   "
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    assert SettingsIssue("app_env", "required") in exc_info.value.issues


def test_explicit_environment_modes_are_accepted():
    """Explicit APP_ENV values work for every supported mode."""
    for environment in AppEnvironment:
        env = {
            "APP_ENV": environment.value,
            "GROQ_API_KEY": "k",
            "ADMISSION_HMAC_SECRET": SECRET,
        }
        if environment in (AppEnvironment.staging, AppEnvironment.production):
            env.update(
                {
                    "SUPABASE_URL": "https://db.example.com",
                    "SUPABASE_SERVICE_ROLE_KEY": "sk",
                    "CORS_ALLOWED_ORIGINS": "https://app.example.com",
                }
            )
        assert Settings.from_env(env).app_env is environment


def test_invalid_environment_is_rejected():
    env = {
        "APP_ENV": "preview",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
    }
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    assert SettingsIssue("app_env", "invalid_environment") in exc_info.value.issues


# ─── 2. Production fails early with missing critical config ─────────────────


def test_production_missing_supabase_url_fails_early():
    with pytest.raises(ValidationError):
        Settings(**_production_kwargs(supabase_url=None))


def test_production_missing_service_role_key_fails_early():
    with pytest.raises(ValidationError):
        Settings(**_production_kwargs(supabase_service_role_key=None))


def test_production_missing_cors_allowlist_fails_early():
    with pytest.raises(ValidationError):
        Settings(**_production_kwargs(cors_allowed_origins=()))


def test_production_from_env_missing_critical_config_fails_sanitized():
    env = {
        "APP_ENV": "production",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
    }
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    fields = {issue.field for issue in exc_info.value.issues}
    assert "supabase_url" in fields
    assert "supabase_service_role_key" in fields


# ─── 3. URL validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "ftp://db.example.com",
        "http://",
        "https://user:pass@db.example.com",
        "https://db.example.com/path",
        "https://db.example.com?x=1",
        "",
        "   ",
        "https://db.example.com:8000/",
    ],
)
def test_invalid_urls_rejected(url):
    if url == "https://db.example.com:8000/":
        # Trailing slash is normalized, not rejected.
        settings = Settings(**_valid_kwargs(supabase_url=url))
        assert settings.supabase_url == "https://db.example.com:8000"
        return
    with pytest.raises(ValidationError):
        Settings(**_valid_kwargs(supabase_url=url))


def test_valid_urls_accepted():
    for url in (
        "https://db.example.com",
        "http://127.0.0.1:54321",
        "https://project.supabase.co",
    ):
        settings = Settings(**_valid_kwargs(supabase_url=url))
        assert settings.supabase_url == url


# ─── 4/5. CORS policy ───────────────────────────────────────────────────────


def test_localhost_origin_rejected_in_production():
    for origin in (
        "http://localhost:3000",
        "https://127.0.0.1",
        "http://app.localhost",
    ):
        with pytest.raises(ValidationError):
            Settings(**_production_kwargs(cors_allowed_origins=(origin,)))


def test_localhost_origin_allowed_outside_production():
    settings = Settings(
        **_valid_kwargs(
            app_env=AppEnvironment.local,
            cors_allowed_origins=("http://localhost:3000",),
        )
    )
    assert settings.cors_allowed_origins == ("http://localhost:3000",)


def test_wildcard_origin_rejected_in_all_environments():
    for environment in AppEnvironment:
        kwargs = _production_kwargs(app_env=environment)
        with pytest.raises(ValidationError):
            Settings(**{**kwargs, "cors_allowed_origins": ("*",)})


def test_localhost_supabase_url_rejected_in_production():
    with pytest.raises(ValidationError):
        Settings(
            **_production_kwargs(
                supabase_url="http://127.0.0.1:54321",
            )
        )


def test_cors_origins_normalized_and_deduplicated():
    settings = Settings(
        **_valid_kwargs(
            cors_allowed_origins=(
                " https://a.example/ ",
                "https://a.example",
                "https://a.example/",
                "https://b.example",
            ),
        )
    )
    assert settings.cors_allowed_origins == (
        "https://a.example",
        "https://b.example",
    )


def test_origin_with_path_or_credentials_rejected():
    for origin in (
        "https://a.example/path",
        "https://a.example?q=1",
        "https://user@a.example",
    ):
        with pytest.raises(ValidationError):
            Settings(**_valid_kwargs(cors_allowed_origins=(origin,)))


# ─── 6. Strict types and ranges ─────────────────────────────────────────────


def test_numeric_timeouts_reject_out_of_range():
    with pytest.raises(ValidationError):
        Settings(**_valid_kwargs(readiness_database_timeout_ms=0))
    with pytest.raises(ValidationError):
        Settings(**_valid_kwargs(readiness_database_timeout_ms=100_000))
    with pytest.raises(ValidationError):
        Settings(**_valid_kwargs(readiness_provider_timeout_ms=-1))


def test_numeric_timeouts_reject_none_bool_and_numeric_strings():
    for value in (None, True, False, "3000", 3.5):
        with pytest.raises(ValidationError):
            Settings(**_valid_kwargs(readiness_database_timeout_ms=value))


def test_boolean_fields_reject_non_bool_values():
    for value in ("true", 1, 0, "false", None, 1.0):
        with pytest.raises(ValidationError):
            Settings(**_valid_kwargs(archival_extraction_enabled=value))
        with pytest.raises(ValidationError):
            Settings(**_valid_kwargs(cors_allow_credentials=value))


def test_boolean_fields_accept_only_bool():
    settings = Settings(**_valid_kwargs(archival_extraction_enabled=True))
    assert settings.archival_extraction_enabled is True


def test_from_env_rejects_permissive_boolean_parsing():
    env = {
        "APP_ENV": "local",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
        "ARCHIVAL_EXTRACTION_ENABLED": "1",
    }
    with pytest.raises(SettingsConfigurationError):
        Settings.from_env(env)

    env["ARCHIVAL_EXTRACTION_ENABLED"] = "yes"
    with pytest.raises(SettingsConfigurationError):
        Settings.from_env(env)


def test_from_env_rejects_empty_critical_values():
    env = {
        "APP_ENV": "local",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
        "CORS_ALLOWED_ORIGINS": "   ",
    }
    with pytest.raises(SettingsConfigurationError):
        Settings.from_env(env)

    env = {
        "APP_ENV": "local",
        "GROQ_API_KEY": "",
        "ADMISSION_HMAC_SECRET": SECRET,
    }
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    assert SettingsIssue("groq_api_key", "empty_secret") in exc_info.value.issues


def test_from_env_rejects_invalid_turn_configuration():
    env = {
        "APP_ENV": "local",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
        "TURN_MAX_ATTEMPTS": "not-a-number",
    }
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    assert SettingsIssue("turn_config", "invalid_turn_configuration") in exc_info.value.issues


def test_from_env_parses_embeddings_retrieval_flag():
    env = {
        "APP_ENV": "local",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
        "EMBEDDINGS_RETRIEVAL_ENABLED": "true",
    }
    assert Settings.from_env(env).embeddings_retrieval_enabled is True

    env["EMBEDDINGS_RETRIEVAL_ENABLED"] = "false"
    assert Settings.from_env(env).embeddings_retrieval_enabled is False

    del env["EMBEDDINGS_RETRIEVAL_ENABLED"]
    assert Settings.from_env(env).embeddings_retrieval_enabled is False


def test_secret_too_short_rejected():
    with pytest.raises(ValidationError):
        Settings(groq_api_key="k", admission_hmac_secret="short")


def test_empty_secrets_rejected():
    with pytest.raises(ValidationError):
        Settings(groq_api_key="", admission_hmac_secret=SECRET)
    with pytest.raises(ValidationError):
        Settings(groq_api_key="k", admission_hmac_secret="   ")


def test_invalid_proxy_cidr_rejected():
    with pytest.raises(ValidationError):
        Settings(**_valid_kwargs(trusted_proxy_cidrs=("not-a-cidr",)))


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        Settings(**_valid_kwargs(unexpected_field="x"))


# ─── 7. Secrets never appear in repr ────────────────────────────────────────


def test_repr_does_not_contain_secrets():
    settings = Settings(
        groq_api_key="groq-secret-value",
        groq_api_key_2="second-secret-value",
        admission_hmac_secret=SECRET,
        supabase_service_role_key="service-secret-value",
    )
    rendered = repr(settings)
    for secret in ("groq-secret-value", "second-secret-value", SECRET, "service-secret-value"):
        assert secret not in rendered


def test_str_does_not_contain_secrets():
    settings = Settings(groq_api_key="groq-secret-value", admission_hmac_secret=SECRET)
    assert "groq-secret-value" not in str(settings)
    assert SECRET not in str(settings)


# ─── 8. Sanitized validation errors ─────────────────────────────────────────


def test_from_env_errors_never_contain_input_values():
    secret_marker = "super-secret-marker-value-12345"
    env = {
        "APP_ENV": "production",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
        "SUPABASE_URL": "http://localhost:9",
        "SUPABASE_SERVICE_ROLE_KEY": secret_marker,
    }
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    rendered = str(exc_info.value)
    assert secret_marker not in rendered
    assert SECRET not in rendered
    assert "localhost:9" not in rendered


def test_from_env_errors_expose_only_field_and_code():
    env = {
        "APP_ENV": "production",
        "GROQ_API_KEY": "k",
        "ADMISSION_HMAC_SECRET": SECRET,
        "CORS_ALLOWED_ORIGINS": "http://localhost:3000",
        "SUPABASE_URL": "https://db.example.com",
        "SUPABASE_SERVICE_ROLE_KEY": "sk",
    }
    with pytest.raises(SettingsConfigurationError) as exc_info:
        Settings.from_env(env)
    assert SettingsIssue("cors_allowed_origins", "origins_localhost_production") in exc_info.value.issues
    assert exc_info.value.sanitized_details() == exc_info.value.issues


def test_settings_configuration_error_repr_never_contains_values():
    error = SettingsConfigurationError(
        (SettingsIssue("supabase_url", "url_invalid"),)
    )
    assert "http://" not in repr(error)
    assert "url_invalid" in repr(error)


# ─── 9. Direct construction never reads the environment ─────────────────────


def test_direct_construction_does_not_consult_environment(monkeypatch):
    def _fail_read(*_args, **_kwargs):
        raise AssertionError("environment must not be read during direct construction")

    monkeypatch.setattr("backend.settings.os.environ", {})
    monkeypatch.setattr("backend.settings.os.getenv", _fail_read)
    settings = Settings(groq_api_key="k", admission_hmac_secret=SECRET)
    assert settings.groq_api_key.get_secret_value() == "k"


def test_from_env_uses_provided_mapping_without_global_environment(monkeypatch):
    def _fail_read(*_args, **_kwargs):
        raise AssertionError("environment must not be read when a mapping is provided")

    monkeypatch.setattr("backend.settings.os.getenv", _fail_read)
    settings = Settings.from_env(
        {"APP_ENV": "local", "GROQ_API_KEY": "k", "ADMISSION_HMAC_SECRET": SECRET}
    )
    assert settings.app_env is AppEnvironment.local


# ─── 10. Immutability (frozen + assignment-validated) ───────────────────────


def test_settings_instance_is_immutable():
    """A constructed Settings can never be mutated into an invalid state."""
    settings = Settings(**_valid_kwargs())
    for mutate in (
        lambda s: setattr(s, "app_env", AppEnvironment.production),
        lambda s: setattr(s, "cors_allowed_origins", ("https://evil.example",)),
        lambda s: setattr(s, "readiness_database_timeout_ms", 999_999),
        lambda s: setattr(s, "groq_api_key", "mutated-secret"),
    ):
        with pytest.raises(ValidationError):
            mutate(settings)
    # The instance is still the original, valid one.
    settings.ensure_valid()
    assert settings.app_env is AppEnvironment.local


def test_ensure_valid_revalidates_the_complete_model():
    """ensure_valid() rebuilds and revalidates the full model, not just the
    cross-field validator."""
    settings = Settings(**_valid_kwargs())
    settings.ensure_valid()  # no-op for a valid frozen instance

    # A replaced settings reference on app.state is caught by the
    # configuration check because ensure_valid() revalidates everything.
    from backend.health import ConfigurationCheck

    async def _run():
        await ConfigurationCheck(settings).run()
        with pytest.raises(Exception):
            await ConfigurationCheck("not-a-settings").run()

    import asyncio

    asyncio.run(_run())


def test_direct_construction_errors_never_contain_sensitive_values():
    """hide_input_in_errors: a ValidationError from direct construction never
    renders URL credentials, keys, or other raw values."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            groq_api_key="SUPER-SECRET-KEY-MARKER-12345",
            admission_hmac_secret="short",
            supabase_url="https://user:pass@db.example.com",
        )
    rendered = str(exc_info.value)
    assert "SUPER-SECRET-KEY-MARKER-12345" not in rendered
    assert "user:pass" not in rendered
    assert "db.example.com" not in rendered
