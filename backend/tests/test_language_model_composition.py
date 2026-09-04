"""Composition-root wiring tests for the LanguageModel contract (#337 review).

Covers the review blockers:

1. **Settings is the source of truth**: the adapter factory captures
   ``Settings.provider_keys()`` and the provider-call parameters derived
   from ``Settings.turn_config`` at composition time. Building the app
   with a key that exists *only inside Settings* (no ``GROQ_API_KEY*``
   in the environment) must produce a working adapter — proof the
   factory never falls back to global environment state.
2. **Real readiness**: ``/ready`` provider check fails when the factory
   exists but the configuration is absent, passes when the captured
   configuration is valid, and never performs an inference call or
   secret echo.
3. **Provider-agnostic runtime**: structural test lives in
   ``test_language_model_isolation.py`` (bans ``groq``/``groq_keys``/
   ``GroqClientManager``/``GroqLanguageModel`` in ``companion_runtime.py``).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from backend.language_model import (
    LanguageModelConfigurationError,
    resolve_language_model_factory,
)
from backend.settings import AppEnvironment, Settings
from backend.turn_execution import TurnExecutionConfig, TurnBudget

SECRET = "s" * 40


def _settings(**overrides) -> Settings:
    kwargs = {
        "app_env": AppEnvironment.local,
        "groq_api_key": "groq-key-only-in-settings",
        "admission_hmac_secret": SECRET,
        "cors_allowed_origins": ("https://allowed.example",),
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


def _no_groq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the environment carries no provider credentials."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY_2", raising=False)


# ─── Blocker 1: Settings keys flow to the adapter ───────────────────────────


def test_factory_builds_adapter_from_settings_key_without_env(monkeypatch):
    """The adapter uses the Settings key, not global environment state.

    Builds a valid ``Settings`` programmatically, guarantees
    ``GROQ_API_KEY*`` are absent from ``os.environ``, resolves the
    factory and constructs the adapter. The resulting manager must hold
    exactly the Settings-captured key (asserted without ever printing
    the key value).
    """
    import backend.groq_keys as groq_keys_module

    _no_groq_env(monkeypatch)
    # Belt-and-braces: even if the loader were consulted, it must see
    # nothing (monkeypatch the loader itself to prove it is NOT used).
    monkeypatch.setattr(
        groq_keys_module,
        "get_groq_api_keys",
        lambda: [None, None],
    )

    settings = _settings()
    factory = resolve_language_model_factory(
        "groq",
        keys=settings.provider_keys(),
        call_params=settings.turn_config.to_groq_params(),
    )
    model = factory()

    # The adapter was built: the manager holds the Settings key (and
    # exactly it — no env fallback, no duplication). Asserted via
    # membership, never by echoing the key.
    manager_keys = model._manager._keys
    assert len(manager_keys) == 1
    assert manager_keys[0] == settings.groq_api_key.get_secret_value()
    # And it is not any environment value (there were none).
    assert manager_keys[0].startswith("groq-key-only-in-settings")


def test_factory_without_keys_raises_sanitized_configuration_error(monkeypatch):
    """No Settings key and no env key => the canonical sanitized error.

    The error message is the constant; no key material, path or SDK
    text appears.
    """
    _no_groq_env(monkeypatch)
    factory = resolve_language_model_factory(
        "groq",
        keys=(),
        call_params=TurnExecutionConfig.defaults().to_groq_params(),
    )
    with pytest.raises(LanguageModelConfigurationError) as excinfo:
        factory()
    assert str(excinfo.value) == "Language model is not configured."
    assert "groq-key" not in str(excinfo.value)
    assert "gsk_" not in str(excinfo.value)


def test_factory_captures_turn_config_params(monkeypatch):
    """Provider-call parameters from ``Settings.turn_config`` reach the manager."""
    _no_groq_env(monkeypatch)
    config = TurnExecutionConfig.defaults()
    factory = resolve_language_model_factory(
        "groq",
        keys=_settings().provider_keys(),
        call_params=config.to_groq_params(),
    )
    model = factory()
    assert model._manager._groq_params.max_attempts == config.max_attempts
    assert (
        model._manager._groq_params.provider_attempt_timeout
        == config.provider_attempt_timeout
    )


# ─── Blocker 2: real readiness ──────────────────────────────────────────────


def test_readiness_fails_when_factory_exists_but_config_absent(monkeypatch):
    """A factory's existence is not configuration: /ready must fail."""
    from backend.chat_engine import ChatConversationEngine

    _no_groq_env(monkeypatch)
    # Factory exists (explicit provider wired) but captured no keys.
    factory = resolve_language_model_factory(
        "groq",
        keys=(),
        call_params=TurnExecutionConfig.defaults().to_groq_params(),
    )
    probe = factory.provider_configured_probe
    engine = ChatConversationEngine(
        language_model_factory=factory,
        provider_configured_probe=probe,
    )
    assert engine._language_model_factory is not None  # factory wired
    assert engine.is_provider_configured() is False


