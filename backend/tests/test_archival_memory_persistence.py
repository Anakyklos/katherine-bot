import sys
import os
from unittest.mock import MagicMock

# Capture original states BEFORE modifying them
_original_sys_modules = dict(sys.modules)
_original_env = dict(os.environ)

import pytest
from unittest.mock import patch

@pytest.fixture(autouse=True, scope="module")
def mock_external_dependencies():
    # Capture states at setup phase
    global _original_sys_modules, _original_env
    _original_sys_modules = dict(sys.modules)
    _original_env = dict(os.environ)

    # Mock external dependencies before importing any backend modules
    sys.modules['sentence_transformers'] = MagicMock()
    sys.modules['supabase'] = MagicMock()

    # Set up environment placeholders
    os.environ['GROQ_API_KEY'] = 'mock_key'
    os.environ['SUPABASE_URL'] = 'http://mock'
    os.environ['SUPABASE_SERVICE_ROLE_KEY'] = 'mock_key'

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
    from backend import memory, archival_memory
    class Holder:
        pass
    h = Holder()
    h.MemoryManager = memory.MemoryManager
    h.TurnPersistenceError = memory.TurnPersistenceError
    h.PersistedTurnRef = archival_memory.PersistedTurnRef
    h.ArchivalFact = archival_memory.ArchivalFact
    h.ArchivalExtractionEnvelope = archival_memory.ArchivalExtractionEnvelope
    h.ArchivalValidationError = archival_memory.ArchivalValidationError
    h.ArchivalDuplicateError = archival_memory.ArchivalDuplicateError
    return h


