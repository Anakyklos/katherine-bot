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


def test_database_check_timeout_bounds_the_real_blocking_probe():
    """Repeated polls never accumulate threads and recover once the probe's
    aligned transport releases the worker (review blocker 4)."""
    import concurrent.futures
    import threading
    import time as _time

    probe_started = threading.Event()
    release = threading.Event()
    calls = []

    class BlockingClient(_OkProbeClient):
        def execute(self):
            calls.append(_time.monotonic())
            probe_started.set()
            # Simulates the aligned transport timeout: the probe self-terminates
            # shortly after the registry-level await was cancelled.
            release.wait(timeout=1.0)
            return _ProbeResult(data=[])

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    check = DatabaseCheck(BlockingClient(), timeout_seconds=0.05, probe_executor=executor)

    async def run_check():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    # First poll times out at the readiness timeout while the thread runs.
    assert asyncio.run(run_check()) == "unavailable"
    assert probe_started.is_set()

    # While the probe is still in flight, repeated polls fail fast without
    # submitting new work: exactly one probe execution has been started.
    for _ in range(5):
        assert asyncio.run(run_check()) == "unavailable"
    assert len(calls) == 1, "no new probe may be submitted while one is in flight"

    # The in-flight probe completes on its own (aligned transport timeout).
    release.set()
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if asyncio.run(run_check()) == "ok":
            break
        _time.sleep(0.01)
    assert asyncio.run(run_check()) == "ok"

    # The executor never grew beyond its single worker: no thread accumulation.
    assert len(executor._threads) <= 1
    executor.shutdown(wait=False)


def test_database_check_fails_while_probe_in_flight_and_recovers():
    """A stuck probe makes readiness fail honestly, and recovery is automatic
    once the probe terminates (review blocker 4)."""
    import concurrent.futures
    import threading
    import time as _time

    release = threading.Event()
    probe_started = threading.Event()
    calls = []

    class StuckClient(_OkProbeClient):
        def execute(self):
            calls.append(1)
            probe_started.set()
            release.wait(timeout=1.0)
            return _ProbeResult(data=[])

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    check = DatabaseCheck(StuckClient(), timeout_seconds=0.05, probe_executor=executor)

    async def run_check():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    assert asyncio.run(run_check()) == "unavailable"
    assert probe_started.is_set()
    # A subsequent poll while in flight does not start a second probe.
    assert asyncio.run(run_check()) == "unavailable"
    assert len(calls) == 1

    release.set()
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if asyncio.run(run_check()) == "ok":
            break
        _time.sleep(0.01)
    assert asyncio.run(run_check()) == "ok"
    executor.shutdown(wait=False)


# ─── 31c. Probe guard race between concurrent requests (review blocker 4) ────


def test_concurrent_polls_submit_single_probe():
    """Concurrent /ready polls while a probe is in flight submit exactly one
    probe; the rest fail fast (review blocker 4)."""
    import asyncio
    import concurrent.futures
    import threading
    import time as _time

    release = threading.Event()
    calls = []

    class BlockingClient(_OkProbeClient):
        def execute(self):
            calls.append(1)
            release.wait(timeout=1.0)
            return _ProbeResult(data=[])

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    check = DatabaseCheck(BlockingClient(), timeout_seconds=0.1, probe_executor=executor)

    async def run_check():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    async def hammer():
        results = await asyncio.gather(*(run_check() for _ in range(12)))
        return results

    results = asyncio.run(hammer())
    assert all(r == "unavailable" for r in results)
    assert len(calls) == 1, "concurrent polls must submit a single probe"

    release.set()
    deadline = _time.monotonic() + 5.0
    while _time.monotonic() < deadline:
        if asyncio.run(run_check()) == "ok":
            break
        _time.sleep(0.01)
    assert asyncio.run(run_check()) == "ok"
    executor.shutdown(wait=False)


def test_probe_guard_preserves_newer_future_on_owner_finish():
    """The guarded clear only drops the future the calling poll owns.

    Deterministically reproduces the race: poll A's probe completes; before
    A's finally runs, poll B installs a newer probe future. A's finally must
    NOT clear B's reference, otherwise a third poll could submit extra work
    (review blocker 4).
    """
    import asyncio
    import concurrent.futures

    made = []

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            fut = concurrent.futures.Future()
            made.append(fut)
            return fut

        def shutdown(self, *args, **kwargs):
            pass

    check = DatabaseCheck(_OkProbeClient(), timeout_seconds=1.0, probe_executor=FakeExecutor())

    async def run_check():
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    async def scenario():
        # Seed an already-completed future (as if a previous probe finished).
        old = concurrent.futures.Future()
        old.set_result(_ProbeResult(data=[]))
        check._probe_future = old

        # Poll A submits a PENDING future and awaits it.
        task_a = asyncio.create_task(run_check())
        await asyncio.sleep(0)
        fut_a = check._probe_future
        assert fut_a is not old and not fut_a.done()

        # While A is still awaiting, poll B sees fut_a done? No: fut_a is
        # pending, so B must fail fast (single-probe invariant holds).
        assert await run_check() == "unavailable"
        assert check._probe_future is fut_a

        # Complete A's future; A's continuation is queued. Before A resumes,
        # simulate B observing fut_a done and installing a newer future. The
        # guard in A's finally must not clear this newer reference.
        fut_a.set_result(_ProbeResult(data=[]))
        fut_b = concurrent.futures.Future()
        fut_b.set_result(_ProbeResult(data=[]))
        check._probe_future = fut_b

        await task_a  # A's finally runs the guarded clear here
        assert check._probe_future is fut_b, "A must not clear B's newer future"
        assert made == [fut_a]

    asyncio.run(scenario())


