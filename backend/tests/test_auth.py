import os
import sys
import logging
import pytest
from unittest.mock import patch, MagicMock, ANY

@pytest.fixture(autouse=True, scope="module")
def mock_external_dependencies():
    _original_modules = dict(sys.modules)
    _original_env = dict(os.environ)

    mock_env = {
        'GROQ_API_KEY': 'mock_key',
        'SUPABASE_URL': 'http://mock',
        'SUPABASE_SERVICE_ROLE_KEY': 'mock_key'
    }
    os.environ.update(mock_env)

    # Mock modules before importing
    sys.modules['sentence_transformers'] = MagicMock()
    sys.modules['supabase'] = MagicMock()

    yield

    # Restore modules directionally:
    # - If the module existed before the fixture, restore the ORIGINAL object
    # - If the module was added by the fixture, remove it
    def _restore_module(name):
        if name in _original_modules:
            sys.modules[name] = _original_modules[name]
        elif name in sys.modules:
            del sys.modules[name]

    _restore_module('backend.main')
    _restore_module('sentence_transformers')
    _restore_module('supabase')

    # Restore backend modules that were added during this test module
    # (not present in the pre-fixture snapshot)
    for k in list(sys.modules.keys()):
        if k.startswith('backend.') and k not in _original_modules:
            del sys.modules[k]

    os.environ.clear()
    os.environ.update(_original_env)


@pytest.fixture
def client_app(mock_external_dependencies):
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app)

@pytest.fixture
def mock_supabase():
    from backend.main import engine
    with patch.object(engine.memory_manager, 'supabase', MagicMock()) as mock_sb:
        yield mock_sb

@pytest.fixture
def mock_engine_process():
    from backend.main import engine
    from backend.emotion_presentation import EmotionStateResponse, PublicPAD
    fake_emotion = EmotionStateResponse(
        schema_version=1,
        mood_label="NEUTRA",
        pad=PublicPAD(pleasure=0.0, arousal=0.0, dominance=0.0),
        dominant_emotions=[],
        timestamp=1_700_000_000.0,
    )
    with patch.object(engine, 'process_turn', return_value=("Mock response", fake_emotion)) as mock_process:
        yield mock_process

class MockUser:
    def __init__(self, id):
        self.id = id

class MockAuthResponse:
    def __init__(self, user):
        self.user = user

from supabase_auth.errors import AuthApiError, AuthRetryableError

