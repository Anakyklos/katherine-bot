import logging
import threading
import time
import pytest
import httpx
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
    lock = threading.Lock()
    
    def worker():
        for _ in range(50):
            res = manager.chat_completion(messages=[], model="test-model")
            with lock:
                results.append(res.choices[0].message.content)
            
    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
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
    
    def mark():
        manager._mark_key_rate_limited("key-one-11111111")
        
    threads = [threading.Thread(target=mark) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    assert manager._cooldowns["key-one-11111111"] == 1010.0
    assert "event=groq_key_rate_limited" in caplog.text
    assert_sanitized(caplog.text)

# 4. Two threads deactivating the same invalid key only record it once
def test_concurrent_deactivation(caplog):
    caplog.set_level(logging.ERROR)
    manager = GroqClientManager(
        keys=["key-one-11111111"]
    )
    
    def deactivate():
        manager._deactivate_key("key-one-11111111")
        
    threads = [threading.Thread(target=deactivate) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
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
        slow_done_event.wait(timeout=5.0)
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
    
    def run_thread_1():
        res = manager.chat_completion(messages=[], model="test")
        results["t1"] = res.choices[0].message.content
        
    def run_thread_2():
        res = manager.chat_completion(messages=[], model="test")
        results["t2"] = res.choices[0].message.content
        
    t1 = threading.Thread(target=run_thread_1)
    t2 = threading.Thread(target=run_thread_2)
    
    t1.start()
    
    # Wait deterministically until Thread 1 has selected key-one and entered slow_create
    assert slow_entered_event.wait(timeout=2.0)
    
    t2.start()
    
    # Thread 2 should finish quickly since it got key-two and is not blocked by the lock
    t2.join(timeout=2.0)
    assert not t2.is_alive()
    assert results["t2"] == "fast"
    
    # Resume slow call
    slow_done_event.set()
    t1.join(timeout=2.0)
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
    
    # Cooled down
    with pytest.raises(GroqPoolExhaustedError):
        manager.chat_completion(messages=[], model="test")
        
    fake_time = 1009.0
    with pytest.raises(GroqPoolExhaustedError):
        manager.chat_completion(messages=[], model="test")
        
    fake_time = 1010.0
    res = manager.chat_completion(messages=[], model="test")
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
    
    with pytest.raises(GroqPoolExhaustedError):
        manager.chat_completion(messages=[], model="test")
        
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
    
    res = manager.chat_completion(messages=[], model="test")
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
    
    with pytest.raises(GroqPoolExhaustedError) as excinfo:
        manager.chat_completion(messages=[], model="test")
        
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
    
    res = manager.chat_completion(messages=[], model="test")
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
    
    res = manager.chat_completion(messages=[], model="test")
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

    res = manager.chat_completion(messages=[], model="test")
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

    res = manager.chat_completion(messages=[], model="test")
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

    with pytest.raises(GroqPoolExhaustedError) as excinfo:
        manager.chat_completion(messages=[], model="test")

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
        manager.chat_completion(messages=[], model="test")

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
        manager.chat_completion(messages=[], model="test")

    assert len(calls) == 1
    assert calls == ["key-one-11111111"]
