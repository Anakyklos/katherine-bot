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


def _ok_auth_client():
    """Duck-typed auth surface whose exact route path (get_user) is callable."""
    return SimpleNamespace(auth=SimpleNamespace(get_user=lambda token: None))


def _ok_auth_probe():
    """A passing async Auth availability probe (no network)."""

    async def probe():
        return None

    return probe


def _ok_db_probe():
    """A passing async database availability probe (no network)."""

    async def probe():
        return None

    return probe


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


# ─── 31b. DatabaseCheck bounds the real blocking probe (review blocker 4) ────


class _ProbeResult:
    def __init__(self, data=None, error=None):
        self.data = data if data is not None else []
        self.error = error


class _OkProbeClient:
    """Duck-typed probe client that succeeds immediately."""

    def table(self, name):
        return self

    def select(self, cols):
        return self

    def limit(self, n):
        return self

    def rpc(self, name, params):
        return self

    def execute(self):
        return _ProbeResult(data=[])


def _slow_db_probe(started=None, release=None):
    """Build an async database probe that blocks until ``release`` is set."""
    import asyncio as _asyncio

    async def probe():
        if started is not None:
            started.set()
        if release is not None:
            await release.wait()
        return None

    return probe


def test_database_check_timeout_cancels_the_slow_probe():
    """A slow async probe is cancelled at the readiness timeout: no worker
    thread survives, no duplicate probe is stacked, and the check recovers
    once the probe can complete (review blocker 4)."""
    import asyncio as _asyncio

    calls = []
    release = _asyncio.Event()

    async def _blocking_probe():
        calls.append(1)
        await release.wait()

    check = DatabaseCheck(_blocking_probe, timeout_seconds=0.05)

    async def run_check():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    async def scenario():
        # First poll times out; the probe task was cancelled (no orphaned work).
        assert await run_check() == "unavailable"
        assert len(calls) == 1
        assert check._inflight is None

        # A fresh probe completes fine once the blocked probe is released.
        release.set()
        assert await run_check() == "ok"
        assert len(calls) == 2

    asyncio.run(scenario())


def test_database_check_fails_while_probe_in_flight_and_recovers():
    """A stuck probe makes readiness fail honestly; while a probe is in
    flight no duplicate is stacked, and once it terminates the next poll
    runs a fresh one (review blocker 4)."""
    import asyncio as _asyncio

    calls = []
    release = _asyncio.Event()

    async def _blocking_probe():
        calls.append(1)
        await release.wait()

    check = DatabaseCheck(_blocking_probe, timeout_seconds=0.05)

    async def run_check():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    async def scenario():
        # First poll times out and cancels its probe.
        assert await run_check() == "unavailable"
        assert len(calls) == 1
        assert check._inflight is None

        # A probe left in flight makes further polls fail fast (single-probe
        # guard) without stacking duplicate work.
        task = _asyncio.ensure_future(check._probe())
        check._inflight = task
        await _asyncio.sleep(0)
        assert len(calls) == 2  # timed-out probe #1 + the in-flight task #2
        assert await run_check() == "unavailable"
        assert await run_check() == "unavailable"
        assert len(calls) == 2, "no duplicate probe while one is in flight"
        task.cancel()
        import contextlib

        with contextlib.suppress(BaseException):
            await task

        # Once the probe can complete, the next poll recovers.
        release.set()
        assert await run_check() == "ok"
        assert len(calls) == 3

    asyncio.run(scenario())


# ─── 31c. Single-probe guard under concurrent polls (review blocker 4) ───────


def test_concurrent_polls_submit_single_probe():
    """Concurrent /ready polls while a probe is in flight submit exactly one
    probe; the rest fail fast (review blocker 4)."""
    import asyncio as _asyncio

    calls = []
    release = _asyncio.Event()

    async def _blocking_probe():
        calls.append(1)
        await release.wait()

    check = DatabaseCheck(_blocking_probe, timeout_seconds=0.1)

    async def run_check():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    async def hammer():
        return await _asyncio.gather(*(run_check() for _ in range(12)))

    results = asyncio.run(hammer())
    assert all(r == "unavailable" for r in results)
    assert len(calls) == 1, "concurrent polls must submit a single probe"

    release.set()
    assert asyncio.run(run_check()) == "ok"