def test_missing_token(client_app, mock_supabase, mock_engine_process):
    response = client_app.post("/chat", json={"message": "Hello"})
    assert response.status_code == 401
    assert "Not authenticated" in response.json()["detail"]
    assert response.headers.get("WWW-Authenticate") == "Bearer"

    response = client_app.get("/history")
    assert response.status_code == 401
    assert "Not authenticated" in response.json()["detail"]
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_invalid_scheme(client_app, mock_supabase, mock_engine_process):
    response = client_app.post(
        "/chat",
        json={"message": "Hello"},
        headers={"Authorization": "Basic x"}
    )
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_invalid_token(client_app, mock_supabase, mock_engine_process):
    mock_supabase.auth.get_user.side_effect = AuthApiError("Internal Mock JWT SDK Error", 400, "")

    response = client_app.post(
        "/chat",
        json={"message": "Hello"},
        headers={"Authorization": "Bearer invalid_token"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication failed"
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    # Ensure raw message is not leaked
    assert "Internal Mock JWT SDK Error" not in response.text
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_user_is_none(client_app, mock_supabase, mock_engine_process):
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=None)
    response = client_app.get("/history", headers={"Authorization": "Bearer token"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication failed"
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_service_unavailable(client_app, mock_supabase, mock_engine_process):
    from backend.main import engine
    with patch.object(engine.memory_manager, 'supabase', None):
        response = client_app.post("/chat", json={"message": "Hi"}, headers={"Authorization": "Bearer t"})
        assert response.status_code == 503
        assert response.json()["detail"] == "Authentication service unavailable"
        mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_valid_token(client_app, mock_supabase, mock_engine_process):
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.post(
        "/chat",
        json={"message": "Hello"},
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 200
    assert response.json()["response"] == "Mock response"
    mock_engine_process.assert_called_once_with("user123", "Hello", ANY)

def test_spoofing_user_id_in_chat(client_app, mock_supabase, mock_engine_process):
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.post(
        "/chat",
        json={"user_id": "other_user", "message": "Hello"},
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 422
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_history_valid_token(client_app, mock_supabase):
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    mock_table = MagicMock()
    mock_supabase.table.return_value = mock_table
    mock_select = MagicMock()
    mock_table.select.return_value = mock_select
    mock_eq = MagicMock()
    mock_select.eq.return_value = mock_eq
    mock_order = MagicMock()
    mock_eq.order.return_value = mock_order
    mock_limit = MagicMock()
    mock_order.limit.return_value = mock_limit

    class MockData:
        def __init__(self, data):
            self.data = data

    mock_limit.execute.return_value = MockData(data=[{"content": "msg1"}])

    response = client_app.get(
        "/history",
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["content"] == "msg1"

    # Verify that it strictly uses current_user.id
    mock_select.eq.assert_called_once_with("user_id", "user123")

def test_history_legacy_route_removed(client_app, mock_supabase, mock_engine_process):
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.get(
        "/history/outro-usuario",
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 404

def test_credential_rejection_401(client_app, mock_supabase, mock_engine_process, caplog):
    error = AuthApiError("SENSITIVE_AUTH_MARKER", 400, "error_code")
    mock_supabase.auth.get_user.side_effect = error

    with caplog.at_level(logging.ERROR):
        response = client_app.post(
            "/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer invalid_token"}
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication failed"
    assert "SENSITIVE_AUTH_MARKER" not in caplog.text
    assert "SENSITIVE_AUTH_MARKER" not in response.text
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_transport_timeout_503(client_app, mock_supabase, mock_engine_process, caplog):
    error = AuthRetryableError("SENSITIVE_AUTH_MARKER_TIMEOUT", 503)
    mock_supabase.auth.get_user.side_effect = error

    with caplog.at_level(logging.ERROR):
        response = client_app.post(
            "/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Authentication service unavailable"
    assert "SENSITIVE_AUTH_MARKER" not in caplog.text
    assert "SENSITIVE_AUTH_MARKER" not in response.text
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_service_error_5xx(client_app, mock_supabase, mock_engine_process, caplog):
    error = AuthApiError("SENSITIVE_AUTH_MARKER_500", 500, "error_code")
    mock_supabase.auth.get_user.side_effect = error

    with caplog.at_level(logging.ERROR):
        response = client_app.post(
            "/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Authentication service unavailable"
    assert "SENSITIVE_AUTH_MARKER" not in caplog.text
    assert "SENSITIVE_AUTH_MARKER" not in response.text
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()

def test_unexpected_error_503(client_app, mock_supabase, mock_engine_process, caplog):
    error = Exception("SENSITIVE_AUTH_MARKER_UNKNOWN")
    mock_supabase.auth.get_user.side_effect = error

    with caplog.at_level(logging.ERROR):
        response = client_app.post(
            "/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Authentication service unavailable"
    assert "SENSITIVE_AUTH_MARKER" not in caplog.text
    assert "SENSITIVE_AUTH_MARKER" not in response.text
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()


def test_http_chat_load_failure_sanitization(client_app, mock_supabase, caplog):
    from backend.main import engine
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=MockUser("user123"))

    # Mock supabase select call to raise a sensitive exception
    mock_supabase.table.return_value.select.return_value.eq.return_value.execute.side_effect = Exception("SENSITIVE_DB_LOAD_ERROR")

    # Mock async LLM calls (appraisal and generation)
    async def _mock_async(**kwargs):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = '{"valence": 0, "arousal_shift": 0, "dominance_shift": 0}'
        return mock_resp
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_async)

    with caplog.at_level(logging.ERROR):
        response = client_app.post(
            "/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "persistence_unavailable"
    assert "SENSITIVE_DB_LOAD_ERROR" not in response.text
    assert "SENSITIVE_DB_LOAD_ERROR" not in caplog.text
    assert "user123" not in response.text
    assert "user123" not in caplog.text


def test_http_chat_persistence_failure_sanitization(client_app, mock_supabase, caplog):
    from backend.main import engine
    from backend.emotional_core import EmotionalState
    from backend.relationship import RelationshipStateV1
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=MockUser("user123"))

    # Mock load_user_state to succeed
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": EmotionalState().to_dict(),
        "relationship_state": RelationshipStateV1.neutral(timestamp=1700000000.0).to_dict()
    })

    # Mock sync_state (update) to raise a sensitive exception
    mock_supabase.table.return_value.update.return_value.eq.return_value.execute.side_effect = Exception("SENSITIVE_DB_SYNC_ERROR")

    # Mock async LLM calls
    async def _mock_async_persist(**kwargs):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = '{"valence": 0, "arousal_shift": 0, "dominance_shift": 0}'
        return mock_resp
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=_mock_async_persist)

    with caplog.at_level(logging.ERROR):
        response = client_app.post(
            "/chat",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "persistence_unavailable"
    assert "SENSITIVE_DB_SYNC_ERROR" not in response.text
    assert "SENSITIVE_DB_SYNC_ERROR" not in caplog.text
    assert "user123" not in response.text
    assert "user123" not in caplog.text

def test_chat_message_exactly_at_limit(client_app, mock_supabase, mock_engine_process):
    from backend.memory import MAX_MESSAGE_LENGTH
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.post(
        "/chat",
        json={"message": "a" * MAX_MESSAGE_LENGTH},
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 200
    assert response.json()["response"] == "Mock response"
    mock_engine_process.assert_called_once_with("user123", "a" * MAX_MESSAGE_LENGTH, ANY)

def test_chat_message_exceeds_limit(client_app, mock_supabase, mock_engine_process):
    from backend.memory import MAX_MESSAGE_LENGTH
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.post(
        "/chat",
        json={"message": "a" * (MAX_MESSAGE_LENGTH + 1)},
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 422
    mock_engine_process.assert_not_called()


def test_fixture_teardown_preserves_existing_modules():
    """
    Verifica que o teardown do fixture restaura os objetos originais
    em vez de apenas deletá-los, preservando a identidade (``is``)
    de módulos que já existiam antes do fixture.

    Este teste executa diretamente a lógica de setup/teardown do
    fixture ``mock_external_dependencies`` sem depender do decorador
    autouse scope=module.
    """
    sentinel_main = object()
    sentinel_sb = object()
    sentinel_st = object()

    # Guard: save actual state to restore later
    saved = {}
    for name in ("backend.main", "supabase", "sentence_transformers"):
        saved[name] = sys.modules.get(name)

    try:
        # 1. Pre-load sentinel objects (simulating modules that existed
        #    before the fixture ran, e.g. from other test files)
        sys.modules["backend.main"] = sentinel_main
        sys.modules["supabase"] = sentinel_sb
        sys.modules["sentence_transformers"] = sentinel_st

        # 2. Simulate fixture setup: snapshot + replace with mocks
        _original_modules = dict(sys.modules)
        sys.modules["sentence_transformers"] = MagicMock()
        sys.modules["supabase"] = MagicMock()

        # 3. Simulate fixture teardown with directional restore
        def _restore_module(name):
            if name in _original_modules:
                sys.modules[name] = _original_modules[name]
            elif name in sys.modules:
                del sys.modules[name]

        _restore_module("backend.main")
        _restore_module("sentence_transformers")
        _restore_module("supabase")

        # 4. Assert identity is preserved (original objects restored)
        assert sys.modules.get("backend.main") is sentinel_main, \
            "backend.main should be restored to original sentinel"
        assert sys.modules.get("supabase") is sentinel_sb, \
            "supabase should be restored to original sentinel"
        assert sys.modules.get("sentence_transformers") is sentinel_st, \
            "sentence_transformers should be restored to original sentinel"
    finally:
        # Restore actual modules
        for name in ("backend.main", "supabase", "sentence_transformers"):
            if saved[name] is not None:
                sys.modules[name] = saved[name]
            else:
                sys.modules.pop(name, None)
