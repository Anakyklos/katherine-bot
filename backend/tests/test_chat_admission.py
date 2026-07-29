from __future__ import annotations

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
from backend.emotion_presentation import EmotionStateResponse
from backend.turn_execution import TurnBudget

UUID = "550e8400-e29b-41d4-a716-446655440000"


def emotion_response() -> EmotionStateResponse:
    return EmotionStateResponse(
        schema_version=1,
        mood_label="NEUTRA",
        pad={"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
        dominant_emotions=[],
        timestamp=1000.0,
    )


class FakeEngine:
    def __init__(self):
        self.memory_manager = SimpleNamespace(supabase=object())
        self.calls = []

    async def process_turn(
        self,
        user_id,
        message,
        background_tasks=None,
        *,
        budget=None,
    ):
        self.calls.append(
            {
                "user_id": user_id,
                "message": message,
                "background_tasks": background_tasks,
                "budget": budget,
            }
        )
        return "response", emotion_response()


@pytest.fixture
def endpoint(monkeypatch):
    fake_engine = FakeEngine()
    captured = {}

    monkeypatch.setattr(main, "engine", fake_engine)
    monkeypatch.setattr(
        main,
        "_admission_config",
        AdmissionRuntimeConfig.from_values("s" * 32),
    )

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
    main.app.dependency_overrides[main.get_current_user] = lambda: SimpleNamespace(
        id="user-a"
    )
    client = TestClient(main.app)
    try:
        yield client, fake_engine, captured
    finally:
        main.app.dependency_overrides.clear()


def test_admitted_normalises_uuid_and_shares_one_budget(endpoint, monkeypatch):
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
    assert len(rpc_calls) == 1
    request = rpc_calls[0]
    assert request.user_id == "user-a"
    assert request.request_id == UUID
    assert request.estimated_units == len("Olá".encode("utf-8"))
    assert len(fake_engine.calls) == 1
    assert fake_engine.calls[0]["budget"] is captured["budget"]
    assert captured["stage_label"] == "reserve_admission"
    assert captured["allowlist_exceptions"] == (AdmissionUnavailable,)


@pytest.mark.parametrize(
    ("decision", "retry", "status", "header"),
    [
        (REQUEST_REPLAY_UNAVAILABLE, 0, 409, None),
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
    monkeypatch.setattr(main, "engine", fake_engine)
    main.app.dependency_overrides.clear()
    called = False

    def reserve(_client, _request):
        nonlocal called
        called = True
        return AdmissionResult(ADMITTED, 0)

    monkeypatch.setattr(main, "reserve_admission_sync", reserve)
    client = TestClient(main.app)
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