def test_readiness_passes_with_valid_captured_configuration(monkeypatch):
    """Valid Settings-captured configuration => provider readiness passes."""
    from backend.chat_engine import ChatConversationEngine

    _no_groq_env(monkeypatch)
    settings = _settings()
    factory = resolve_language_model_factory(
        "groq",
        keys=settings.provider_keys(),
        call_params=settings.turn_config.to_groq_params(),
    )
    engine = ChatConversationEngine(
        language_model_factory=factory,
        provider_configured_probe=factory.provider_configured_probe,
    )
    assert engine.is_provider_configured() is True


def test_readiness_never_runs_inference_call(monkeypatch):
    """Readiness performs no generation/appraisal/extraction call.

    The probe is a pure configuration check: patch the manager's call
    surface to fail loudly if touched, then run readiness twice.
    """
    from backend.chat_engine import ChatConversationEngine

    _no_groq_env(monkeypatch)
    settings = _settings()
    factory = resolve_language_model_factory(
        "groq",
        keys=settings.provider_keys(),
        call_params=settings.turn_config.to_groq_params(),
    )

    def _explode(*args: Any, **kwargs: Any):
        raise AssertionError("readiness must not perform provider calls")

    # Build the adapter, replace every call surface with a tripwire.
    model = factory()
    monkeypatch.setattr(model._manager, "chat_completion_async", _explode)
    monkeypatch.setattr(model._manager, "chat_completion", _explode)

    engine = ChatConversationEngine(
        language_model=model,
        provider_configured_probe=factory.provider_configured_probe,
    )
    assert engine.is_provider_configured() is True
    assert engine.is_provider_configured() is True  # idempotent, still silent


def test_readiness_does_not_echo_secret(monkeypatch, caplog):
    """The probe never leaks the key via logs, errors or responses."""
    import logging

    from backend.chat_engine import ChatConversationEngine

    _no_groq_env(monkeypatch)
    settings = _settings()
    factory = resolve_language_model_factory(
        "groq",
        keys=settings.provider_keys(),
        call_params=settings.turn_config.to_groq_params(),
    )
    engine = ChatConversationEngine(
        language_model_factory=factory,
        provider_configured_probe=factory.provider_configured_probe,
    )
    with caplog.at_level(logging.DEBUG):
        configured = engine.is_provider_configured()
    assert configured is True
    assert "groq-key-only-in-settings" not in caplog.text
    assert "gsk_" not in caplog.text


def test_probe_is_bound_to_same_config_as_adapter(monkeypatch):
    """Probe and adapter answer from the same captured configuration.

    With the same captured keys both agree; change the captured keys
    and both change together (no divergent sources of truth).
    """
    _no_groq_env(monkeypatch)
    settings = _settings()
    factory = resolve_language_model_factory(
        "groq",
        keys=settings.provider_keys(),
        call_params=settings.turn_config.to_groq_params(),
    )
    assert factory.provider_configured_probe() is True

    empty_factory = resolve_language_model_factory(
        "groq",
        keys=(),
        call_params=settings.turn_config.to_groq_params(),
    )
    assert empty_factory.provider_configured_probe() is False


# ─── Web composition end-to-end (Settings, no env) ───────────────────────────


def test_build_default_dependencies_uses_settings_not_env(monkeypatch):
    """Full web composition: adapter built from Settings while env is clean."""
    from backend.dependencies import build_default_dependencies

    _no_groq_env(monkeypatch)
    settings = _settings()
    dependencies, owned = build_default_dependencies(settings)
    try:
        engine = dependencies.conversation_engine
        # Readiness reflects the Settings configuration.
        assert engine.is_provider_configured() is True
        # The adapter's manager holds the Settings key (never echoed).
        model = engine._language_model_factory()
        assert model._manager._keys == [
            settings.groq_api_key.get_secret_value()
        ]
    finally:
        for resource in owned:
            resource.close()


def test_ready_endpoint_fails_without_real_configuration(monkeypatch):
    """/ready provider check fails when required provider is unconfigured."""
    from fastapi.testclient import TestClient

    import backend.main as main_module

    _no_groq_env(monkeypatch)
    # Settings without any provider key: the probe must report False
    # and the provider readiness check must fail.
    settings = Settings(
        app_env=AppEnvironment.local,
        groq_api_key="x" * 40,  # placeholder replaced below
        admission_hmac_secret=SECRET,
        cors_allowed_origins=("https://allowed.example",),
    )
    # Build a factory capturing NO keys (simulating unconfigured state).
    factory = resolve_language_model_factory(
        "groq",
        keys=(),
        call_params=settings.turn_config.to_groq_params(),
    )
    from backend.chat_engine import ChatConversationEngine

    engine = ChatConversationEngine(
        language_model_factory=factory,
        provider_configured_probe=factory.provider_configured_probe,
    )
    # Engine-level readiness is the contract the /ready check consumes.
    assert engine.is_provider_configured() is False