# ─── 31d. aclose cleanup guarantees (review blockers 2 and 3) ───────────────


def test_aclose_cleans_up_when_probe_completes_exceptionally():
    """A probe that terminates with an exception while aclose is draining
    must not escape the drain and skip executor/client cleanup (review
    blocker 2)."""
    import concurrent.futures
    import time as _time

    class DelayedFailingClient(_OkProbeClient):
        def __init__(self):
            self.closed = False

        def execute(self):
            _time.sleep(0.05)
            raise RuntimeError("probe exploded")

        def close(self):
            self.closed = True

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    client = DelayedFailingClient()
    check = DatabaseCheck(
        client,
        timeout_seconds=0.01,
        probe_executor=executor,
        owns_client=True,
    )

    async def scenario():
        try:
            await check.run()  # times out at the readiness bound; probe pending
        except Exception:
            pass
        # aclose must not propagate the probe exception and must clean up.
        await check.aclose()
        assert client.closed, "owned client must be closed after an exceptional probe"
        assert check._probe_future is None

    asyncio.run(scenario())
    executor.shutdown(wait=False)


def test_aclose_guarantees_cleanup_on_drain_timeout():
    """When the drain bound expires, aclose still clears the future and
    closes the owned client so no owned resource survives the lifespan
    (review blocker 3)."""
    import concurrent.futures
    import threading
    import time as _time

    release = threading.Event()
    calls = []

    class StuckClient(_OkProbeClient):
        def __init__(self):
            self.closed = False

        def execute(self):
            calls.append(1)
            release.wait(timeout=5.0)
            return _ProbeResult(data=[])

        def close(self):
            self.closed = True

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    client = StuckClient()
    check = DatabaseCheck(
        client,
        timeout_seconds=0.01,
        probe_executor=executor,
        owns_client=True,
    )

    async def scenario():
        try:
            await check.run()  # times out; probe still blocked
        except Exception:
            pass
        await check.aclose()  # drain bound (0.01 + 1.0) expires; probe stuck
        assert client.closed, "owned client must be closed even on drain timeout"
        assert check._probe_future is None

    asyncio.run(scenario())
    assert calls == [1]
    # Release the stuck probe so its worker thread terminates on its own.
    release.set()
    deadline = _time.monotonic() + 5.0
    while any(t.is_alive() for t in executor._threads):
        assert _time.monotonic() < deadline, "probe thread alive after close"
        _time.sleep(0.01)
    executor.shutdown(wait=False)


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
        persistence_client=_OkProbeClient(),
        database_probe_client=object(),
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
        persistence_client=_OkProbeClient(),
        database_probe_client=object(),
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
    """The auth check verifies the callable route path ``auth.get_user``,
    not mere attribute existence (review blocker 1)."""
    from backend.health import AuthClientCheck

    async def _run(check):
        try:
            await check.run()
            return "ok"
        except Exception:
            return "unavailable"

    assert asyncio.run(_run(AuthClientCheck(None))) == "unavailable"
    assert asyncio.run(_run(AuthClientCheck(object()))) == "unavailable"
    # False positive fixed: an ``auth`` object without a callable get_user
    # (or with get_user=None) is unavailable.
    assert (
        asyncio.run(_run(AuthClientCheck(SimpleNamespace(auth=object()))))
        == "unavailable"
    )
    assert (
        asyncio.run(
            _run(AuthClientCheck(SimpleNamespace(auth=SimpleNamespace(get_user=None))))
        )
        == "unavailable"
    )
    # The exact path routes call exists and is callable.
    assert asyncio.run(_run(AuthClientCheck(_ok_auth_client()))) == "ok"


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
        persistence_client=persistence,
        database_probe_client=_OkProbeClient(),
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
        persistence_client=None,
        database_probe_client=_OkProbeClient(),
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
        persistence_client=_OkProbeClient(),
        database_probe_client=_OkProbeClient(),
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
