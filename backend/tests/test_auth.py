import os
import sys
import logging
import pytest
from unittest.mock import patch, MagicMock, ANY

import backend.main as main

REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"


def _chat_payload(message, **extra):
    return {"request_id": REQUEST_ID, "message": message, **extra}


@pytest.fixture(autouse=True, scope="module")
def mock_external_dependencies():
    _original_modules = dict(sys.modules)
    _original_env = dict(os.environ)

    mock_env = {
        'GROQ_API_KEY': 'mock_key',
        'SUPABASE_URL': 'http://mock',
        'SUPABASE_SERVICE_ROLE_KEY': 'mock_key',
        'ADMISSION_HMAC_SECRET': 'test-admission-secret-that-is-at-least-32-bytes',
        'TRUSTED_PROXY_CIDRS': '',
    }
    os.environ.update(mock_env)

    # Mock modules before importing — save originals for exact restore
    _mocked_keys: set[str] = set()
    for _key in ('sentence_transformers', 'supabase'):
        _original_modules.setdefault(_key, sys.modules.get(_key))
        sys.modules[_key] = MagicMock()
        _mocked_keys.add(_key)

    yield

    # Restore only the modules that this fixture explicitly touched.
    # NEVER iterate over all backend.* modules — that destroys class
    # identity for modules imported by other test files during the
    # same session (e.g. backend.trusted_context, backend.memory).
    def _restore_module(name):
        if name in _original_modules and _original_modules[name] is not None:
            sys.modules[name] = _original_modules[name]
        elif name in sys.modules:
            del sys.modules[name]

    for _key in _mocked_keys:
        _restore_module(_key)

    # Explicitly restore only backend.main if it existed before
    if 'backend.main' in _original_modules:
        sys.modules['backend.main'] = _original_modules['backend.main']
    elif 'backend.main' in sys.modules:
        del sys.modules['backend.main']

    os.environ.clear()
    os.environ.update(_original_env)


@pytest.fixture(scope="module")
def client_app(mock_external_dependencies):
    """Real engine composed through the application factory with mocked env.

    The engine is built once per module (like the historical module-level
    engine) and composed via ``create_app`` with the validated settings, so
    the auth dependency runs through the real server-side path.
    """
    from fastapi.testclient import TestClient
    from backend.admission import AdmissionRuntimeConfig
    from backend.chat_engine import ChatConversationEngine
    from backend.dependencies import ApplicationDependencies
    from backend.health import HealthRegistry
    from backend.settings import AppEnvironment, Settings
    from backend.turn_execution import TurnExecutionConfig

    engine = ChatConversationEngine()
    settings = Settings(
        app_env=AppEnvironment.local,
        groq_api_key="mock_key",
        admission_hmac_secret="test-admission-secret-that-is-at-least-32-bytes",
        cors_allowed_origins=("http://localhost:3000",),
    )
    deps = ApplicationDependencies(
        conversation_engine=engine,
        auth_client=engine.memory_manager.supabase,
        admission_config=AdmissionRuntimeConfig.from_values(
            "test-admission-secret-that-is-at-least-32-bytes"
        ),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
    )
    app = main.create_app(settings=settings, dependencies=deps)
    return TestClient(app)


def _app_engine(client_app):
    """Resolve the composed engine from the test client's app state."""
    return client_app.app.state.dependencies.conversation_engine


@pytest.fixture
def mock_supabase(client_app):
    engine = _app_engine(client_app)
    with patch.object(engine.memory_manager, 'supabase', MagicMock()) as mock_sb:
        mock_sb.rpc.return_value.execute.return_value.data = [
            {"decision": "admitted", "retry_after_seconds": 0}
        ]
        yield mock_sb


@pytest.fixture
def mock_engine_process(client_app):
    engine = _app_engine(client_app)
    from backend.emotion_presentation import EmotionStateResponse, PublicPAD
    from backend.process_turn import ProcessTurnResult
    fake_emotion = EmotionStateResponse(
        schema_version=1,
        mood_label="NEUTRA",
        pad=PublicPAD(pleasure=0.0, arousal=0.0, dominance=0.0),
        dominant_emotions=[],
        timestamp=1_700_000_000.0,
    )
    fake_result = ProcessTurnResult(
        committed=object(),
        response="Mock response",
        emotion_state=fake_emotion,
    )
    with patch.object(engine, 'process_turn', return_value=fake_result) as mock_process:
        yield mock_process


class MockUser:
    def __init__(self, id):
        self.id = id


class MockAuthResponse:
    def __init__(self, user):
        self.user = user


from supabase_auth.errors import AuthApiError, AuthRetryableError


