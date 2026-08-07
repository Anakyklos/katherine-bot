from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.admission import (
    ADMITTED,
    APPLICATION_RATE_LIMITED,
    INVALID_ADMISSION_INPUT,
    NETWORK_RATE_LIMITED,
    REQUEST_ID_CONFLICT,
    REQUEST_REPLAY_UNAVAILABLE,
    USER_DAILY_REQUEST_QUOTA_EXCEEDED,
    USER_DAILY_UNIT_QUOTA_EXCEEDED,
    USER_RATE_LIMITED,
    AdmissionResult,
    AdmissionRuntimeConfig,
    AdmissionUnavailable,
)
from backend.atomic_turn_commit import ConflictError
from backend.emotion_presentation import EmotionStateResponse
from backend.process_turn import ProcessTurnResult, TurnMode
from backend.turn_execution import TurnBudget
from backend.admission import compute_turn_correlation

UUID = "550e8400-e29b-41d4-a716-446655440000"
SECRET = "s" * 32


def emotion_response() -> EmotionStateResponse:
    return EmotionStateResponse(
        schema_version=1,
        mood_label="NEUTRA",
        pad={"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
        dominant_emotions=[],
        timestamp=1000.0,
    )


def _turn_result(mode: str = "normal") -> ProcessTurnResult:
    return ProcessTurnResult(
        committed=object(),
        response="response text",
        emotion_state=emotion_response(),
    )


class FakeEngine:
    def __init__(self):
        self.memory_manager = SimpleNamespace(supabase=object())
        self.calls = []
        self.error = None

    async def process_turn(
        self, user_id, message, request_id, *, budget=None, mode=None, correlation=None
    ):
        self.calls.append(
            {
                "user_id": user_id,
                "message": message,
                "request_id": request_id,
                "budget": budget,
                "mode": mode,
                "correlation": correlation,
            }
        )
        if self.error is not None:
            raise self.error
        return _turn_result()


def _build_app(fake_engine, *, override_auth=True):
    """Build an application with injected fakes (no lifespan needed)."""
    from backend.health import HealthRegistry
    from backend.settings import AppEnvironment, Settings
    from backend.turn_execution import TurnExecutionConfig

    settings = Settings(
        app_env=AppEnvironment.local,
        groq_api_key="k",
        admission_hmac_secret=SECRET,
        cors_allowed_origins=("http://localhost:3000",),
    )
    deps = main.ApplicationDependencies(
        conversation_engine=fake_engine,
        auth_client=fake_engine.memory_manager.supabase,
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
    )
    app = main.create_app(settings=settings, dependencies=deps)
    if override_auth:
        app.dependency_overrides[main.get_current_user] = lambda: SimpleNamespace(
            id="user-a"
        )
    return app


@pytest.fixture
def endpoint(monkeypatch):
    fake_engine = FakeEngine()
    captured = {}

    async def fake_run_blocking_write(
        stage_label,
        budget,
        supabase_timeout,
        func,
        *args,
        allowlist_exceptions=(),
        **kwargs,
    ):
        captured["stage_label"] = stage_label
        captured["budget"] = budget
        captured["supabase_timeout"] = supabase_timeout
        captured["allowlist_exceptions"] = allowlist_exceptions
        return func(*args, **kwargs)

    monkeypatch.setattr(main, "run_blocking_write", fake_run_blocking_write)
    app = _build_app(fake_engine)
    client = TestClient(app)
    try:
        yield client, fake_engine, captured
    finally:
        app.dependency_overrides.clear()


def test_admitted_runs_process_turn_normal_with_request_id_and_one_budget(endpoint, monkeypatch):
    client, fake_engine, captured = endpoint
    rpc_calls = []

    def admitted(_client, request):
        rpc_calls.append(request)
        return AdmissionResult(ADMITTED, 0)

    monkeypatch.setattr(main, "reserve_admission_sync", admitted)

    response = client.post(
        "/chat",
        json={"request_id": UUID.upper(), "message": "Olá"},
    )

    assert response.status_code == 200
    assert set(response.json()) == {"response", "emotion_state"}
    assert response.json()["response"] == "response text"
    assert len(rpc_calls) == 1
    request = rpc_calls[0]
    assert request.user_id == "user-a"
    assert request.request_id == UUID
    assert request.estimated_units == len("Olá".encode("utf-8"))
    assert len(fake_engine.calls) == 1
    call = fake_engine.calls[0]
    # request_id is forwarded to the ProcessTurn-backed engine and the SAME
    # budget flows from admission through the engine call.
    assert call["request_id"] == UUID
    assert call["mode"] is TurnMode.normal
    assert call["budget"] is captured["budget"]
    assert captured["stage_label"] == "reserve_admission"
    assert captured["allowlist_exceptions"] == (AdmissionUnavailable,)


def test_replay_unavailable_runs_process_turn_replay_mode(endpoint, monkeypatch):
    client, fake_engine, _captured = endpoint
    monkeypatch.setattr(
        main,
        "reserve_admission_sync",
        lambda _client, _request: AdmissionResult(REQUEST_REPLAY_UNAVAILABLE, 0),
    )

    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": "hello"},
    )

    assert response.status_code == 200
    assert len(fake_engine.calls) == 1
    call = fake_engine.calls[0]
    assert call["request_id"] == UUID
    assert call["mode"] is TurnMode.replay_attempt


