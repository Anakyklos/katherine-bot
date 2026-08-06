import os
import sys
import logging
import json
from unittest.mock import MagicMock

# Capture original states BEFORE modifying them
_original_sys_modules = dict(sys.modules)
_original_env = dict(os.environ)

import pytest
from unittest.mock import patch, AsyncMock
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

import backend.main as main

REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture(autouse=True, scope="module")
def mock_external_dependencies():
    # Capture states at setup phase
    global _original_sys_modules, _original_env
    _original_sys_modules = dict(sys.modules)
    _original_env = dict(os.environ)

    # Mock external dependencies before importing any backend modules
    sys.modules['sentence_transformers'] = MagicMock()
    sys.modules['supabase'] = MagicMock()

    # Setup environment variables immediately
    os.environ['GROQ_API_KEY'] = 'mock_key'
    os.environ['SUPABASE_URL'] = 'http://mock'
    os.environ['SUPABASE_SERVICE_ROLE_KEY'] = 'mock_key'
    os.environ['ADMISSION_HMAC_SECRET'] = 'test-admission-secret-that-is-at-least-32-bytes'
    os.environ['TRUSTED_PROXY_CIDRS'] = ''

    yield

    # Restore environment variables safely without breaking Pytest internals
    for k in list(os.environ.keys()):
        if k.startswith("PYTEST_"):
            continue
        if k not in _original_env:
            del os.environ[k]
    for k, v in _original_env.items():
        if not k.startswith("PYTEST_"):
            os.environ[k] = v

    # Restore ONLY the modules this fixture explicitly mocked.
    # Never iterate over all sys.modules keys or all backend.* modules:
    # doing so destroys class identity for modules imported naturally by
    # other test files during the same session (e.g. backend.trusted_context,
    # backend.memory, backend.engine).
    for _key in ('sentence_transformers', 'supabase'):
        _original = _original_sys_modules.get(_key)
        if _original is not None:
            sys.modules[_key] = _original
        elif _key in sys.modules:
            del sys.modules[_key]


@pytest.fixture(scope="module")
def backend(mock_external_dependencies):
    from backend import engine, archival_memory
    class Holder:
        pass
    h = Holder()
    h.ConversationEngine = engine.ConversationEngine
    h.PersistedTurnRef = archival_memory.PersistedTurnRef
    h.ArchivalDuplicateError = archival_memory.ArchivalDuplicateError
    h.ArchivalValidationError = archival_memory.ArchivalValidationError
    h.compute_idempotency_key = archival_memory.compute_idempotency_key
    return h


@pytest.fixture(scope="module")
def client_app(mock_external_dependencies):
    """Real engine composed through the application factory with mocked env."""
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
    return client_app.app.state.dependencies.conversation_engine


@pytest.fixture
def mock_supabase(client_app):
    engine = _app_engine(client_app)
    deps = client_app.app.state.dependencies
    with patch.object(engine.memory_manager, 'supabase', MagicMock()) as mock_sb, \
         patch.object(deps, 'auth_client', mock_sb), \
         patch.object(deps, 'persistence_client', mock_sb):
        mock_sb.rpc.return_value.execute.return_value.data = [
            {"decision": "admitted", "retry_after_seconds": 0}
        ]
        yield mock_sb


class MockUser:
    def __init__(self, id):
        self.id = id


class MockAuthResponse:
    def __init__(self, user):
        self.user = user


@pytest.mark.anyio
async def test_run_archival_extraction_llm_failure(backend, caplog):
    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_persisted_user_message = MagicMock(return_value="Hello")
    
    # Mock Groq client failure
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=Exception("Groq error"))
    
    ref = backend.PersistedTurnRef(user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2)
    
    with caplog.at_level(logging.ERROR):
        await engine.run_archival_extraction(ref)
        
    assert "archival_extraction_llm_failed" in caplog.text
    # Check sensitive values are not logged
    assert "user123" not in caplog.text
    assert "Hello" not in caplog.text
    assert "Groq error" not in caplog.text
    engine.memory_manager.store_archival_extraction.assert_not_called()


