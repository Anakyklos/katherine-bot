"""Tests for /live, /ready, /health and the readiness check registry (#275)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.health import (
    CheckStatus,
    ConfigurationCheck,
    DatabaseCheck,
    EmbeddingsCheck,
    HealthRegistry,
    LifespanCheck,
    ProviderCheck,
    build_health_registry,
    build_ready_response,
)
from backend.settings import AppEnvironment, Settings
from backend.turn_execution import TurnExecutionConfig

SECRET = "s" * 40


def _settings(**overrides) -> Settings:
    kwargs = {
        "app_env": AppEnvironment.local,
        "groq_api_key": "groq-key",
        "admission_hmac_secret": SECRET,
        "cors_allowed_origins": ("https://allowed.example",),
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


class OkCheck:
    def __init__(self, name, timeout_seconds=1.0):
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.runs = 0

    async def run(self):
        self.runs += 1


class FailCheck:
    def __init__(self, name, timeout_seconds=1.0):
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.runs = 0

    async def run(self):
        self.runs += 1
        from backend.health import CheckFailure

        raise CheckFailure()


def _deps_with_checks(checks):
    from backend.admission import AdmissionRuntimeConfig

    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(supabase=object()),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    return main_module.ApplicationDependencies(
        conversation_engine=engine,
        auth_client=object(),
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(checks),
        clock=__import__("time").time,
    )


def _client(settings=None, checks=None):
    settings = settings or _settings()
    checks = checks or {
        "database": OkCheck("database"),
        "provider": OkCheck("provider"),
    }
    app = main_module.create_app(
        settings=settings, dependencies=_deps_with_checks(checks)
    )
    return TestClient(app), checks


# ─── 24/25. /live ───────────────────────────────────────────────────────────


def test_live_returns_200_even_when_dependencies_are_down():
    checks = {
        "database": FailCheck("database"),
        "provider": FailCheck("provider"),
    }
    client, _ = _client(checks=checks)
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_live_never_runs_external_checks():
    checks = {
        "database": OkCheck("database"),
        "provider": OkCheck("provider"),
    }
    client, checks = _client(checks=checks)
    response = client.get("/live")
    assert response.status_code == 200
    assert checks["database"].runs == 0
    assert checks["provider"].runs == 0


# ─── 26-30. /ready components ───────────────────────────────────────────────


def test_ready_returns_200_only_when_all_critical_components_pass():
    client, _ = _client()
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["components"] == {
        "database": "ok",
        "provider": "ok",
        "lifespan": "ok",
    }


def test_ready_database_unavailable_returns_503_with_sanitized_body():
    checks = {"database": FailCheck("database"), "provider": OkCheck("provider")}
    client, _ = _client(checks=checks)
    response = client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["components"]["database"] == "unavailable"
    assert body["components"]["provider"] == "ok"


def test_ready_provider_unavailable_blocks_ready():
    checks = {"database": OkCheck("database"), "provider": FailCheck("provider")}
    client, _ = _client(checks=checks)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_optional_feature_disabled_does_not_block_readiness():
    settings = _settings(archival_extraction_enabled=False)
    client, _ = _client(settings=settings)
    response = client.get("/ready")
    assert response.status_code == 200
    assert "embeddings" not in response.json()["components"]


def test_enabled_required_feature_unavailable_blocks_readiness():
    settings = _settings(archival_extraction_enabled=True)
    checks = {
        "database": OkCheck("database"),
        "provider": OkCheck("provider"),
        "embeddings": FailCheck("embeddings"),
    }
    client, _ = _client(settings=settings, checks=checks)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["components"]["embeddings"] == "unavailable"


def test_enabled_required_feature_available_passes_readiness():
    settings = _settings(archival_extraction_enabled=True)
    checks = {
        "database": OkCheck("database"),
        "provider": OkCheck("provider"),
        "embeddings": OkCheck("embeddings"),
    }
    client, _ = _client(settings=settings, checks=checks)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["components"]["embeddings"] == "ok"


def test_ready_before_lifespan_returns_503():
    app = main_module.create_app(
        settings=_settings(),
        dependencies=_deps_with_checks(
            {"database": OkCheck("database"), "provider": OkCheck("provider")}
        ),
    )
    app.state.dependencies = None  # simulate startup not completed
    app.state.lifespan_started = False
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


# ─── 31. Timeout ────────────────────────────────────────────────────────────


class SlowCheck:
    def __init__(self, name, timeout_seconds=0.05):
        self.name = name
        self.timeout_seconds = timeout_seconds

    async def run(self):
        await asyncio.sleep(5.0)


def test_slow_check_expires_at_approved_timeout():
    checks = {
        "database": SlowCheck("database"),
        "provider": OkCheck("provider"),
    }
    client, _ = _client(checks=checks)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["components"]["database"] == "unavailable"
    assert response.json()["components"]["provider"] == "ok"


# ─── 32. Sensitive marker sanitization ──────────────────────────────────────


class LeakyCheck:
    def __init__(self, name, timeout_seconds=1.0):
        self.name = name
        self.timeout_seconds = timeout_seconds

    async def run(self):
        raise Exception("SENSITIVE_PROVIDER_MARKER_XYZ")


def test_sensitive_exception_never_appears_in_response_or_logs(caplog):
    checks = {"database": LeakyCheck("database"), "provider": OkCheck("provider")}
    client, _ = _client(checks=checks)
    with caplog.at_level(logging.ERROR):
        response = client.get("/ready")
    assert response.status_code == 503
    assert "SENSITIVE_PROVIDER_MARKER_XYZ" not in response.text
    assert "SENSITIVE_PROVIDER_MARKER_XYZ" not in caplog.text
    # The sanitized failure event carries only the component name.
    assert "event=readiness_check_failed component=database" in caplog.text


# ─── 33. Deterministic schema and order ─────────────────────────────────────


def test_ready_response_schema_and_order_are_deterministic():
    checks = {
        "configuration": OkCheck("configuration"),
        "database": OkCheck("database"),
        "provider": OkCheck("provider"),
    }
    client, _ = _client(checks=checks)
    first = client.get("/ready").json()
    second = client.get("/ready").json()
    assert first == second
    assert list(first["components"].keys()) == ["configuration", "database", "provider", "lifespan"]
    assert set(first.keys()) == {"status", "components"}


def test_build_ready_response_contract():
    from backend.health import CheckResult

    results = [
        CheckResult("database", CheckStatus.ok),
        CheckResult("provider", CheckStatus.unavailable),
    ]
    status, body = build_ready_response(results, lifespan_started=True)
    assert status == 503
    assert body == {
        "status": "not_ready",
        "components": {"database": "ok", "provider": "unavailable", "lifespan": "ok"},
    }
    status, body = build_ready_response(results, lifespan_started=False)
    assert status == 503
    assert body["components"]["lifespan"] == "unavailable"


# ─── 34. /health legacy semantics ───────────────────────────────────────────


def test_health_is_process_alive_alias_and_never_asserts_readiness():
    checks = {"database": FailCheck("database"), "provider": FailCheck("provider")}
    client, _ = _client(checks=checks)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert "ready" not in response.text
    # It must not run readiness checks either.
    assert checks["database"].runs == 0
    assert checks["provider"].runs == 0


# ─── Default registry builder ───────────────────────────────────────────────


def test_default_registry_component_set_and_order():
    settings = _settings()
    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(embedding_model=object(), supabase=object()),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    registry = build_health_registry(settings, engine, object())
    assert registry.names() == ("configuration", "database", "provider")


def test_default_registry_includes_embeddings_only_when_enabled():
    settings = _settings(archival_extraction_enabled=True)
    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(embedding_model=object(), supabase=object()),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    registry = build_health_registry(settings, engine, object())
    assert registry.names() == ("configuration", "database", "provider", "embeddings")


def test_embeddings_check_fails_when_model_missing():
    check = EmbeddingsCheck(SimpleNamespace(embedding_model=None))

    async def _run():
        with pytest.raises(Exception):
            await check.run()

    asyncio.run(_run())


def test_provider_check_fails_when_manager_unconfigured():
    check = ProviderCheck(SimpleNamespace(is_configured=lambda: False), 1.0)

    async def _run():
        with pytest.raises(Exception):
            await check.run()

    asyncio.run(_run())


def test_database_check_fails_without_client():
    check = DatabaseCheck(None, 1.0)

    async def _run():
        with pytest.raises(Exception):
            await check.run()

    asyncio.run(_run())


def test_configuration_check_fails_without_settings():
    check = ConfigurationCheck(None)

    async def _run():
        with pytest.raises(Exception):
            await check.run()

    asyncio.run(_run())


def test_lifespan_check_reflects_state():
    state = {"started": True}
    check = LifespanCheck(lambda: state["started"])

    async def _run():
        await check.run()
        state["started"] = False
        with pytest.raises(Exception):
            await check.run()

    asyncio.run(_run())
