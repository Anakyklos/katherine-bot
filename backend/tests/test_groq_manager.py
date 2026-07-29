import logging
import threading
import time
import pytest
import httpx
from unittest.mock import AsyncMock
from groq import RateLimitError, APIStatusError, AuthenticationError, APIConnectionError
from backend.groq_manager import (
    GroqClientManager,
    GroqConfigurationError,
    GroqPoolExhaustedError,
    GroqRequestError,
)

# Helpers for mocking Groq Client
class MockCompletion:
    def __init__(self, content="Mock response"):
        self.choices = [MockChoice(content)]

class MockChoice:
    def __init__(self, content):
        self.message = MockMessage(content)

class MockMessage:
    def __init__(self, content):
        self.content = content

class MockCompletions:
    def __init__(self, create_func):
        self.create = create_func

class MockChat:
    def __init__(self, create_func):
        self.completions = MockCompletions(create_func)

class MockClient:
    def __init__(self, create_func):
        self.chat = MockChat(create_func)

def assert_sanitized(caplog_text: str):
    """Verifies that no secrets, keys, prefixes, tokens, or custom details are leaked."""
    sensitive_markers = [
        "key-one", "key-two", "key-three", "11111111", "22222222", "333333",
        "secret-token", "user-sensitive-message", "assistant-response-secret",
        "very-secret-error-marker"
    ]
    for marker in sensitive_markers:
        assert marker not in caplog_text, f"Leaked sensitive marker in logs: {marker}"

# 1. Empty initialization
def test_empty_keys_initialization(monkeypatch):
    with pytest.raises(GroqConfigurationError) as excinfo:
        GroqClientManager(keys=[])
    assert "No Groq API keys configured" in str(excinfo.value)
    
    with pytest.raises(GroqConfigurationError):
        GroqClientManager(keys=["", "   "])

    # Apply patch to the explicit accessor function to return []
    import backend.groq_manager
    monkeypatch.setattr(backend.groq_manager.groq_keys, "get_groq_api_keys", lambda: [])
    with pytest.raises(GroqConfigurationError):
        GroqClientManager()

    # Apply patch to the explicit accessor function to return empty strings
    monkeypatch.setattr(backend.groq_manager.groq_keys, "get_groq_api_keys", lambda: ["", "   "])
    with pytest.raises(GroqConfigurationError):
        GroqClientManager()

    # Restore the monkeypatch
    monkeypatch.undo()

    # Confirm that after restoration, a new instantiation successfully resolves get_groq_api_keys
    # returning the test environment placeholders and thus constructs successfully
    manager = GroqClientManager()
    assert "mock_groq_key_placeholder" in manager._keys

# 2. Concurrent calls do not corrupt the pool
def test_concurrent_access_no_corruption():
    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222", "key-three-333333"],
        client_factory=lambda k: MockClient(lambda *args, **kwargs: MockCompletion("ok"))
    )
    
    results = []
    results_lock = threading.Lock()
    worker_errors = []
    worker_errors_lock = threading.Lock()
    
    def worker(idx):
        try:
            for _ in range(50):
                res = manager.chat_completion(messages=[{"role": "user", "content": "hello"}], model="test-model")
                with results_lock:
                    results.append(res.choices[0].message.content)
        except BaseException as e:
            with worker_errors_lock:
                worker_errors.append(e)
            
    threads = [threading.Thread(target=worker, args=(i,), name=f"groq-worker-{i}") for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.0)

    alive = [t.name for t in threads if t.is_alive()]
    assert not worker_errors, f"Worker errors: {worker_errors}"
    assert not alive, f"Threads did not terminate: {alive}"
    assert len(results) == 500
    assert all(r == "ok" for r in results)