def test_replay_unavailable_without_confirmed_result_returns_409(endpoint, monkeypatch):
    client, fake_engine, _captured = endpoint
    monkeypatch.setattr(
        main,
        "reserve_admission_sync",
        lambda _client, _request: AdmissionResult(REQUEST_REPLAY_UNAVAILABLE, 0),
    )
    fake_engine.error = ConflictError(
        code="request_replay_unavailable",
        message="Request replay is unavailable.",
        expected_revision=0,
    )

    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": "hello"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "request_replay_unavailable"
    assert UUID not in response.text
    assert fake_engine.calls[0]["mode"] is TurnMode.replay_attempt


@pytest.mark.parametrize(
    ("decision", "retry", "status", "header"),
    [
        (REQUEST_ID_CONFLICT, 0, 409, None),
        (USER_RATE_LIMITED, 60, 429, "60"),
        (NETWORK_RATE_LIMITED, 60, 429, "60"),
        (APPLICATION_RATE_LIMITED, 60, 429, "60"),
        (USER_DAILY_REQUEST_QUOTA_EXCEEDED, 86400, 429, "86400"),
        (USER_DAILY_UNIT_QUOTA_EXCEEDED, 86400, 429, "86400"),
        (INVALID_ADMISSION_INPUT, 0, 422, None),
    ],
)
def test_rejections_map_exactly_without_calling_engine(
    endpoint,
    monkeypatch,
    decision,
    retry,
    status,
    header,
):
    client, fake_engine, _captured = endpoint
    monkeypatch.setattr(
        main,
        "reserve_admission_sync",
        lambda _client, _request: AdmissionResult(decision, retry),
    )

    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": "hello"},
    )

    assert response.status_code == status
    assert response.json()["detail"]["code"] == decision
    assert response.headers.get("Retry-After") == header
    assert fake_engine.calls == []


@pytest.mark.parametrize(
    ("code", "status", "public_code"),
    [
        ("revision_mismatch", 409, "revision_conflict"),
        ("request_payload_conflict", 409, "request_id_conflict"),
        ("request_in_progress", 409, "request_in_progress"),
        ("lease_conflict", 409, "lease_conflict"),
        ("request_replay_unavailable", 409, "request_replay_unavailable"),
    ],
)
def test_process_turn_conflicts_map_without_leaking_request_id(
    endpoint, monkeypatch, code, status, public_code
):
    client, fake_engine, _captured = endpoint
    monkeypatch.setattr(
        main,
        "reserve_admission_sync",
        lambda _client, _request: AdmissionResult(ADMITTED, 0),
    )
    fake_engine.error = ConflictError(
        code=code,
        message="internal details that must never be exposed",
        expected_revision=7,
        actual_revision=8,
        request_id=UUID,
    )

    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": "hello"},
    )

    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == public_code
    # ConflictError text, revisions and request_id never reach the client.
    assert "internal details" not in response.text
    assert UUID not in response.text
    assert "expected_revision" not in response.text
    assert "actual_revision" not in response.text


def test_persistence_error_maps_to_503(endpoint, monkeypatch):
    from backend.atomic_turn_commit import PersistenceError

    client, fake_engine, _captured = endpoint
    monkeypatch.setattr(
        main,
        "reserve_admission_sync",
        lambda _client, _request: AdmissionResult(ADMITTED, 0),
    )
    fake_engine.error = PersistenceError("database_error", "persistence error")

    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": "hello"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "persistence_unavailable"