@pytest.mark.anyio
async def test_run_archival_extraction_validation_failure(backend, caplog):
    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_persisted_user_message = MagicMock(return_value="Hello")
    
    # Mock Groq client returning invalid fact payload (importance is bool)
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = json.dumps({
        "facts": [{"content": "hello", "importance": True, "tags": []}]
    })
    async def mock_async(*args, **kwargs):
        return mock_resp
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=mock_async)
    
    ref = backend.PersistedTurnRef(user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2)
    
    with caplog.at_level(logging.WARNING):
        await engine.run_archival_extraction(ref)
        
    assert "archival_extraction_invalid" in caplog.text
    assert "user123" not in caplog.text
    engine.memory_manager.store_archival_extraction.assert_not_called()


@pytest.mark.anyio
async def test_run_archival_extraction_duplicate(backend, caplog):
    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_persisted_user_message = MagicMock(return_value="Hello")
    
    m = MagicMock()
    m.choices = [MagicMock()]
    m.choices[0].message.content = json.dumps({
        "facts": [{"content": "likes coding", "importance": 0.9, "tags": []}],
        "schema_version": 1,
        "extractor_version": 1
    })
    async def _async_return(*args, **kwargs):
        return m
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=_async_return)
    
    # Simulate unique constraint failure treated as duplicate success
    engine.memory_manager.store_archival_extraction.side_effect = backend.ArchivalDuplicateError("Duplicate")
    
    ref = backend.PersistedTurnRef(user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2)
    
    with caplog.at_level(logging.INFO):
        await engine.run_archival_extraction(ref)
        
    assert "archival_extraction_duplicate" in caplog.text
    assert "user123" not in caplog.text


@pytest.mark.anyio
async def test_run_archival_extraction_store_failed(backend, caplog):
    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_persisted_user_message = MagicMock(return_value="Hello secret message")
    
    m = MagicMock()
    m.choices = [MagicMock()]
    m.choices[0].message.content = json.dumps({
        "facts": [{"content": "likes coding", "importance": 0.9, "tags": []}],
        "schema_version": 1,
        "extractor_version": 1
    })
    async def _async_return(*args, **kwargs):
        return m
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=_async_return)
    
    # Simulate general database failure
    engine.memory_manager.store_archival_extraction.side_effect = Exception("DB connection failed secret token")
    
    ref = backend.PersistedTurnRef(user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2)
    
    with caplog.at_level(logging.ERROR):
        await engine.run_archival_extraction(ref)
        
    assert "archival_extraction_store_failed" in caplog.text
    # Check sensitive values are not logged
    assert "user123" not in caplog.text
    assert "Hello secret message" not in caplog.text
    assert "DB connection failed secret token" not in caplog.text


def _valid_legacy_emotion_dict():
    import time
    return {
        "pleasure": 0.0,
        "arousal": 0.0,
        "dominance": 0.0,
        "libido": 0.5,
        "aggression": 0.0,
        "connection": 0.5,
        "energy": 0.8,
        "tension": 0.0,
        "coping_mode": "HEALTHY",
        "last_update": time.time(),
    }