# 3. Two threads marking key as rate limited maintain state consistency
def test_concurrent_rate_limiting_cooldown(caplog):
    caplog.set_level(logging.WARNING)
    fake_time = 1000.0
    manager = GroqClientManager(
        keys=["key-one-11111111"],
        time_provider=lambda: fake_time
    )
    
    worker_errors = []
    worker_errors_lock = threading.Lock()
    
    def mark():
        try:
            manager._mark_key_rate_limited("key-one-11111111")
        except BaseException as e:
            with worker_errors_lock:
                worker_errors.append(e)
        
    threads = [threading.Thread(target=mark, name=f"rate-limit-worker-{i}") for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.0)

    alive = [t.name for t in threads if t.is_alive()]
    assert not worker_errors, f"Worker errors: {worker_errors}"
    assert not alive, f"Threads did not terminate: {alive}"
    assert manager._cooldowns["key-one-11111111"] == 1010.0
    assert "event=groq_key_rate_limited" in caplog.text
    assert_sanitized(caplog.text)

# 4. Two threads deactivating the same invalid key only record it once
def test_concurrent_deactivation(caplog):
    caplog.set_level(logging.ERROR)
    manager = GroqClientManager(
        keys=["key-one-11111111"]
    )
    
    worker_errors = []
    worker_errors_lock = threading.Lock()
    
    def deactivate():
        try:
            manager._deactivate_key("key-one-11111111")
        except BaseException as e:
            with worker_errors_lock:
                worker_errors.append(e)
        
    threads = [threading.Thread(target=deactivate, name=f"deactivate-worker-{i}") for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.0)

    alive = [t.name for t in threads if t.is_alive()]
    assert not worker_errors, f"Worker errors: {worker_errors}"
    assert not alive, f"Threads did not terminate: {alive}"
    assert "key-one-11111111" in manager._deactivated
    assert len(manager._deactivated) == 1
    assert "event=groq_key_disabled" in caplog.text
    assert_sanitized(caplog.text)

# 5. Slow call does not block other threads
def test_slow_client_does_not_hold_lock():
    slow_entered_event = threading.Event()
    slow_done_event = threading.Event()
    
    def slow_create(*args, **kwargs):
        slow_entered_event.set()
        try:
            slow_done_event.wait(timeout=5.0)
        except BaseException:
            pass
        return MockCompletion("slow")
        
    def fast_create(*args, **kwargs):
        return MockCompletion("fast")
        
    def make_client(key):
        if "one" in key:
            return MockClient(slow_create)
        return MockClient(fast_create)
        
    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )
    
    results = {}
    worker_errors = []
    worker_errors_lock = threading.Lock()
    
    VALID_MSG = [{"role": "user", "content": "hello"}]
    def run_thread_1():
        try:
            res = manager.chat_completion(messages=VALID_MSG, model="test")
            results["t1"] = res.choices[0].message.content
        except BaseException as e:
            with worker_errors_lock:
                worker_errors.append(e)
        finally:
            slow_done_event.set()
        
    def run_thread_2():
        try:
            res = manager.chat_completion(messages=VALID_MSG, model="test")
            results["t2"] = res.choices[0].message.content
        except BaseException as e:
            with worker_errors_lock:
                worker_errors.append(e)
        
    t1 = threading.Thread(target=run_thread_1, name="groq-slow-worker")
    t2 = threading.Thread(target=run_thread_2, name="groq-fast-worker")
    
    t1.start()
    
    # Wait deterministically until Thread 1 has selected key-one and entered slow_create
    assert slow_entered_event.wait(timeout=2.0)
    
    t2.start()
    
    # Thread 2 should finish quickly since it got key-two and is not blocked by the lock
    t2.join(timeout=2.0)
    alive = [t.name for t in [t2] if t.is_alive()]
    assert not worker_errors, f"Worker errors: {worker_errors}"
    assert not alive, f"Threads did not terminate: {alive}"
    assert results["t2"] == "fast"
    
    # Resume slow call
    slow_done_event.set()
    t1.join(timeout=2.0)
    alive = [t.name for t in [t1] if t.is_alive()]
    assert not worker_errors, f"Worker errors: {worker_errors}"
    assert not alive, f"Threads did not terminate: {alive}"
    assert results["t1"] == "slow"