def test_missing_token(client_app, mock_supabase, mock_engine_process):
    response = client_app.post("/chat", json=_chat_payload("Hello"))
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
        json=_chat_payload("Hello"),
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
        json=_chat_payload("Hello"),
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
    engine = _app_engine(client_app)
    with patch.object(engine.memory_manager, 'supabase', None):
        response = client_app.post(
            "/chat",
            json=_chat_payload("Hi"),
            headers={"Authorization": "Bearer t"},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "Authentication service unavailable"
        mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()


def test_valid_token(client_app, mock_supabase, mock_engine_process):
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.post(
        "/chat",
        json=_chat_payload("Hello"),
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 200
    assert response.json()["response"] == "Mock response"
    mock_engine_process.assert_called_once_with(
        "user123", "Hello", REQUEST_ID, budget=ANY, mode=ANY, correlation=ANY
    )


def test_spoofing_user_id_in_chat(client_app, mock_supabase, mock_engine_process):
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.post(
        "/chat",
        json=_chat_payload("Hello", user_id="other_user"),
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
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
            json=_chat_payload("Hello"),
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
            json=_chat_payload("Hello"),
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
            json=_chat_payload("Hello"),
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
            json=_chat_payload("Hello"),
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Authentication service unavailable"
    assert "SENSITIVE_AUTH_MARKER" not in caplog.text
    assert "SENSITIVE_AUTH_MARKER" not in response.text
    mock_engine_process.assert_not_called()
    mock_supabase.table.assert_not_called()


def test_http_chat_load_failure_sanitization(client_app, mock_supabase, caplog):
    engine = _app_engine(client_app)
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
            json=_chat_payload("Hello"),
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "persistence_unavailable"
    assert "SENSITIVE_DB_LOAD_ERROR" not in response.text
    assert "SENSITIVE_DB_LOAD_ERROR" not in caplog.text
    assert "user123" not in response.text
    assert "user123" not in caplog.text


def test_http_chat_persistence_failure_sanitization(client_app, mock_supabase, caplog):
    engine = _app_engine(client_app)
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
            json=_chat_payload("Hello"),
            headers={"Authorization": "Bearer some_token"}
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "persistence_unavailable"
    assert "SENSITIVE_DB_SYNC_ERROR" not in response.text
    assert "SENSITIVE_DB_SYNC_ERROR" not in caplog.text
    assert "user123" not in response.text
    assert "user123" not in caplog.text


def test_chat_message_exactly_at_limit(client_app, mock_supabase, mock_engine_process):
    from backend.admission_contracts import NEW_MESSAGE_MAX_CHARS
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)
    message = "a" * NEW_MESSAGE_MAX_CHARS

    response = client_app.post(
        "/chat",
        json=_chat_payload(message),
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 200
    assert response.json()["response"] == "Mock response"
    mock_engine_process.assert_called_once_with(
        "user123", message, REQUEST_ID, budget=ANY, mode=ANY, correlation=ANY
    )


def test_chat_message_exceeds_limit(client_app, mock_supabase, mock_engine_process):
    from backend.admission_contracts import NEW_MESSAGE_MAX_CHARS
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)

    response = client_app.post(
        "/chat",
        json=_chat_payload("a" * (NEW_MESSAGE_MAX_CHARS + 1)),
        headers={"Authorization": "Bearer valid_token"}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "message_too_long"
    mock_engine_process.assert_not_called()


def test_fixture_teardown_preserves_existing_modules():
    """
    Verifica que o teardown do fixture restaura os objetos originais
    em vez de apenas deletá-los, preservando a identidade (``is``)
    de módulos que já existiam antes do fixture.

    Este teste executa diretamente a lógica de setup/teardown do
    fixture ``mock_external_dependencies`` sem depender do decorador
    autouse scope=module.

    A nova implementação do fixture:
    1. Registra as chaves explicitamente mockadas em _mocked_keys.
    2. No teardown, restaura somente as chaves em _mocked_keys.
    3. NÃO percorre todos os módulos backend.* para removê-los.
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

        # 2. Simulate fixture setup: snapshot + replace specific keys with mocks
        _original_modules = dict(sys.modules)
        _mocked_keys: set[str] = set()
        for _key in ('sentence_transformers', 'supabase'):
            _original_modules.setdefault(_key, sys.modules.get(_key))
            sys.modules[_key] = MagicMock()
            _mocked_keys.add(_key)

        # 3. Simulate fixture teardown (NEW implementation):
        #    Restore only the explicitly mocked keys, never backend.*
        def _restore_module(name):
            if name in _original_modules and _original_modules[name] is not None:
                sys.modules[name] = _original_modules[name]
            elif name in sys.modules:
                del sys.modules[name]

        for _key in _mocked_keys:
            _restore_module(_key)

        # Explicitly restore backend.main if it existed before
        if 'backend.main' in _original_modules:
            sys.modules['backend.main'] = _original_modules['backend.main']
        elif 'backend.main' in sys.modules:
            del sys.modules['backend.main']

        # 4. Assert identity is preserved (original objects restored)
        assert sys.modules.get("backend.main") is sentinel_main, \
            "backend.main should be restored to original sentinel"
        assert sys.modules.get("supabase") is sentinel_sb, \
            "supabase should be restored to original sentinel"
        assert sys.modules.get("sentence_transformers") is sentinel_st, \
            "sentence_transformers should be restored to original sentinel"

        # 5. Assert that NO backend.* modules were removed (key fix of the new impl)
        for k in list(sys.modules.keys()):
            if k.startswith('backend.') and k not in _original_modules:
                assert k in sys.modules, f"{k} should NOT have been removed"
    finally:
        # Restore actual modules
        for name in ("backend.main", "supabase", "sentence_transformers"):
            if saved[name] is not None:
                sys.modules[name] = saved[name]
            else:
                sys.modules.pop(name, None)