@pytest.mark.anyio
async def test_process_turn_schedules_background_task(backend):
    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    
    # Mock all internal methods of process_turn to focus on orchestration
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": _valid_legacy_emotion_dict(),
        "relationship_state": {}
    })
    # load_context_data returns an empty valid LoadedContextData — the engine
    # calls the module-level build_context_bundle directly (no mock needed).
    from backend.trusted_context import LoadedContextData
    engine.memory_manager.load_context_data = MagicMock(return_value=LoadedContextData(
        history_rows=(),
        retrieved_memories=(),
        profile_snapshot={},
        persona_snapshot="",
    ))
    
    # Mock save_turn and sync_state to track order
    call_order = []
    
    def mock_save_turn(user_id, user_msg, bot_msg):
        call_order.append("save_turn")
        return backend.PersistedTurnRef(user_id=user_id, source_chat_log_id=1, assistant_chat_log_id=2)
        
    def mock_sync_state(user_id, state, relationship):
        call_order.append("sync_state")
        
    engine.memory_manager.save_turn = MagicMock(side_effect=mock_save_turn)
    engine.memory_manager.sync_state = MagicMock(side_effect=mock_sync_state)
    
    from unittest.mock import AsyncMock
    
    # Responses: first for appraisal (JSON), then for generation (text)
    responses = [
        MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps({
            "valence": 0.1, "arousal_shift": 0.0, "dominance_shift": 0.0,
            "triggered_emotions": {"joy": 0.5},
        })))]),
        MagicMock(choices=[MagicMock(message=MagicMock(content="assistant reply"))]),
    ]
    
    async def async_create(**kwargs):
        return responses.pop(0)
    
    # Override the groq manager's async factory to return a mock client
    engine.groq_manager._async_client_factory = lambda k: AsyncMock(**{
        "chat.completions.create": async_create
    })
    
    bg_tasks = MagicMock(spec=BackgroundTasks)
    
    # Run process_turn
    resp, emotions = await engine.process_turn("user123", "hello", background_tasks=bg_tasks)
    
    from backend.emotion_presentation import EmotionStateResponse
    # Assert return format — process_turn returns EmotionStateResponse, not dict
    assert resp == "assistant reply"
    assert isinstance(emotions, EmotionStateResponse)
    assert emotions.schema_version == 1
    
    # Assert execution order: sync_state must complete before scheduling background task
    assert call_order == ["save_turn", "sync_state"]
    
    # Assert background task scheduled
    bg_tasks.add_task.assert_called_once()
    args, kwargs = bg_tasks.add_task.call_args
    assert args[0] == engine.run_archival_extraction
    assert isinstance(args[1], backend.PersistedTurnRef)
    assert args[1].user_id == "user123"


@pytest.mark.anyio
async def test_process_turn_does_not_schedule_when_extraction_disabled(backend):
    """With default archival_extraction_enabled=False, no background task is scheduled."""
    engine = backend.ConversationEngine()  # default False
    assert not engine.archival_extraction_enabled
    
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": _valid_legacy_emotion_dict(),
        "relationship_state": {}
    })
    # load_context_data returns an empty valid LoadedContextData — the engine
    # calls the module-level build_context_bundle directly (no mock needed).
    from backend.trusted_context import LoadedContextData
    engine.memory_manager.load_context_data = MagicMock(return_value=LoadedContextData(
        history_rows=(),
        retrieved_memories=(),
        profile_snapshot={},
        persona_snapshot="",
    ))
    
    engine.memory_manager.save_turn = MagicMock(return_value=backend.PersistedTurnRef(
        user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2
    ))
    engine.memory_manager.sync_state = MagicMock()
    
    from unittest.mock import AsyncMock
    
    # Responses: first for appraisal (JSON), then for generation (text)
    responses = [
        MagicMock(choices=[MagicMock(message=MagicMock(content=json.dumps({
            "valence": 0.1, "arousal_shift": 0.0, "dominance_shift": 0.0,
            "triggered_emotions": {"joy": 0.5},
        })))]),
        MagicMock(choices=[MagicMock(message=MagicMock(content="reply"))]),
    ]
    
    async def async_create(**kwargs):
        return responses.pop(0)
    
    engine.groq_manager._async_client_factory = lambda k: AsyncMock(**{
        "chat.completions.create": async_create
    })
    
    bg_tasks = MagicMock(spec=BackgroundTasks)
    
    await engine.process_turn("user123", "hello", background_tasks=bg_tasks)
    
    # Background task should NOT be scheduled
    bg_tasks.add_task.assert_not_called()