def test_store_unavailable_fails_closed_without_engine(endpoint, monkeypatch):
    client, fake_engine, _captured = endpoint
    marker = "sensitive-upstream-marker"

    def unavailable(_client, _request):
        raise AdmissionUnavailable() from RuntimeError(marker)

    monkeypatch.setattr(main, "reserve_admission_sync", unavailable)
    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": marker},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["code"] == "admission_unavailable"
    assert marker not in response.text
    assert fake_engine.calls == []


def test_invalid_uuid_is_sanitised_and_never_reserves(endpoint, monkeypatch):
    client, fake_engine, _captured = endpoint
    marker = "not-a-uuid-sensitive-marker"
    called = False

    def reserve(_client, _request):
        nonlocal called
        called = True
        return AdmissionResult(ADMITTED, 0)

    monkeypatch.setattr(main, "reserve_admission_sync", reserve)
    response = client.post(
        "/chat",
        json={"request_id": marker, "message": "hello"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request_id"
    assert marker not in response.text
    assert called is False
    assert fake_engine.calls == []


def test_character_limit_error_does_not_echo_message(endpoint):
    client, fake_engine, _captured = endpoint
    message = "x" * 4001
    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": message},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "message_too_long"
    assert message not in response.text
    assert fake_engine.calls == []


def test_utf8_budget_error_does_not_echo_message(endpoint):
    client, fake_engine, _captured = endpoint
    message = "🌙" * 1501
    assert len(message) <= 4000
    assert len(message.encode("utf-8")) > 6000
    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": message},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "message_budget_exceeded"
    assert message not in response.text
    assert fake_engine.calls == []


def test_extra_field_and_wrong_types_use_generic_sanitised_validation(endpoint):
    client, fake_engine, _captured = endpoint
    marker = "raw-body-marker"
    response = client.post(
        "/chat",
        json={
            "request_id": UUID,
            "message": 123,
            "extra": marker,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
    assert marker not in response.text
    assert "123" not in response.text
    assert fake_engine.calls == []


def test_missing_authentication_does_not_reserve(monkeypatch):
    fake_engine = FakeEngine()
    app = _build_app(fake_engine, override_auth=False)
    called = False

    def reserve(_client, _request):
        nonlocal called
        called = True
        return AdmissionResult(ADMITTED, 0)

    monkeypatch.setattr(main, "reserve_admission_sync", reserve)
    client = TestClient(app)
    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": "hello"},
    )
    assert response.status_code == 401
    assert called is False
    assert fake_engine.calls == []


def test_expired_budget_returns_timeout_before_rpc(endpoint, monkeypatch):
    client, fake_engine, _captured = endpoint
    called = False

    def reserve(_client, _request):
        nonlocal called
        called = True
        return AdmissionResult(ADMITTED, 0)

    monkeypatch.setattr(main, "reserve_admission_sync", reserve)
    monkeypatch.setattr(
        main,
        "create_budget",
        lambda _config: TurnBudget(
            deadline=0.0,
            reserve=0.0,
            now_provider=lambda: 1.0,
        ),
    )
    from backend.turn_execution import run_blocking_write

    monkeypatch.setattr(main, "run_blocking_write", run_blocking_write)
    response = client.post(
        "/chat",
        json={"request_id": UUID, "message": "hello"},
    )
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "turn_timeout"
    assert called is False
    assert fake_engine.calls == []


def test_cancellation_is_propagated_not_converted_to_500(monkeypatch):
    """The endpoint must re-raise CancelledError, never map it to HTTP 500."""
    fake_engine = FakeEngine()
    app = _build_app(fake_engine, override_auth=False)
    monkeypatch.setattr(
        main,
        "reserve_admission_sync",
        lambda _client, _request: AdmissionResult(ADMITTED, 0),
    )

    async def fake_run_blocking_write(*_args, **_kwargs):
        return AdmissionResult(ADMITTED, 0)

    monkeypatch.setattr(main, "run_blocking_write", fake_run_blocking_write)

    async def boom(*_args, **_kwargs):
        raise asyncio.CancelledError()

    fake_engine.process_turn = boom

    input_data = main.ChatInput(request_id=UUID, message="hello")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "app": app,
    }
    request = main.Request(scope)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await main.chat_endpoint(
                input_data=input_data,
                request=request,
                current_user=SimpleNamespace(id="user-a"),
            )

    asyncio.run(run())


class TestCorrelationFlow:
    def _expected(self) -> str:
        return compute_turn_correlation(
            AdmissionRuntimeConfig.from_values(SECRET), UUID
        )

    def test_same_request_same_correlation_between_endpoint_and_engine(
        self, endpoint, monkeypatch
    ):
        client, fake_engine, _captured = endpoint
        monkeypatch.setattr(
            main,
            "reserve_admission_sync",
            lambda _client, _request: AdmissionResult(ADMITTED, 0),
        )

        response = client.post(
            "/chat",
            json={"request_id": UUID, "message": "hello"},
        )

        assert response.status_code == 200
        expected = self._expected()
        assert len(expected) == 64
        assert fake_engine.calls[0]["correlation"] == expected
        # the raw request id is never forwarded as the correlation
        assert fake_engine.calls[0]["correlation"] != UUID

    def test_different_requests_produce_different_correlations(self, endpoint, monkeypatch):
        client, fake_engine, _captured = endpoint
        monkeypatch.setattr(
            main,
            "reserve_admission_sync",
            lambda _client, _request: AdmissionResult(ADMITTED, 0),
        )

        client.post("/chat", json={"request_id": UUID, "message": "hello"})
        client.post(
            "/chat",
            json={"request_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "message": "hi"},
        )

        correlations = {call["correlation"] for call in fake_engine.calls}
        assert len(correlations) == 2

    def test_replay_attempt_receives_same_correlation(self, endpoint, monkeypatch):
        client, fake_engine, _captured = endpoint
        monkeypatch.setattr(
            main,
            "reserve_admission_sync",
            lambda _client, _request: AdmissionResult(REQUEST_REPLAY_UNAVAILABLE, 0),
        )

        response = client.post(
            "/chat",
            json={"request_id": UUID, "message": "hello"},
        )

        assert response.status_code == 200
        assert fake_engine.calls[0]["mode"] is TurnMode.replay_attempt
        assert fake_engine.calls[0]["correlation"] == self._expected()

    def test_admission_events_are_distinct_and_correlated(self, endpoint, monkeypatch, caplog):
        import logging

        client, fake_engine, _captured = endpoint
        monkeypatch.setattr(
            main,
            "reserve_admission_sync",
            lambda _client, _request: AdmissionResult(REQUEST_REPLAY_UNAVAILABLE, 0),
        )

        with caplog.at_level(logging.INFO, logger="backend.main"):
            client.post(
                "/chat",
                json={"request_id": UUID, "message": "hello"},
            )

        expected = self._expected()
        messages = [r.getMessage() for r in caplog.records]
        assert f"event=admission_replay correlation={expected}" in messages
        # a replay is NEVER logged as a normal admission
        assert not any("event=admission_admitted" in m for m in messages)

    def test_admitted_event_logged_only_for_fresh_admission(
        self, endpoint, monkeypatch, caplog
    ):
        import logging

        client, fake_engine, _captured = endpoint
        monkeypatch.setattr(
            main,
            "reserve_admission_sync",
            lambda _client, _request: AdmissionResult(ADMITTED, 0),
        )

        with caplog.at_level(logging.INFO, logger="backend.main"):
            client.post(
                "/chat",
                json={"request_id": UUID, "message": "hello"},
            )

        expected = self._expected()
        messages = [r.getMessage() for r in caplog.records]
        assert f"event=admission_admitted correlation={expected}" in messages
        assert not any("event=admission_replay" in m for m in messages)

    def test_no_raw_uuid_and_no_secret_in_http_or_logs(
        self, endpoint, monkeypatch, caplog
    ):
        import logging

        client, fake_engine, _captured = endpoint
        monkeypatch.setattr(
            main,
            "reserve_admission_sync",
            lambda _client, _request: AdmissionResult(ADMITTED, 0),
        )

        with caplog.at_level(logging.INFO, logger="backend.main"):
            response = client.post(
                "/chat",
                json={"request_id": UUID, "message": "hello"},
            )

        assert response.status_code == 200
        assert UUID not in response.text
        assert SECRET not in response.text
        assert UUID not in caplog.text
        assert SECRET not in caplog.text