def test_save_turn_success(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    
    mock_resp = MagicMock()
    mock_resp.data = [
        {"id": 100, "user_id": "u1", "role": "user", "content": "hi"},
        {"id": 101, "user_id": "u1", "role": "assistant", "content": "hello"}
    ]
    mock_resp.error = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    
    ref = mm.save_turn("u1", "hi", "hello")
    assert isinstance(ref, backend.PersistedTurnRef)
    assert ref.user_id == "u1"
    assert ref.source_chat_log_id == 100
    assert ref.assistant_chat_log_id == 101


def test_save_turn_invalid_roles(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    
    mock_resp = MagicMock()
    mock_resp.data = [
        {"id": 100, "user_id": "u1", "role": "user", "content": "hi"},
        {"id": 101, "user_id": "u1", "role": "user", "content": "hello"}  # two users
    ]
    mock_resp.error = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "hi", "hello")


def test_load_persisted_user_message_success(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    
    mock_resp = MagicMock()
    mock_resp.data = [{"id": 100, "user_id": "u1", "role": "user", "content": "hi"}]
    mock_resp.error = None
    mm.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_resp
    
    msg = mm.load_persisted_user_message("u1", 100)
    assert msg == "hi"


def test_load_persisted_user_message_failure(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    
    # Mismatch user role (e.g. role is assistant instead of user)
    mock_resp = MagicMock()
    mock_resp.data = [{"id": 100, "user_id": "u1", "role": "assistant", "content": "hello"}]
    mm.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_resp
    with pytest.raises(KeyError):
        mm.load_persisted_user_message("u1", 100)


def test_store_archival_extraction_success(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    
    mock_resp = MagicMock()
    mock_resp.data = [{"source_chat_log_id": 100}]
    mock_resp.error = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    
    env = backend.ArchivalExtractionEnvelope(facts=[backend.ArchivalFact(content="F1", importance=0.9, tags=["t1"])])
    # Should run without exception
    mm.store_archival_extraction("u1", 100, "key_abc", env)
    
    mm.supabase.table.assert_called_with("archival_extractions")


def test_store_archival_extraction_conflict(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    
    class PostgrestAPIError(Exception):
        def __init__(self):
            self.code = "23505"
            self.message = "Unique violation"

    mm.supabase.table.return_value.insert.return_value.execute.side_effect = PostgrestAPIError()
    
    env = backend.ArchivalExtractionEnvelope(facts=[])
    # 23505 raises ArchivalDuplicateError
    with pytest.raises(backend.ArchivalDuplicateError):
        mm.store_archival_extraction("u1", 100, "key_abc", env)


def test_load_persisted_user_message_db_uninitialized(backend):
    mm = backend.MemoryManager()
    mm.supabase = None
    with pytest.raises(RuntimeError) as exc_info:
        mm.load_persisted_user_message("u1", 100)
    assert "Serviço de persistência indisponível." in str(exc_info.value)


def test_load_persisted_user_message_db_failure(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mm.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.side_effect = Exception("DB Connection Error")
    
    with pytest.raises(RuntimeError) as exc_info:
        mm.load_persisted_user_message("u1", 100)
    assert "Serviço de persistência indisponível." in str(exc_info.value)


# save_turn validation tests
def test_save_turn_invalid_records_count(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[{"id": 100}])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")


def test_save_turn_invalid_record_type(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[{"id": 100}, "invalid_type"])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")


def test_save_turn_missing_keys(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[
        {"id": 100, "role": "user"},
        {"id": 101, "role": "assistant"}  # missing user_id
    ])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")


def test_save_turn_invalid_ids(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    
    # Negative ID
    mock_resp_neg = MagicMock(data=[
        {"id": -1, "role": "user", "user_id": "u1", "content": "user msg"},
        {"id": 101, "role": "assistant", "user_id": "u1", "content": "bot msg"}
    ])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp_neg
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")
        
    # Non-integer ID
    mock_resp_float = MagicMock(data=[
        {"id": 100.5, "role": "user", "user_id": "u1", "content": "user msg"},
        {"id": 101, "role": "assistant", "user_id": "u1", "content": "bot msg"}
    ])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp_float
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")
        
    # Equal IDs
    mock_resp_equal = MagicMock(data=[
        {"id": 100, "role": "user", "user_id": "u1", "content": "user msg"},
        {"id": 100, "role": "assistant", "user_id": "u1", "content": "bot msg"}
    ])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp_equal
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")


def test_save_turn_wrong_user_id(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[
        {"id": 100, "role": "user", "user_id": "u1", "content": "user msg"},
        {"id": 101, "role": "assistant", "user_id": "another_user", "content": "bot msg"}
    ])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")


def test_save_turn_wrong_roles(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[
        {"id": 100, "role": "user", "user_id": "u1", "content": "user msg"},
        {"id": 101, "role": "user", "user_id": "u1", "content": "bot msg"}
    ])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")


def test_save_turn_mismatched_content(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[
        {"id": 100, "role": "user", "user_id": "u1", "content": "divergent user msg"},
        {"id": 101, "role": "assistant", "user_id": "u1", "content": "bot msg"}
    ])
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    with pytest.raises(backend.TurnPersistenceError):
        mm.save_turn("u1", "user msg", "bot msg")


# load_persisted_user_message validation tests
def test_load_persisted_user_message_multiple_rows(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[
        {"id": 100, "role": "user", "user_id": "u1", "content": "hi"},
        {"id": 100, "role": "user", "user_id": "u1", "content": "double"}
    ])
    mm.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_resp
    with pytest.raises(KeyError):
        mm.load_persisted_user_message("u1", 100)


def test_load_persisted_user_message_wrong_id(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[{"id": 999, "role": "user", "user_id": "u1", "content": "hi"}])
    mm.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_resp
    with pytest.raises(KeyError):
        mm.load_persisted_user_message("u1", 100)


def test_load_persisted_user_message_wrong_user(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[{"id": 100, "role": "user", "user_id": "another_user", "content": "hi"}])
    mm.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_resp
    with pytest.raises(KeyError):
        mm.load_persisted_user_message("u1", 100)


def test_load_persisted_user_message_non_str_content(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(data=[{"id": 100, "role": "user", "user_id": "u1", "content": 12345}])
    mm.supabase.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value = mock_resp
    with pytest.raises(KeyError):
        mm.load_persisted_user_message("u1", 100)


# store_archival_extraction validation tests
def test_store_archival_extraction_none_response(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mm.supabase.table.return_value.insert.return_value.execute.return_value = None
    env = backend.ArchivalExtractionEnvelope(facts=[])
    with pytest.raises(RuntimeError) as exc_info:
        mm.store_archival_extraction("u1", 100, "key_abc", env)
    assert "Sem resposta do banco" in str(exc_info.value)


def test_store_archival_extraction_error_in_response(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(error="Database error message")
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    env = backend.ArchivalExtractionEnvelope(facts=[])
    with pytest.raises(RuntimeError) as exc_info:
        mm.store_archival_extraction("u1", 100, "key_abc", env)
    assert "Erro retornado pelo banco" in str(exc_info.value)


def test_store_archival_extraction_invalid_data(backend):
    mm = backend.MemoryManager()
    mm.supabase = MagicMock()
    mock_resp = MagicMock(error=None)
    mock_resp.data = None
    mm.supabase.table.return_value.insert.return_value.execute.return_value = mock_resp
    env = backend.ArchivalExtractionEnvelope(facts=[])
    with pytest.raises(RuntimeError) as exc_info:
        mm.store_archival_extraction("u1", 100, "key_abc", env)
    assert "Resposta estruturalmente inválida" in str(exc_info.value)