# ─── 31d. aclose cleanup guarantees (review blockers 2 and 3) ───────────────


def test_aclose_cancels_inflight_probe():
    """aclose cancels and awaits an in-flight async probe; no fire-and-forget
    cleanup and no owned work survives (review blocker 3)."""
    import asyncio as _asyncio

    cancelled = []

    async def _slow_probe():
        try:
            await _asyncio.sleep(10)
        except _asyncio.CancelledError:
            cancelled.append(1)
            raise

    check = DatabaseCheck(_slow_probe, 1.0)

    async def scenario():
        # Start the probe directly (simulating an in-flight readiness poll).
        task = _asyncio.ensure_future(check._probe())
        check._inflight = task
        await _asyncio.sleep(0)
        await check.aclose()
        assert task.done()
        assert task.cancelled()
        assert cancelled == [1]
        assert check._inflight is None
        # A second aclose is a no-op.
        await check.aclose()

    asyncio.run(scenario())


def test_aclose_is_clean_after_expired_ready():
    """After /ready expires on a slow probe, the probe task was already
    cancelled by the check itself; aclose stays clean and idempotent."""
    import asyncio as _asyncio

    cancelled = []

    async def _slow_probe():
        try:
            await _asyncio.sleep(10)
        except _asyncio.CancelledError:
            cancelled.append(1)
            raise

    check = DatabaseCheck(_slow_probe, 0.01)

    async def scenario():
        try:
            await check.run()
        except Exception:
            pass
        assert cancelled == [1], "expired probe must be cancelled by run()"
        assert check._inflight is None
        await check.aclose()
        assert check._inflight is None

    asyncio.run(scenario())


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
    registry = build_health_registry(
        settings,
        engine,
        auth_client=_ok_auth_client(),
        auth_probe=_ok_auth_probe(),
        persistence_client=_OkProbeClient(),
        database_probe=_ok_db_probe(),
    )
    assert registry.names() == (
        "configuration",
        "auth",
        "database",
        "persistence",
        "provider",
    )


def test_default_registry_includes_embeddings_only_when_retrieval_enabled():
    settings = _settings(embeddings_retrieval_enabled=True)
    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(embedding_model=object(), supabase=object()),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    registry = build_health_registry(
        settings,
        engine,
        auth_client=_ok_auth_client(),
        auth_probe=_ok_auth_probe(),
        persistence_client=_OkProbeClient(),
        database_probe=_ok_db_probe(),
    )
    assert registry.names() == (
        "configuration",
        "auth",
        "database",
        "persistence",
        "provider",
        "embeddings",
    )


def test_embeddings_check_fails_when_model_missing():
    check = EmbeddingsCheck(SimpleNamespace(embedding_model=None))

    async def _run():
        with pytest.raises(Exception):
            await check.run()

    asyncio.run(_run())


# ─── 34c. Real auth/persistence surfaces are readiness-critical (blocker 1) ──


def test_auth_check_fails_when_surface_missing():
    """The auth check verifies the callable route path ``auth.get_user``
    AND an Auth availability probe; either missing is unavailable
    (review blocker 1)."""
    from backend.health import AuthClientCheck

    async def _run(check):
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    cases = [
        # Broken surfaces, even with a working probe.
        (AuthClientCheck(None, 1.0, probe=_ok_auth_probe()), "unavailable"),
        (AuthClientCheck(object(), 1.0, probe=_ok_auth_probe()), "unavailable"),
        (
            AuthClientCheck(
                SimpleNamespace(auth=object()), 1.0, probe=_ok_auth_probe()
            ),
            "unavailable",
        ),
        (
            AuthClientCheck(
                SimpleNamespace(auth=SimpleNamespace(get_user=None)),
                1.0,
                probe=_ok_auth_probe(),
            ),
            "unavailable",
        ),
        # Callable surface but no probe configured: unavailable (an instance
        # whose Auth cannot be probed must not report ready).
        (AuthClientCheck(_ok_auth_client(), 1.0, probe=None), "unavailable"),
        # Surface ok + probe ok: available.
        (
            AuthClientCheck(_ok_auth_client(), 1.0, probe=_ok_auth_probe()),
            "ok",
        ),
    ]
    try:
        for check, expected in cases:
            assert asyncio.run(_run(check)) == expected
    finally:
        for check, _ in cases:
            check.close()