# 6. Cooldown expired makes key eligible again
def test_clock_progression_cooldown():
    fake_time = 1000.0
    def time_provider():
        return fake_time
        
    manager = GroqClientManager(
        keys=["key-one-11111111"],
        time_provider=time_provider,
        client_factory=lambda k: MockClient(lambda *args, **kwargs: MockCompletion("ok"))
    )
    
    manager._mark_key_rate_limited("key-one-11111111")
    VALID_MSG = [{"role": "user", "content": "hello"}]
    
    # Cooled down
    with pytest.raises(GroqPoolExhaustedError):
        manager.chat_completion(messages=VALID_MSG, model="test")
        
    fake_time = 1009.0
    with pytest.raises(GroqPoolExhaustedError):
        manager.chat_completion(messages=VALID_MSG, model="test")
        
    fake_time = 1010.0
    res = manager.chat_completion(messages=VALID_MSG, model="test")
    assert res.choices[0].message.content == "ok"

# 7. Bounded attempts per call
def test_bounded_attempts():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            mock_request = httpx.Request("POST", "https://api.groq.com")
            response_429 = httpx.Response(429, request=mock_request)
            raise RateLimitError("rate limited", response=response_429, body=None)
        return MockClient(create)
        
    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )
    
    VALID_MSG = [{"role": "user", "content": "hello"}]
    with pytest.raises(GroqPoolExhaustedError):
        manager.chat_completion(messages=VALID_MSG, model="test")
        
    assert len(calls) == 2
    assert "key-one-11111111" in calls
    assert "key-two-22222222" in calls

# 8. Rate limit attempts next eligible key
def test_rate_limit_rotation():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            if "one" in key:
                mock_request = httpx.Request("POST", "https://api.groq.com")
                response_429 = httpx.Response(429, request=mock_request)
                raise RateLimitError("rate limited", response=response_429, body=None)
            return MockCompletion("success")
        return MockClient(create)
        
    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )
    VALID_MSG = [{"role": "user", "content": "hello"}]
    res = manager.chat_completion(messages=VALID_MSG, model="test")
    assert res.choices[0].message.content == "success"
    assert len(calls) == 2
    assert calls == ["key-one-11111111", "key-two-22222222"]
    assert "key-one-11111111" in manager._cooldowns

# 9. All keys unavailable produces sanitized exception
def test_all_keys_unavailable(caplog):
    caplog.set_level(logging.WARNING)
    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=lambda k: MockClient(lambda *args, **kwargs: MockCompletion("ok"))
    )
    
    manager._deactivate_key("key-one-11111111")
    manager._deactivate_key("key-two-22222222")
    
    VALID_MSG = [{"role": "user", "content": "hello"}]
    with pytest.raises(GroqPoolExhaustedError) as excinfo:
        manager.chat_completion(messages=VALID_MSG, model="test")
        
    assert "deactivated" in str(excinfo.value)
    assert "key-one" not in str(excinfo.value)
    assert "event=groq_pool_unavailable" in caplog.text
    assert_sanitized(caplog.text)

# 10. Structured 401 recognition
def test_structured_401_authentication_error():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            if "one" in key:
                mock_request = httpx.Request("POST", "https://api.groq.com")
                response_401 = httpx.Response(401, request=mock_request)
                raise AuthenticationError("Invalid Key", response=response_401, body=None)
            return MockCompletion("success")
        return MockClient(create)
        
    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )
    VALID_MSG = [{"role": "user", "content": "hello"}]
    res = manager.chat_completion(messages=VALID_MSG, model="test")
    assert res.choices[0].message.content == "success"
    assert "key-one-11111111" in manager._deactivated
    assert "key-two-22222222" not in manager._deactivated
    assert calls == ["key-one-11111111", "key-two-22222222"]

