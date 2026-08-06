"""Tests for the application factory, lifespan, and dependency container (#275)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import backend.main as main_module
from backend.admission import AdmissionRuntimeConfig
from backend.health import HealthRegistry
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


class RecordingResource:
    """Fake owned resource with a close() that records invocations."""

    def __init__(self, label="resource"):
        self.label = label
        self.close_calls = 0
        self.closed = False

    def close(self):
        self.close_calls += 1
        self.closed = True


class FakeGroqManager:
    def is_configured(self):
        return True


class FakeEngine:
    def __init__(self, supabase=None):
        self.memory_manager = SimpleNamespace(supabase=supabase)
        self.groq_manager = FakeGroqManager()


def _fake_dependencies(**overrides) -> main_module.ApplicationDependencies:
    settings = _settings()
    engine = FakeEngine(supabase=object())
    kwargs = {
        "conversation_engine": engine,
        "auth_client": engine.memory_manager.supabase,
        "admission_config": AdmissionRuntimeConfig.from_values(SECRET),
        "turn_config": TurnExecutionConfig.defaults(),
        "health_checks": HealthRegistry(),
        "clock": time.time,
    }
    kwargs.update(overrides)
    return main_module.ApplicationDependencies(**kwargs)


# ─── 17. create_app with settings and injected fakes ────────────────────────


def test_create_app_works_with_injected_settings_and_dependencies():
    settings = _settings()
    deps = _fake_dependencies()
    app = main_module.create_app(settings=settings, dependencies=deps)
    assert app.state.settings is settings
    assert app.state.dependencies is deps
    assert app.state.lifespan_started is True
    assert app.state.owned_resources == ()
    assert app.state.shutdown_completed is False


def test_create_app_without_dependencies_defers_construction_to_lifespan():
    app = main_module.create_app(settings=_settings())
    assert app.state.dependencies is None
    assert app.state.lifespan_started is False
    # No heavy resources were built by the factory itself.
    assert app.state.owned_resources == ()


def test_create_app_defaults_settings_from_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    monkeypatch.setenv("ADMISSION_HMAC_SECRET", SECRET)
    app = main_module.create_app()
    assert app.state.settings.app_env is AppEnvironment.local
    assert app.state.dependencies is None


def test_create_app_fails_closed_without_app_env(monkeypatch):
    """A runtime that forgets APP_ENV never starts in an implicit mode."""
    from backend.settings import SettingsConfigurationError

    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "env-key")
    monkeypatch.setenv("ADMISSION_HMAC_SECRET", SECRET)
    with pytest.raises(SettingsConfigurationError):
        main_module.create_app()


def test_create_app_with_default_dependencies_builds_owned_resources_once(monkeypatch):
    settings = _settings()
    built = []

    def fake_builder(settings_arg):
        built.append(settings_arg)
        engine = FakeEngine(supabase=object())
        deps = main_module.ApplicationDependencies(
            conversation_engine=engine,
            auth_client=object(),
            admission_config=AdmissionRuntimeConfig.from_values(SECRET),
            turn_config=TurnExecutionConfig.defaults(),
            health_checks=HealthRegistry(),
            clock=time.time,
        )
        return deps, (RecordingResource("owned-a"),)

    monkeypatch.setattr(main_module, "build_default_dependencies", fake_builder)
    app = main_module.create_app(settings=settings)
    with TestClient(app):
        pass
    assert len(built) == 1
    assert app.state.dependencies is not None


# ─── 18/19. Lifespan ownership and close counts ─────────────────────────────


def test_lifespan_closes_each_owned_resource_exactly_once(monkeypatch):
    settings = _settings()
    owned_a = RecordingResource("a")
    owned_b = RecordingResource("b")

    def fake_builder(settings_arg):
        engine = FakeEngine(supabase=object())
        deps = main_module.ApplicationDependencies(
            conversation_engine=engine,
            auth_client=object(),
            admission_config=AdmissionRuntimeConfig.from_values(SECRET),
            turn_config=TurnExecutionConfig.defaults(),
            health_checks=HealthRegistry(),
            clock=time.time,
        )
        return deps, (owned_a, owned_b)

    monkeypatch.setattr(main_module, "build_default_dependencies", fake_builder)
    app = main_module.create_app(settings=settings)
    with TestClient(app):
        assert owned_a.close_calls == 0
        assert owned_b.close_calls == 0
    assert owned_a.close_calls == 1
    assert owned_b.close_calls == 1
    assert owned_a.closed and owned_b.closed
    # Idempotent shutdown: a second lifespan run closes nothing again.
    with TestClient(app):
        pass
    assert owned_a.close_calls == 1
    assert owned_b.close_calls == 1


def test_injected_dependencies_are_never_closed():
    calls = []

    class EngineWithClose(FakeEngine):
        def close(self):
            calls.append(self)

    engine = EngineWithClose(supabase=object())
    deps = _fake_dependencies(conversation_engine=engine)
    app = main_module.create_app(settings=_settings(), dependencies=deps)
    with TestClient(app):
        pass
    # The caller keeps ownership of injected resources.
    assert calls == []
    assert app.state.owned_resources == ()


def test_aclose_and_close_contracts_both_supported():
    from backend.dependencies import close_resource

    class SyncClose:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class AsyncClose:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    async def _run():
        sync_resource = SyncClose()
        async_resource = AsyncClose()
        await close_resource(sync_resource)
        await close_resource(async_resource)
        assert sync_resource.closed
        assert async_resource.closed
        # Idempotent per-resource: closing again is a no-op by contract.
        await close_resource(sync_resource)

    import asyncio

    asyncio.run(_run())


# ─── 21. Partial startup failure ────────────────────────────────────────────


def test_builder_cleans_partial_resources_when_startup_fails(monkeypatch):
    """The real default builder must close resources created before a failure."""
    import backend.dependencies as dependencies_module

    created = []
    closed = []

    class FakeEngineWithClose:
        def __init__(self, *args, **kwargs):
            self.groq_manager = object()
            self.memory_manager = SimpleNamespace(supabase=object())
            created.append(self)

        def close(self):
            closed.append(self)

    monkeypatch.setattr(dependencies_module, "ChatConversationEngine", FakeEngineWithClose)

    def boom(settings, engine, **kwargs):
        raise RuntimeError("late startup failure")

    monkeypatch.setattr(dependencies_module, "build_health_registry", boom)
    with pytest.raises(RuntimeError):
        dependencies_module.build_default_dependencies(_settings())
    assert len(created) == 1
    assert closed == created


def test_lifespan_startup_failure_leaves_app_not_ready(monkeypatch, caplog):
    settings = _settings()

    def failing_builder(settings_arg):
        raise RuntimeError("startup exploded")

    monkeypatch.setattr(main_module, "build_default_dependencies", failing_builder)
    app = main_module.create_app(settings=settings)
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass
    assert app.state.dependencies is None
    assert app.state.lifespan_started is False
    # Startup failure is observable through the sanitized event.
    assert "event=app_startup_failed" in caplog.text


# ─── 22. Shutdown continues past individual failures ────────────────────────


def test_shutdown_continues_when_one_resource_fails(monkeypatch):
    settings = _settings()

    class FailingResource(RecordingResource):
        def close(self):
            super().close()
            raise RuntimeError("close exploded")

    failing = FailingResource("failing")
    ok = RecordingResource("ok")

    def fake_builder(settings_arg):
        engine = FakeEngine(supabase=object())
        deps = main_module.ApplicationDependencies(
            conversation_engine=engine,
            auth_client=object(),
            admission_config=AdmissionRuntimeConfig.from_values(SECRET),
            turn_config=TurnExecutionConfig.defaults(),
            health_checks=HealthRegistry(),
            clock=time.time,
        )
        return deps, (failing, ok)

    monkeypatch.setattr(main_module, "build_default_dependencies", fake_builder)
    app = main_module.create_app(settings=settings)
    with TestClient(app):
        pass  # shutdown must not raise
    assert failing.closed
    assert ok.closed
    assert ok.close_calls == 1


# ─── 23. No per-user state in singletons or app.state ───────────────────────


def test_no_per_user_state_in_app_state_or_container():
    app = main_module.create_app(settings=_settings(), dependencies=_fake_dependencies())
    state_keys = set(app.state._state)
    assert state_keys <= {
        "settings",
        "dependencies",
        "lifespan_started",
        "owned_resources",
        "shutdown_completed",
    }
    deps = app.state.dependencies
    container_fields = {
        "conversation_engine",
        "auth_client",
        "admission_config",
        "turn_config",
        "health_checks",
        "clock",
        "persistence_client",
    }
    assert set(deps.__dataclass_fields__) == container_fields
    # No attribute of the container may hold a user identity.
    for field_name in container_fields:
        assert "user" not in field_name.lower()
        assert "request" not in field_name.lower()


# ─── DI: routes use only the injected dependency surfaces (review blocker 2) ─


def test_routes_use_only_the_correct_dependency():
    """Auth uses only ``auth_client``; history/admission use only
    ``persistence_client``; routes never navigate engine internals."""
    from types import SimpleNamespace as NS

    from backend.emotion_presentation import EmotionStateResponse
    from backend.process_turn import ProcessTurnResult

    auth_calls = []
    persist_calls = []

    class AuthOnlyClient:
        """Exposes ONLY the auth surface; any other use fails loudly."""

        @property
        def auth(self):
            return self

        def get_user(self, token):
            auth_calls.append(token)
            return NS(user=NS(id="user-123"))

    class PersistenceOnlyClient:
        """Exposes ONLY rpc/table persistence; no auth surface at all."""

        def rpc(self, name, params):
            persist_calls.append(("rpc", name))
            return NS(
                execute=lambda: NS(
                    data=[{"decision": "admitted", "retry_after_seconds": 0}]
                )
            )

        def table(self, name):
            persist_calls.append(("table", name))
            return self

        def select(self, cols):
            return self

        def eq(self, key, value):
            return self

        def order(self, col, **kwargs):
            return self

        def limit(self, n):
            return self

        def execute(self):
            return NS(data=[{"content": "msg1", "role": "user"}], error=None)

    class GuardedMemoryManager:
        @property
        def supabase(self):
            raise AssertionError("routes must not navigate engine internals")

    class GuardedEngine:
        def __init__(self):
            self.memory_manager = GuardedMemoryManager()
            self.groq_manager = FakeGroqManager()
            self.turn_calls = []

        async def process_turn(
            self, user_id, message, request_id, *, budget=None, mode=None, correlation=None
        ):
            self.turn_calls.append((user_id, message, request_id))
            return ProcessTurnResult(
                committed=object(),
                response="di response",
                emotion_state=EmotionStateResponse(
                    schema_version=1,
                    mood_label="NEUTRA",
                    pad={"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
                    dominant_emotions=[],
                    timestamp=1000.0,
                ),
            )

    engine = GuardedEngine()
    auth_client = AuthOnlyClient()
    persistence_client = PersistenceOnlyClient()
    deps = main_module.ApplicationDependencies(
        conversation_engine=engine,
        auth_client=auth_client,
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
        clock=time.time,
        persistence_client=persistence_client,
    )
    app = main_module.create_app(settings=_settings(), dependencies=deps)
    client = TestClient(app)

    # History: auth via auth_client, query via persistence_client only.
    history = client.get("/history", headers={"Authorization": "Bearer t"})
    assert history.status_code == 200
    assert history.json() == [{"content": "msg1", "role": "user"}]
    assert auth_calls == ["t"]
    assert ("table", "chat_logs") in persist_calls

    # Chat: auth via auth_client, admission RPC via persistence_client only,
    # and the engine receives the turn.
    auth_calls.clear()
    persist_calls.clear()
    chat = client.post(
        "/chat",
        json={"request_id": "550e8400-e29b-41d4-a716-446655440000", "message": "hi"},
        headers={"Authorization": "Bearer t"},
    )
    assert chat.status_code == 200
    assert chat.json()["response"] == "di response"
    assert auth_calls == ["t"]
    assert ("rpc", "reserve_admission") in persist_calls
    assert len(engine.turn_calls) == 1
    assert engine.turn_calls[0][0] == "user-123"


# ─── CORS (35-38) ───────────────────────────────────────────────────────────


def _cors_client(settings):
    app = main_module.create_app(settings=settings, dependencies=_fake_dependencies())
    return TestClient(app)


def test_allowed_origin_receives_expected_headers():
    client = _cors_client(_settings(cors_allowed_origins=("https://allowed.example",)))
    response = client.get("/health", headers={"Origin": "https://allowed.example"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://allowed.example"
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_disallowed_origin_does_not_receive_authorization():
    client = _cors_client(_settings(cors_allowed_origins=("https://allowed.example",)))
    response = client.get("/health", headers={"Origin": "https://denied.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_unsafe_configuration_never_starts_in_production():
    with pytest.raises(ValidationError):
        _settings(
            app_env=AppEnvironment.production,
            supabase_url="https://db.example.com",
            supabase_service_role_key="sk",
            cors_allowed_origins=("http://localhost:3000",),
        )
    with pytest.raises(ValidationError):
        _settings(
            app_env=AppEnvironment.production,
            supabase_url="https://db.example.com",
            supabase_service_role_key="sk",
            cors_allowed_origins=("*",),
        )
    with pytest.raises(ValidationError):
        _settings(
            app_env=AppEnvironment.production,
            supabase_url="https://db.example.com",
            cors_allowed_origins=("https://app.example.com",),
        )


def test_valid_production_configuration_starts():
    settings = _settings(
        app_env=AppEnvironment.production,
        supabase_url="https://db.example.com",
        supabase_service_role_key="sk",
        cors_allowed_origins=("https://app.example.com",),
    )
    client = _cors_client(settings)
    response = client.get("/health", headers={"Origin": "https://app.example.com"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://app.example.com"


def test_local_configuration_continues_working():
    client = _cors_client(
        _settings(cors_allowed_origins=("http://localhost:3000",))
    )
    response = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_methods_and_headers_are_minimal():
    client = _cors_client(_settings(cors_allowed_origins=("https://allowed.example",)))
    response = client.options(
        "/chat",
        headers={
            "Origin": "https://allowed.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    allow_methods = response.headers.get("access-control-allow-methods", "")
    assert "POST" in allow_methods and "GET" in allow_methods
    assert "*" not in allow_methods
    allow_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allow_headers
    assert "content-type" in allow_headers
    assert "*" not in allow_headers