def test_auth_check_fails_when_availability_probe_fails():
    """A callable auth surface with a failing Auth service probe is
    unavailable (review blocker 1)."""
    from backend.health import AuthClientCheck

    async def _failing_probe():
        raise RuntimeError("auth service unreachable")

    check = AuthClientCheck(_ok_auth_client(), 1.0, probe=_failing_probe)

    async def _run():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    try:
        assert asyncio.run(_run()) == "unavailable"
    finally:
        check.close()


def _auth_ready_client(auth_probe):
    """/ready client: callable auth surface, healthy DB/persistence probes,
    configurable Auth availability probe."""
    from backend.admission import AdmissionRuntimeConfig

    settings = _settings()
    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(embedding_model=object(), supabase=object()),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    registry = build_health_registry(
        settings,
        engine,
        auth_client=_ok_auth_client(),
        auth_probe=auth_probe,
        persistence_client=_OkProbeClient(),
        database_probe=_ok_db_probe(),
    )
    deps = main_module.ApplicationDependencies(
        conversation_engine=engine,
        auth_client=_ok_auth_client(),
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=registry,
        clock=__import__("time").time,
    )
    app = main_module.create_app(settings=settings, dependencies=deps)
    return TestClient(app), registry


def test_ready_503_when_auth_service_unavailable():
    """Callable auth surface + healthy database probe + Auth service down
    => /ready 503 with auth=unavailable (review blocker 1)."""
    async def _failing_probe():
        raise RuntimeError("auth service unreachable")

    client, registry = _auth_ready_client(_failing_probe)
    try:
        response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["components"]["auth"] == "unavailable"
        assert body["components"]["database"] == "ok"
        assert body["components"]["persistence"] == "ok"
    finally:
        registry.close()


def test_ready_200_when_auth_service_available():
    """Callable auth surface + healthy database probe + Auth service up
    => /ready 200 with auth=ok (review blocker 1)."""
    client, registry = _auth_ready_client(_ok_auth_probe())
    try:
        response = client.get("/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["components"]["auth"] == "ok"
    finally:
        registry.close()


def test_auth_health_probe_detects_service_availability():
    """The default probe performs a bounded GET /health and detects an
    unreachable Auth service without external network (review blocker 1)."""
    import http.server
    import threading

    from backend.health import CheckFailure, _auth_health_probe

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/auth/v1/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"version":"v2.0.0"}')
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        ok_probe = _auth_health_probe(f"{base}/auth/v1", None, 1.0)
        asyncio.run(ok_probe())  # healthy Auth service: no raise

        # Unreachable Auth service (connection refused): sanitized failure.
        bad_probe = _auth_health_probe("http://127.0.0.1:1/auth/v1", None, 1.0)
        with pytest.raises(CheckFailure):
            asyncio.run(bad_probe())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_persistence_check_fails_when_surface_missing():
    """The persistence check verifies callable, invocable ``table``/``rpc``
    surfaces, not mere attribute existence (review blocker 1)."""
    from backend.health import PersistenceClientCheck

    async def _run(check):
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    assert asyncio.run(_run(PersistenceClientCheck(None))) == "unavailable"
    assert asyncio.run(_run(PersistenceClientCheck(object()))) == "unavailable"
    # False positive fixed: table=None/rpc=None passes hasattr but cannot be
    # called by the routes.
    assert (
        asyncio.run(
            _run(PersistenceClientCheck(SimpleNamespace(table=None, rpc=None)))
        )
        == "unavailable"
    )
    # Invocation must not raise and must return a usable builder.
    assert asyncio.run(_run(PersistenceClientCheck(_OkProbeClient()))) == "ok"