def test_structured_401_api_status_error():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            if "one" in key:
                mock_request = httpx.Request("POST", "https://api.groq.com")
                response_401 = httpx.Response(401, request=mock_request)
                raise APIStatusError("401 Unauthorized", response=response_401, body=None)
            return MockCompletion("success")
        return MockClient(create)
        
    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )
    VALID_MSG = [{"role": "user", "content": "hello"}]
    res = manager.chat_completion(messages=VALID_MSG, model="test")
    assert res.choices[0].message.content == "success"
    assert "key-one-11111111" in manager._deactivated
    assert "key-two-22222222" not in manager._deactivated
    assert calls == ["key-one-11111111", "key-two-22222222"]

# 11 & 12. Transient/Unexpected errors and Sanitization checks
def test_unexpected_error_sanitization(caplog):
    caplog.set_level(logging.ERROR)
    
    def make_client(key):
        def create(*args, **kwargs):
            raise ValueError("very-secret-error-marker inside key-one-11111111 with token secret-token")
        return MockClient(create)
        
    manager = GroqClientManager(
        keys=["key-one-11111111"],
        client_factory=make_client
    )
    
    # Generic Exception is now treated as transient; when all keys exhausted,
    # GroqPoolExhaustedError is raised (not GroqRequestError).
    with pytest.raises(GroqPoolExhaustedError) as excinfo:
        manager.chat_completion(messages=[{"role": "user", "content": "user-sensitive-message"}], model="test")
        
    # Assert public exception message is sanitized
    assert "very-secret-error-marker" not in str(excinfo.value)
    assert "key-one" not in str(excinfo.value)
    
    # Assert logs are sanitized
    assert "event=groq_request_failed" in caplog.text
    assert_sanitized(caplog.text)

# 13. APIConnectionError on first key rotates to the second key and returns success
def test_transient_connection_error_rotation():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            if "one" in key:
                mock_request = httpx.Request("POST", "https://api.groq.com")
                raise APIConnectionError(request=mock_request)
            return MockCompletion("success")
        return MockClient(create)

    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )

    VALID_MSG = [{"role": "user", "content": "hello"}]
    res = manager.chat_completion(messages=VALID_MSG, model="test")
    assert res.choices[0].message.content == "success"
    assert calls == ["key-one-11111111", "key-two-22222222"]

# 14. APIStatusError 5xx on first key rotates to the second key and returns success
def test_transient_5xx_status_error_rotation():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            if "one" in key:
                mock_request = httpx.Request("POST", "https://api.groq.com")
                response_503 = httpx.Response(503, request=mock_request)
                raise APIStatusError("503 Service Unavailable", response=response_503, body=None)
            return MockCompletion("success")
        return MockClient(create)

    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )

    VALID_MSG = [{"role": "user", "content": "hello"}]
    res = manager.chat_completion(messages=VALID_MSG, model="test")
    assert res.choices[0].message.content == "success"
    assert calls == ["key-one-11111111", "key-two-22222222"]

# 15. All keys failing with connection/5xx errors raise GroqPoolExhaustedError (sanitized)
def test_all_keys_failing_transient():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            mock_request = httpx.Request("POST", "https://api.groq.com")
            response_500 = httpx.Response(500, request=mock_request)
            raise APIStatusError("500 Internal Error", response=response_500, body=None)
        return MockClient(create)

    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )

    VALID_MSG = [{"role": "user", "content": "hello"}]
    with pytest.raises(GroqPoolExhaustedError) as excinfo:
        manager.chat_completion(messages=VALID_MSG, model="test")

    assert len(calls) == 2
    assert "key-one" not in str(excinfo.value)