@pytest.mark.anyio
async def test_run_archival_extraction_disabled_returns_early(backend):
    """With archival_extraction_enabled=False, run_archival_extraction returns without doing anything."""
    engine = backend.ConversationEngine()  # default False
    assert not engine.archival_extraction_enabled
    
    # Even with fully unmocked dependencies, it should return without touching anything
    engine.memory_manager = MagicMock()
    engine.groq_manager = MagicMock()
    
    ref = backend.PersistedTurnRef(user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2)
    
    await engine.run_archival_extraction(ref)
    
    # No message loading, no Groq, no storage
    engine.memory_manager.load_persisted_user_message.assert_not_called()
    engine.memory_manager.store_archival_extraction.assert_not_called()
    engine.groq_manager.chat_completion.assert_not_called()


def test_chat_response_format(client_app, mock_supabase, monkeypatch):
    engine = _app_engine(client_app)
    from backend.relationship import RelationshipStateV1
    
    mock_user = MockUser(id="user123")
    mock_supabase.auth.get_user.return_value = MockAuthResponse(user=mock_user)
    
    # Mock load_user_state to succeed with valid legacy emotion state
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": _valid_legacy_emotion_dict(),
        "relationship_state": RelationshipStateV1.neutral(timestamp=1700000000.0).to_dict()
    })
    
    from backend.emotion_presentation import EmotionStateResponse, PublicPAD, PublicDominantEmotion
    
    # Mock process_turn to return valid EmotionStateResponse and restore it after the test.
    mock_emotion = EmotionStateResponse(
        schema_version=1,
        mood_label="NEUTRA",
        pad=PublicPAD(pleasure=0.0, arousal=0.0, dominance=0.0),
        dominant_emotions=[
            PublicDominantEmotion(name="joy", intensity=0.5),
        ],
        timestamp=1700000000.0,
    )
    from backend.process_turn import ProcessTurnResult
    monkeypatch.setattr(
        engine,
        "process_turn",
        AsyncMock(
            return_value=ProcessTurnResult(
                committed=object(),
                response="My response text",
                emotion_state=mock_emotion,
            )
        ),
    )
    
    response = client_app.post(
        "/chat",
        json={"request_id": REQUEST_ID, "message": "hello"},
        headers={"Authorization": "Bearer valid_token"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "response" in data
    assert "emotion_state" in data
    assert len(data) == 2  # exactly response and emotion_state
    assert data["response"] == "My response text"
    # EmotionStateResponse is serialised as a typed dict, not a plain dict
    assert data["emotion_state"]["schema_version"] == 1
    assert data["emotion_state"]["mood_label"] == "NEUTRA"
    assert data["emotion_state"]["pad"]["pleasure"] == 0.0
    assert len(data["emotion_state"]["dominant_emotions"]) == 1


@pytest.mark.anyio
async def test_run_archival_extraction_load_failure(backend, caplog):
    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_persisted_user_message = MagicMock(side_effect=Exception("DB connection error user123 secret message"))
    
    ref = backend.PersistedTurnRef(user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2)
    
    with caplog.at_level(logging.ERROR):
        await engine.run_archival_extraction(ref)
        
    assert "archival_extraction_load_failed" in caplog.text
    # No leak
    assert "user123" not in caplog.text
    assert "secret message" not in caplog.text
    assert "DB connection error" not in caplog.text


def test_different_turns_same_content_distinct(backend):
    # Different turns with the same content (same user, but different source_chat_log_id)
    # produce different idempotency keys
    key1 = backend.compute_idempotency_key("user123", 100, 1)
    key2 = backend.compute_idempotency_key("user123", 101, 1)
    
    assert key1 != key2
    
    # Same turn (same user, same source_chat_log_id) produces same key (idempotency)
    key3 = backend.compute_idempotency_key("user123", 100, 1)
    assert key1 == key3


# ---------------------------------------------------------------------------
# ARCHIVAL EXTRACTION FLAG PARAMETER TESTS
# ---------------------------------------------------------------------------


def test_archival_extraction_default_disabled(backend):
    """ConversationEngine() defaults to archival_extraction_enabled=False."""
    engine = backend.ConversationEngine()
    assert engine.archival_extraction_enabled is False


def test_archival_extraction_explicit_true(backend):
    """ConversationEngine(archival_extraction_enabled=True) sets flag True."""
    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    assert engine.archival_extraction_enabled is True


def test_archival_extraction_explicit_false(backend):
    """ConversationEngine(archival_extraction_enabled=False) sets flag False."""
    engine = backend.ConversationEngine(archival_extraction_enabled=False)
    assert engine.archival_extraction_enabled is False


def test_no_real_external_dependencies_proof():
    # Programmatic proof that real SentenceTransformer, Supabase or Groq are not used
    # in any test environment instantiation
    import sys
    from unittest.mock import MagicMock
    
    assert isinstance(sys.modules.get('sentence_transformers'), MagicMock)
    assert isinstance(sys.modules.get('supabase'), MagicMock)


@pytest.mark.anyio
async def test_run_archival_extraction_cancelled_during_store(backend, caplog):
    """Cancel during archival store — write is not abandoned.

    When the background archival extraction task is cancelled while
    store_archival_extraction is running, the write helper must drain
    the worker to completion before propagating the cancellation.
    """
    import threading
    import asyncio

    engine = backend.ConversationEngine(archival_extraction_enabled=True)
    engine.memory_manager = MagicMock()
    engine.memory_manager.load_persisted_user_message = MagicMock(return_value="Hello")

    m = MagicMock()
    m.choices = [MagicMock()]
    m.choices[0].message.content = json.dumps({
        "facts": [{"content": "likes coding", "importance": 0.9, "tags": []}],
        "schema_version": 1,
        "extractor_version": 1
    })
    async def _async_return(*args, **kwargs):
        return m
    engine.groq_manager.chat_completion_async = MagicMock(side_effect=_async_return)

    # Block store_archival_extraction in a thread so we can verify drain
    store_started = threading.Event()
    store_can_proceed = threading.Event()
    store_completed = threading.Event()

    def blocking_store(user_id, source_chat_log_id, idempotency_key, envelope):
        store_started.set()
        store_can_proceed.wait(timeout=10.0)
        store_completed.set()

    engine.memory_manager.store_archival_extraction = blocking_store

    ref = backend.PersistedTurnRef(
        user_id="user123", source_chat_log_id=1, assistant_chat_log_id=2
    )

    task = asyncio.create_task(engine.run_archival_extraction(ref))

    # Wait for store_archival_extraction to start in its thread
    started_ok = await asyncio.to_thread(store_started.wait, 5.0)
    assert started_ok, "store_archival_extraction did not start"

    # Cancel the task while store is in progress
    task.cancel()
    await asyncio.sleep(0.05)

    # Store should not have completed yet (blocked by event)
    assert not store_completed.is_set(), "store should still be blocked"

    # Release store to complete
    store_can_proceed.set()

    # Task should propagate CancelledError after draining
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store_completed.is_set(), "store must have completed (drained)"


def test_sql_guarantees_mock():
    # Verify RLS and composite FK setup exists in the migration schema
    schema_path = "supabase_schema.sql"
    with open(schema_path, "r", encoding="utf-8") as f:
        schema_content = f.read()
        
    # Check RLS exists and FOR ALL is removed
    assert "alter table archival_extractions enable row level security;" in schema_content
    assert "for select" in schema_content
    assert "for all" not in schema_content
    
    # Check composite foreign key constraints exist
    assert "foreign key (user_id, source_chat_log_id) references chat_logs(user_id, id)" in schema_content
    assert "constraint chat_logs_user_id_id_key unique (user_id, id)" in schema_content
    
    # Check mandatory not null fields
    assert "user_id text not null" in schema_content
    assert "source_chat_log_id bigint not null" in schema_content