def test_auth_and_persistence_surfaces_are_independent():
    """With distinct injected surfaces, readiness fails when either the auth
    or the persistence surface is missing even if the probe client works
    (review blocker 1)."""
    settings = _settings()
    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(embedding_model=object(), supabase=object()),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    auth = _ok_auth_client()
    persistence = _OkProbeClient()

    async def _statuses(registry):
        results = await registry.run_all()
        return {result.name: result.status.value for result in results}

    # Auth missing, persistence + probe present.
    registry = build_health_registry(
        settings,
        engine,
        auth_client=None,
        auth_probe=_ok_auth_probe(),
        persistence_client=persistence,
        database_probe=_ok_db_probe(),
    )
    by_name = asyncio.run(_statuses(registry))
    assert by_name["auth"] == "unavailable"
    assert by_name["persistence"] == "ok"
    assert by_name["database"] == "ok"

    # Persistence missing, auth + probe present.
    registry = build_health_registry(
        settings,
        engine,
        auth_client=auth,
        auth_probe=_ok_auth_probe(),
        persistence_client=None,
        database_probe=_ok_db_probe(),
    )
    by_name = asyncio.run(_statuses(registry))
    assert by_name["auth"] == "ok"
    assert by_name["persistence"] == "unavailable"
    assert by_name["database"] == "ok"


# ─── 34b. Embeddings lifecycle and readiness (review blocker 3) ──────────────


def _embeddings_client(retrieval_enabled: bool, model_available: bool):
    """Build a /ready client for the retrieval-mode × model-availability grid."""
    from backend.admission import AdmissionRuntimeConfig

    settings = _settings(embeddings_retrieval_enabled=retrieval_enabled)
    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(
            embedding_model=object() if model_available else None,
            supabase=object(),
        ),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    registry = build_health_registry(
        settings,
        engine,
        auth_client=_ok_auth_client(),
        auth_probe=_ok_auth_probe(),
        persistence_client=_OkProbeClient(),
        database_probe=_ok_db_probe(),
    )
    deps = main_module.ApplicationDependencies(
        conversation_engine=engine,
        auth_client=object(),
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=registry,
        clock=__import__("time").time,
    )
    app = main_module.create_app(settings=settings, dependencies=deps)
    return TestClient(app)


@pytest.mark.parametrize(
    ("retrieval_enabled", "model_available", "expected_status", "embeddings_present"),
    [
        # Feature off × model would be available → ready, no embeddings component.
        (False, True, 200, False),
        # Feature off × model unavailable → ready, no embeddings component.
        (False, False, 200, False),
        # Feature on × model available → ready with embeddings ok.
        (True, True, 200, True),
        # Feature on × model unavailable → NOT ready, embeddings fails honestly.
        (True, False, 503, True),
    ],
)
def test_embeddings_lifecycle_four_scenarios(
    retrieval_enabled,
    model_available,
    expected_status,
    embeddings_present,
):
    client = _embeddings_client(retrieval_enabled, model_available)
    response = client.get("/ready")
    assert response.status_code == expected_status
    body = response.json()
    components = body["components"]
    assert ("embeddings" in components) is embeddings_present
    if embeddings_present:
        expected = "ok" if model_available else "unavailable"
        assert components["embeddings"] == expected
        assert body["status"] == ("ready" if model_available else "not_ready")


def test_default_builder_never_constructs_embedding_model_when_disabled(monkeypatch):
    """Startup with retrieval disabled must not construct SentenceTransformer."""
    import backend.dependencies as dependencies_module
    import backend.memory as memory_module

    def _no_model(*_args, **_kwargs):
        raise AssertionError("embedding model must not be constructed when disabled")

    monkeypatch.setattr(memory_module, "SentenceTransformer", _no_model)
    settings = _settings(embeddings_retrieval_enabled=False)
    deps, _owned = dependencies_module.build_default_dependencies(settings)
    assert deps.conversation_engine.memory_manager.embedding_model is None


def test_memory_manager_constructs_model_only_when_enabled(monkeypatch):
    """Startup with retrieval enabled constructs the model; a failure surfaces
    as embedding_model is None (readiness then blocks traffic)."""
    import backend.memory as memory_module

    calls = []

    def _fake_model(*_args, **_kwargs):
        calls.append(1)
        return object()

    monkeypatch.setattr(memory_module, "SentenceTransformer", _fake_model)

    disabled = memory_module.MemoryManager(embeddings_enabled=False)
    assert disabled.embedding_model is None
    assert calls == []

    enabled = memory_module.MemoryManager(embeddings_enabled=True)
    assert enabled.embedding_model is not None
    assert calls == [1]

    def _failing_model(*_args, **_kwargs):
        raise RuntimeError("model download failed")

    monkeypatch.setattr(memory_module, "SentenceTransformer", _failing_model)
    broken = memory_module.MemoryManager(embeddings_enabled=True)
    assert broken.embedding_model is None


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