# 16. Client factory throwing exception with sensitive token is caught, logged safely, and raises GroqRequestError
def test_client_factory_leak_sanitization(caplog):
    caplog.set_level(logging.ERROR)
    def failing_factory(key):
        raise ValueError("very-secret-error-marker inside key-one-11111111 with token secret-token")

    manager = GroqClientManager(
        keys=["key-one-11111111"],
        client_factory=failing_factory
    )

    with pytest.raises(GroqRequestError) as excinfo:
        manager.chat_completion(messages=[{"role": "user", "content": "hello"}], model="test")

    assert "Falha ao executar requisição Groq" in str(excinfo.value)
    assert "very-secret-error-marker" not in str(excinfo.value)
    assert "event=groq_request_failed" in caplog.text
    assert_sanitized(caplog.text)

# 17. Non-retryable HTTP error (e.g., 400 Bad Request) fails immediately without retry/rotation loop
def test_non_retryable_http_error_fails_immediately():
    calls = []
    def make_client(key):
        def create(*args, **kwargs):
            calls.append(key)
            mock_request = httpx.Request("POST", "https://api.groq.com")
            response_400 = httpx.Response(400, request=mock_request)
            raise APIStatusError("400 Bad Request", response=response_400, body=None)
        return MockClient(create)

    manager = GroqClientManager(
        keys=["key-one-11111111", "key-two-22222222"],
        client_factory=make_client
    )

    with pytest.raises(GroqRequestError):
        manager.chat_completion(messages=[{"role": "user", "content": "hello"}], model="test")

    assert len(calls) == 1
    assert calls == ["key-one-11111111"]


# 18. Sync invalid envelope rejected before key acquisition
def test_sync_invalid_envelope_rejected():
    """Invalid message structure is rejected before any key access."""
    manager = GroqClientManager(
        keys=["key-one-11111111"],
        client_factory=lambda k: MockClient(lambda *args, **kwargs: MockCompletion("ok"))
    )

    with pytest.raises(GroqRequestError) as excinfo:
        manager.chat_completion(messages=[], model="test")
    assert "Falha ao executar requisição Groq" in str(excinfo.value)


# 19. Sync oversized envelope rejected before key acquisition
def test_sync_oversized_envelope_rejected(caplog):
    """Oversized message is rejected before any key access."""
    caplog.set_level(logging.ERROR)
    manager = GroqClientManager(
        keys=["key-one-11111111"],
        client_factory=lambda k: MockClient(lambda *args, **kwargs: MockCompletion("ok"))
    )

    oversized = "x" * 20000
    with pytest.raises(GroqRequestError) as excinfo:
        manager.chat_completion(
            messages=[{"role": "user", "content": oversized}],
            model="test"
        )
    assert "Falha ao executar requisição Groq" in str(excinfo.value)
    assert "event=provider_input_budget_exceeded" in caplog.text


# 20. Async invalid envelope rejected before key acquisition
@pytest.mark.anyio
async def test_async_invalid_envelope_rejected():
    """Invalid message structure is rejected before any key access (async)."""
    from backend.turn_execution import TurnBudget, TurnExecutionConfig

    config = TurnExecutionConfig(
        total_deadline=30.0,
        connect_timeout=2.0,
        provider_attempt_timeout=10.0,
        supabase_timeout=5.0,
        commit_reserve=12.0,
        max_attempts=1,
    )
    manager = GroqClientManager(
        keys=["key-one-11111111"],
        async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": AsyncMock()}),
        groq_params=config.to_groq_params(),
    )

    budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)
    with pytest.raises(GroqRequestError) as excinfo:
        await manager.chat_completion_async(
            messages=[],
            model="test",
            budget=budget,
            stage="test",
        )
    assert "Falha ao executar requisição Groq" in str(excinfo.value)


