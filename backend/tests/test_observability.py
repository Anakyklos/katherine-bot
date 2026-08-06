"""Tests for the sanitized observability contract and turn events (#275)."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.admission import (
    ADMITTED,
    REQUEST_REPLAY_UNAVAILABLE,
    AdmissionResult,
    AdmissionRuntimeConfig,
)
from backend.atomic_turn_commit import ConflictError
from backend.emotion_presentation import EmotionStateResponse
from backend.health import HealthRegistry
from backend.process_turn import ProcessTurnResult, TurnMode
from backend.settings import AppEnvironment, Settings
from backend.turn_execution import (
    DeadlineExceeded,
    TurnErrorCode,
    TurnExecutionConfig,
    TurnExecutionError,
)

SECRET = "s" * 40
REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"
UUID = REQUEST_ID
CORRELATION_RE = re.compile(r"^[0-9a-f]{64}$")


def _settings() -> Settings:
    return Settings(
        app_env=AppEnvironment.local,
        groq_api_key="groq-key",
        admission_hmac_secret=SECRET,
        cors_allowed_origins=("https://allowed.example",),
    )


def _emotion() -> EmotionStateResponse:
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
        emotion_state=_emotion(),
    )


class FakeAuthClient:
    """Minimal auth surface used by the real get_current_user dependency."""

    def __init__(self):
        self.tokens = []

    @property
    def auth(self):
        return self

    def get_user(self, token):
        self.tokens.append(token)
        return SimpleNamespace(user=SimpleNamespace(id="user-a"))


class FakeEngine:
    def __init__(self, supabase=None):
        self.memory_manager = SimpleNamespace(supabase=supabase or object())
        self.calls = []
        self.error = None
        self.result = _turn_result()

    async def process_turn(
        self, user_id, message, request_id, *, budget=None, mode=None, correlation=None
    ):
        self.calls.append(
            {
                "user_id": user_id,
                "message": message,
                "request_id": request_id,
                "mode": mode,
                "correlation": correlation,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def endpoint(monkeypatch):
    fake_auth = FakeAuthClient()
    fake_engine = FakeEngine(supabase=fake_auth)
    captured = {}

    monkeypatch.setattr(
        main_module,
        "reserve_admission_sync",
        lambda client, request: captured.get(
            "admission_result", AdmissionResult(ADMITTED, 0)
        ),
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
        return func(*args, **kwargs)

    monkeypatch.setattr(main_module, "run_blocking_write", fake_run_blocking_write)

    deps = main_module.ApplicationDependencies(
        conversation_engine=fake_engine,
        auth_client=fake_auth,
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
        clock=__import__("time").time,
    )
    app = main_module.create_app(settings=_settings(), dependencies=deps)
    client = TestClient(app)
    try:
        yield client, fake_engine, captured, fake_auth
    finally:
        app.dependency_overrides.clear()


def _payload(request_id=REQUEST_ID, message="Hello"):
    return {"request_id": request_id, "message": message}


def _post(client, payload, **kwargs):
    return client.post(
        "/chat", json=payload, headers={"Authorization": "Bearer valid-token"}, **kwargs
    )


def _correlation_from(log_lines: str) -> str | None:
    match = re.search(r"correlation=([0-9a-f]{64})", log_lines)
    return match.group(1) if match else None


# ─── 39. Nominal turn events correlatable by request ID ─────────────────────


def test_nominal_turn_emits_correlatable_events(endpoint, caplog):
    client, fake_engine, captured, fake_auth = endpoint
    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload(message="Hello there"))

    assert response.status_code == 200
    assert response.json()["response"] == "response text"

    correlation = _correlation_from(caplog.text)
    assert correlation is not None
    assert "event=auth_completed outcome=ok" in caplog.text
    assert "event=admission_admitted" in caplog.text
    assert "event=turn_completed code=ok" in caplog.text
    assert "event=http_result code=200" in caplog.text
    # The turn_completed line carries the same sanitized correlation.
    turn_line = next(
        line
        for line in caplog.text.splitlines()
        if "event=turn_completed code=ok" in line
    )
    assert f"correlation={correlation}" in turn_line

    # The engine received the same sanitized correlation the logs carry.
    assert fake_engine.calls[0]["correlation"] == correlation


def test_same_request_id_produces_same_correlation(endpoint, caplog):
    client, _, _, _ = endpoint
    correlations = set()
    for _ in range(2):
        with caplog.at_level(logging.INFO, logger="backend.main"):
            response = _post(client, _payload())
            assert response.status_code == 200
        correlations.add(_correlation_from(caplog.text))
    assert len(correlations) == 1


# ─── 40. Replay and conflict are distinct events ────────────────────────────


def test_replay_attempt_is_a_distinct_event(endpoint, caplog):
    client, fake_engine, captured, fake_auth = endpoint
    captured["admission_result"] = AdmissionResult(REQUEST_REPLAY_UNAVAILABLE, 0)

    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload())

    assert response.status_code == 200
    assert "event=admission_replay" in caplog.text
    assert fake_engine.calls[0]["mode"] is TurnMode.replay_attempt


def test_revision_conflict_is_a_distinct_event(endpoint, caplog):
    client, fake_engine, captured, fake_auth = endpoint
    fake_engine.error = ConflictError("revision_mismatch", "conflict", expected_revision=1)

    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload())

    assert response.status_code == 409
    assert "event=request_conflict code=revision_mismatch" in caplog.text
    assert "event=admission_replay" not in caplog.text


# ─── 41. Failure codes remain distinct ──────────────────────────────────────


@pytest.mark.parametrize(
    "error,expected_code,expected_status",
    [
        (DeadlineExceeded(), "turn_timeout", 504),
        (
            TurnExecutionError(TurnErrorCode.upstream_rate_limited, "rate"),
            "upstream_rate_limited",
            429,
        ),
        (
            TurnExecutionError(TurnErrorCode.provider_unavailable, "down"),
            "provider_unavailable",
            503,
        ),
        (
            TurnExecutionError(TurnErrorCode.persistence_unavailable, "db"),
            "persistence_unavailable",
            503,
        ),
    ],
)
def test_failure_codes_remain_distinct(endpoint, caplog, error, expected_code, expected_status):
    client, fake_engine, _, _ = endpoint
    fake_engine.error = error
    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload())
    assert response.status_code == expected_status
    assert f"event=turn_failed code={expected_code}" in caplog.text
    assert f"event=http_result code={expected_status}" in caplog.text


# ─── 42/43/44. Logs never contain content or identifiers ────────────────────


def test_logs_never_contain_message_response_or_secrets(endpoint, caplog):
    client, _, _, _ = endpoint
    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload(message="TOP_SECRET_USER_MESSAGE"))
    assert response.status_code == 200
    assert "TOP_SECRET_USER_MESSAGE" not in caplog.text
    assert "response text" not in caplog.text
    assert SECRET not in caplog.text
    assert "groq-key" not in caplog.text


def test_logs_never_contain_raw_user_id(endpoint, caplog):
    client, fake_engine, _, _ = endpoint
    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload())
    assert response.status_code == 200
    assert "user-a" not in caplog.text
    assert fake_engine.calls[0]["user_id"] == "user-a"  # used internally, never logged


def test_sensitive_fake_exception_remains_sanitized(endpoint, caplog):
    client, fake_engine, _, _ = endpoint
    fake_engine.error = Exception("SENSITIVE_TURN_MARKER_XYZ")
    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload())
    assert response.status_code == 500
    assert "SENSITIVE_TURN_MARKER_XYZ" not in response.text
    assert "SENSITIVE_TURN_MARKER_XYZ" not in caplog.text
    assert "event=turn_failed code=internal_error" in caplog.text


# ─── 45. Events never alter the turn result ─────────────────────────────────


def test_events_do_not_change_turn_result(endpoint, caplog):
    client, fake_engine, _, _ = endpoint
    with caplog.at_level(logging.INFO, logger="backend.main"):
        response = _post(client, _payload(message="Hello"))
    assert response.status_code == 200
    assert len(fake_engine.calls) == 1
    call = fake_engine.calls[0]
    assert call["message"] == "Hello"
    assert call["mode"] is TurnMode.normal
    assert call["request_id"] == REQUEST_ID
    assert response.json() == {"response": "response text", "emotion_state": _emotion().model_dump()}
    # Observability must not introduce extra provider/persistence calls.
    assert "event=turn_completed code=ok" in caplog.text