# 21. Async oversized envelope rejected before key acquisition
@pytest.mark.anyio
async def test_async_oversized_envelope_rejected(caplog):
    """Oversized message is rejected before any key access (async)."""
    caplog.set_level(logging.ERROR)
    from backend.turn_execution import TurnBudget, TurnExecutionConfig

    config = TurnExecutionConfig(
        total_deadline=30.0,
        connect_timeout=2.0,
        provider_attempt_timeout=10.0,
        supabase_timeout=5.0,
        commit_reserve=12.0,
        max_attempts=1,
    )
    manager = GroqClientManager(
        keys=["key-one-11111111"],
        async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": AsyncMock()}),
        groq_params=config.to_groq_params(),
    )

    oversized = "x" * 20000
    budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)
    with pytest.raises(GroqRequestError) as excinfo:
        await manager.chat_completion_async(
            messages=[{"role": "user", "content": oversized}],
            model="test",
            budget=budget,
            stage="test",
        )
    assert "Falha ao executar requisição Groq" in str(excinfo.value)
    assert "event=provider_input_budget_exceeded" in caplog.text
    assert "key-one" not in caplog.text


# 22. Async 4xx (e.g. 400, 422) produces invalid_request through full chain
@pytest.mark.anyio
async def test_async_4xx_produces_invalid_request():
    """Verify APIStatusError 4xx (terminal) in async path produces invalid_request.

    Full chain: GroqClientManager → ConversationEngine → _map_turn_error → HTTP 503
    """
    import json
    from unittest.mock import MagicMock
    from backend.engine import ConversationEngine
    from backend.turn_execution import TurnExecutionConfig, TurnErrorCode, TurnExecutionError
    from backend.emotional_domain import EmotionalStateV1
    from backend.relationship import RelationshipStateV1
    from backend.main import _map_turn_error

    async def always_400(**kwargs):
        mock_request = httpx.Request("POST", "https://api.groq.com")
        response_400 = httpx.Response(400, request=mock_request)
        raise APIStatusError("400 Bad Request", response=response_400, body=None)

    config = TurnExecutionConfig(
        total_deadline=30.0,
        connect_timeout=2.0,
        provider_attempt_timeout=10.0,
        supabase_timeout=5.0,
        commit_reserve=12.0,
        max_attempts=1,
    )
    engine = ConversationEngine(
        clock=lambda: 1700000000.0,
        turn_config=config,
    )
    # Mock memory
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": EmotionalStateV1.neutral(timestamp=1700000000.0).to_dict(),
        "relationship_state": RelationshipStateV1.neutral(timestamp=1700000000.0).to_dict(),
    })
    engine.memory_manager.sync_state = MagicMock()
    engine.memory_manager.save_turn = MagicMock()
    engine.memory_manager.get_context = MagicMock(return_value="[mocked context]")
    engine.memory_manager.get_context_components = MagicMock(return_value={
        "persona": "Katherine...",
        "user_profile_str": "{}",
        "memory_str": "",
        "history_list": [],
        "assembled": "[mocked context]",
    })
    engine.memory_manager.load_recent_history = MagicMock(return_value=[])

    mgr = GroqClientManager(
        keys=["key-1-alpha", "key-2-beta"],
        async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": always_400}),
        groq_params=config.to_groq_params(),
    )
    engine.groq_manager = mgr

    # The 4xx terminal error is classified as invalid_request and raises
    # GroqPoolExhaustedError (not TurnExecutionError) because the pool
    # exhausts all keys with this failure code.
    from backend.groq_manager import GroqPoolExhaustedError
    with pytest.raises(GroqPoolExhaustedError) as exc_info:
        await engine.process_turn("user", "Hello")
    assert exc_info.value.failure_code is not None
    from backend.groq_manager import ProviderFailure, provider_failure_to_turn_code
    turn_code = provider_failure_to_turn_code(exc_info.value.failure_code)
    assert turn_code == TurnErrorCode.provider_invalid_request

    # Now verify HTTP mapping produces 503
    http_exc = _map_turn_error(exc_info.value)
    assert http_exc.status_code == 503
    detail = http_exc.detail
    assert detail["code"] == TurnErrorCode.provider_invalid_request.value
