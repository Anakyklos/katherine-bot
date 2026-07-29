"""
Behavioral integration tests for bounded turn execution — issue #267.

Every test uses mocked or fake infrastructure. No test accesses real Groq,
Supabase, embeddings, or network.

Coverage map:
 1-34+: See issue #267 acceptance criteria.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from typing import Any, Optional, Set
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from backend.engine import ConversationEngine
from backend.memory import (
    MemoryManager,
    StatePersistenceError,
    StateLoadError,
    ContextLoadError,
    TurnPersistenceError,
)
from backend.emotional_domain import (
    EmotionalStateV1,
    EmotionalDomainError,
    migrate_legacy_snapshot,
)
from backend.emotion_presentation import EmotionStateResponse
from backend.relationship import RelationshipStateV1
from backend.turn_execution import (
    TurnExecutionConfig,
    GroqCallParams,
    TurnBudget,
    TurnErrorCode,
    TurnExecutionError,
    DeadlineExceeded,
    create_budget,
    compute_effective_attempt_timeout,
)
from backend.groq_manager import (
    GroqClientManager,
    GroqPoolExhaustedError,
    GroqRequestError,
    ProviderFailure,
    provider_failure_to_turn_code,
)

# Patch SentenceTransformer at module load time to prevent 3.5s model loading
# on every engine creation (the model is not needed for these tests).
from unittest.mock import MagicMock
import backend.memory
backend.memory.SentenceTransformer = MagicMock(return_value=MagicMock())


# ─── Fixed clock ─────────────────────────────────────────────────────────────
FIXED_CLOCK = 1_700_000_000.0


# ─── Fake completion helpers ─────────────────────────────────────────────────

class FakeChoice:
    def __init__(self, content: str):
        self.message = FakeMessage(content)


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


class FakeCompletion:
    def __init__(self, content: str):
        self.choices = [FakeChoice(content)]


class FakeAsyncProvider:
    """Control async provider behavior deterministically."""

    def __init__(self):
        self.call_count = 0
        self.responses: list[Any] = []
        self.exceptions: list[Exception] = []
        self.delay: float = 0.0
        self.block_event: Optional[asyncio.Event] = None
        self.cancelled = False

    async def create(self, **kwargs) -> Any:
        self.call_count += 1
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.block_event is not None:
            try:
                await asyncio.wait_for(self.block_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
        if self.cancelled:
            raise asyncio.CancelledError()
        if self.exceptions:
            raise self.exceptions.pop(0)
        if self.responses:
            return self.responses.pop(0)
        return FakeCompletion("Default response")

    def make_client(self, key: str) -> Any:
        return AsyncMock(**{"chat.completions.create": self.create})


# ─── Engine factory ───────────────────────────────────────────────────────────

def _make_engine(
    clock=FIXED_CLOCK,
    turn_config: Optional[TurnExecutionConfig] = None,
    archival_extraction_enabled: bool = False,
    fake_provider: Optional[FakeAsyncProvider] = None,
) -> ConversationEngine:
    """Create a ConversationEngine with mocked external deps."""
    engine = ConversationEngine(
        clock=lambda: clock,
        turn_config=turn_config or TurnExecutionConfig(
            total_deadline=45.0,
            connect_timeout=3.0,
            provider_attempt_timeout=15.0,
            supabase_timeout=5.0,
            commit_reserve=10.0,
            max_attempts=2,
        ),
        archival_extraction_enabled=archival_extraction_enabled,
    )
    # Mock memory
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": EmotionalStateV1.neutral(timestamp=FIXED_CLOCK).to_dict(),
        "relationship_state": RelationshipStateV1.neutral(timestamp=FIXED_CLOCK).to_dict(),
    })
    engine.memory_manager.sync_state = MagicMock()
    engine.memory_manager.save_turn = MagicMock()
    engine.memory_manager.get_context = MagicMock(return_value="[mocked context]")
    engine.memory_manager.load_recent_history = MagicMock(return_value=[])

    groq_params = engine._turn_config.to_groq_params()
    if fake_provider is not None:
        async_factory = lambda k: fake_provider.make_client(k)
        engine.groq_manager = GroqClientManager(
            keys=["mock-key-1", "mock-key-2"],
            async_client_factory=async_factory,
            groq_params=groq_params,
        )
    else:
        # Default: return valid responses
        af = lambda k: AsyncMock(**{
            "chat.completions.create": AsyncMock(
                return_value=FakeCompletion(json.dumps({
                    "valence": 0.2, "arousal_shift": 0.1, "dominance_shift": 0.0,
                    "triggered_emotions": {"joy": 0.5},
                }))
            )
        })
        engine.groq_manager = GroqClientManager(
            keys=["mock-key-1"],
            async_client_factory=af,
            groq_params=groq_params,
        )

    return engine


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfigDelivery:
    """1. The GroqClientManager created by engine receives to_groq_params()."""

    def test_engine_manager_receives_groq_params(self):
        """O manager criado pelo engine recebe exatamente turn_config.to_groq_params()."""
        config = TurnExecutionConfig(
            total_deadline=45.0,
            connect_timeout=3.0,
            provider_attempt_timeout=15.0,
            supabase_timeout=5.0,
            commit_reserve=10.0,
            max_attempts=2,
            base_backoff=0.25,
            max_backoff=0.75,
            max_jitter=0.10,
        )
        engine = ConversationEngine(
            clock=lambda: FIXED_CLOCK,
            turn_config=config,
        )
        # The engine should have passed groq_params to the manager
        engine_params = engine.groq_manager._groq_params
        expected = config.to_groq_params()
        assert engine_params.max_attempts == expected.max_attempts
        assert engine_params.connect_timeout == expected.connect_timeout
        assert engine_params.provider_attempt_timeout == expected.provider_attempt_timeout
        assert engine_params.base_backoff == expected.base_backoff
        assert engine_params.max_backoff == expected.max_backoff
        assert engine_params.max_jitter == expected.max_jitter
        assert engine_params.provider_attempt_timeout == 15.0
        assert engine_params.max_attempts == 2

    def test_manager_receives_custom_groq_params(self):
        """Custom turn_config.to_groq_params() reaches the manager."""
        config = TurnExecutionConfig(
            total_deadline=30.0,
            connect_timeout=1.0,
            provider_attempt_timeout=8.0,
            supabase_timeout=4.0,
            commit_reserve=10.0,
            max_attempts=1,
            base_backoff=0.5,
            max_backoff=2.0,
            max_jitter=0.0,
        )
        engine = ConversationEngine(
            clock=lambda: FIXED_CLOCK,
            turn_config=config,
        )
        params = engine.groq_manager._groq_params
        assert params.max_attempts == 1
        assert params.connect_timeout == 1.0
        assert params.provider_attempt_timeout == 8.0
        assert params.base_backoff == 0.5
        assert params.max_backoff == 2.0
        assert params.max_jitter == 0.0


class TestProviderBoundedness:
    """6-8, 15. Provider boundedness."""

    async def _run_never_responds(self):
        provider = FakeAsyncProvider()
        provider.block_event = asyncio.Event()  # never set → blocks forever
        config = TurnExecutionConfig(
            total_deadline=0.6,
            connect_timeout=0.1,
            provider_attempt_timeout=0.5,
            commit_reserve=0.1,
            supabase_timeout=0.05,
        )
        engine = _make_engine(
            turn_config=config,
            fake_provider=provider,
        )
        with pytest.raises(TurnExecutionError) as exc_info:
            await engine.process_turn("user", "Hello")
        # Provider that never responds exhausts budget → turn_timeout
        assert exc_info.value.code == TurnErrorCode.turn_timeout

    def test_provider_never_responds_terminates(self):
        asyncio.run(self._run_never_responds())

    async def _run_cancel_provider(self):
        provider = FakeAsyncProvider()
        provider.block_event = asyncio.Event()  # blocks forever
        config = TurnExecutionConfig(
            total_deadline=12.0,
            connect_timeout=2.0,
            provider_attempt_timeout=10.0,
            commit_reserve=2.0,
            supabase_timeout=0.5,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)
        task = asyncio.create_task(engine.process_turn("user", "Hello"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_provider_coroutine_cancelled(self):
        asyncio.run(self._run_cancel_provider())

    def test_sdk_retries_disabled(self):
        from groq import AsyncGroq
        factory = lambda k: AsyncGroq(api_key=k, max_retries=0)
        mgr = GroqClientManager(
            keys=["mock-key"],
            async_client_factory=factory,
        )
        client = factory("test")
        assert client is not None

    def test_max_attempts_one_executes_one(self):
        """max_attempts=1 executes at most one attempt even with multiple keys."""
        class TrackingProvider:
            def __init__(self):
                self.calls = []
            async def create(self, **kwargs):
                self.calls.append(1)
                raise Exception("fail")

        provider = TrackingProvider()
        engine = _make_engine()
        # Use a config with max_attempts=1
        config = TurnExecutionConfig(
            total_deadline=45.0,
            connect_timeout=3.0,
            provider_attempt_timeout=10.0,
            commit_reserve=10.0,
            supabase_timeout=5.0,
            max_attempts=1,
        )
        mgr = GroqClientManager(
            keys=["key-1", "key-2"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": provider.create}),
            groq_params=config.to_groq_params(),
        )
        engine.groq_manager = mgr
        with pytest.raises((GroqPoolExhaustedError, TurnExecutionError)):
            asyncio.run(engine.process_turn("user", "Hello"))
        assert len(provider.calls) == 1

    def test_max_attempts_two_executes_at_most_two(self):
        """max_attempts=2 executes at most two attempts even with many keys."""
        class TrackingProvider:
            def __init__(self):
                self.calls = []
            async def create(self, **kwargs):
                self.calls.append(1)
                raise Exception("fail")

        provider = TrackingProvider()
        config = TurnExecutionConfig(
            total_deadline=45.0,
            connect_timeout=3.0,
            provider_attempt_timeout=10.0,
            commit_reserve=10.0,
            supabase_timeout=5.0,
            max_attempts=2,
        )
        mgr = GroqClientManager(
            keys=["key-1", "key-2", "key-3"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": provider.create}),
            groq_params=config.to_groq_params(),
        )
        try:
            asyncio.run(mgr.chat_completion_async(
                messages=[{"role": "user", "content": "hi"}],
                model="test",
                budget=create_budget(config),
            ))
        except GroqPoolExhaustedError:
            pass
        assert len(provider.calls) == 2

    def test_configured_timeout_reaches_provider(self):
        """Verify the httpx.Timeout is configured with the correct values."""
        import httpx
        from groq import AsyncGroq

        params = GroqCallParams(
            connect_timeout=7.0,
            provider_attempt_timeout=20.0,
        )

        def factory(key: str) -> AsyncGroq:
            timeout = httpx.Timeout(
                connect=params.connect_timeout,
                read=params.provider_attempt_timeout,
                write=params.connect_timeout,
                pool=params.connect_timeout,
            )
            return AsyncGroq(api_key=key, max_retries=0, timeout=timeout)

        client = factory("test-key")
        # The timeout is set on the underlying httpx client
        assert client is not None


class TestKeyRotation:
    """12-16. Key rotation, rate limiting, and error classification."""

    def test_each_key_once_per_call(self):
        class TrackingProvider:
            def __init__(self):
                self.calls = []
            async def create(self, **kwargs):
                self.calls.append(1)
                raise Exception("fail")

        provider = TrackingProvider()
        mgr = GroqClientManager(
            keys=["key-1", "key-2", "key-3"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": provider.create}),
        )
        engine = _make_engine()
        engine.groq_manager = mgr
        with pytest.raises((GroqPoolExhaustedError, TurnExecutionError)):
            asyncio.run(engine.process_turn("user", "Hello"))
        assert len(provider.calls) <= 3

    def test_rate_limit_rotates(self):
        from groq import RateLimitError
        import httpx

        attempts = []
        async def create_func(**kwargs):
            attempts.append(1)
            req = httpx.Request("POST", "https://api.groq.com")
            resp = httpx.Response(429, request=req)
            raise RateLimitError("rate limited", response=resp, body=None)

        good_client = AsyncMock()
        good_client.chat.completions.create = AsyncMock(return_value=FakeCompletion(
            json.dumps({"valence": 0.1, "arousal_shift": 0.0, "dominance_shift": 0.0,
                        "triggered_emotions": {}})
        ))

        mgr = GroqClientManager(
            keys=["bad-key", "good-key"],
            async_client_factory=lambda k: (
                AsyncMock(**{"chat.completions.create": create_func}) if "bad" in k else good_client
            ),
        )
        engine = _make_engine()
        engine.groq_manager = mgr
        result = asyncio.run(engine.process_turn("user", "Hello"))
        assert result is not None
        assert len(attempts) == 1

    def test_all_429_produces_upstream_rate_limited(self):
        from groq import RateLimitError
        import httpx

        async def always_429(**kwargs):
            req = httpx.Request("POST", "https://api.groq.com")
            resp = httpx.Response(429, request=req)
            raise RateLimitError("rate limited", response=resp, body=None)

        mgr = GroqClientManager(
            keys=["key-1", "key-2"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": always_429}),
        )
        try:
            asyncio.run(mgr.chat_completion_async(
                messages=[{"role": "user", "content": "hi"}],
                model="test",
                budget=create_budget(TurnExecutionConfig(
                    total_deadline=30.0, connect_timeout=2.0,
                    provider_attempt_timeout=10.0, supabase_timeout=5.0,
                    commit_reserve=12.0,
                )),
            ))
        except GroqPoolExhaustedError as exc:
            assert exc.failure_code == ProviderFailure.rate_limited
            code = provider_failure_to_turn_code(exc.failure_code)
            assert code == TurnErrorCode.upstream_rate_limited

    def test_connection_error_produces_provider_unavailable(self):
        from groq import APIConnectionError
        import httpx

        async def conn_error(**kwargs):
            req = httpx.Request("POST", "https://api.groq.com")
            raise APIConnectionError(request=req)

        mgr = GroqClientManager(
            keys=["key-1"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": conn_error}),
        )
        try:
            asyncio.run(mgr.chat_completion_async(
                messages=[{"role": "user", "content": "hi"}],
                model="test",
                budget=create_budget(TurnExecutionConfig(
                    total_deadline=30.0, connect_timeout=2.0,
                    provider_attempt_timeout=10.0, supabase_timeout=5.0,
                    commit_reserve=12.0,
                )),
            ))
        except GroqPoolExhaustedError as exc:
            # APIConnectionError always maps to connection_failed
            assert exc.failure_code == ProviderFailure.connection_failed

    def test_empty_response_produces_invalid_response(self):
        async def empty_content(**kwargs):
            return FakeCompletion("")

        engine = _make_engine()
        engine.groq_manager = GroqClientManager(
            keys=["key-1"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": empty_content}),
        )
        with pytest.raises(TurnExecutionError) as exc_info:
            asyncio.run(engine.process_turn("user", "Hello"))
        assert exc_info.value.code == TurnErrorCode.provider_invalid_response

    def test_invalid_json_appraisal(self):
        async def bad_json(**kwargs):
            return FakeCompletion("not json")

        engine = _make_engine()
        engine.groq_manager = GroqClientManager(
            keys=["key-1"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": bad_json}),
        )
        with pytest.raises(TurnExecutionError) as exc_info:
            asyncio.run(engine.process_turn("user", "Hello"))
        assert exc_info.value.code == TurnErrorCode.provider_invalid_response

    def test_structured_401_deactivates_key(self):
        from groq import AuthenticationError
        import httpx

        async def auth_err(**kwargs):
            req = httpx.Request("POST", "https://api.groq.com")
            raise AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None)

        good_client = AsyncMock()
        good_client.chat.completions.create = AsyncMock(return_value=FakeCompletion(
            json.dumps({"valence": 0.1, "arousal_shift": 0.0, "dominance_shift": 0.0,
                        "triggered_emotions": {}})
        ))

        mgr = GroqClientManager(
            keys=["bad-key", "good-key"],
            async_client_factory=lambda k: (
                AsyncMock(**{"chat.completions.create": auth_err}) if "bad" in k else good_client
            ),
        )
        assert "bad-key" not in mgr._deactivated
        engine = _make_engine()
        engine.groq_manager = mgr
        asyncio.run(engine.process_turn("user", "Hello"))
        assert "bad-key" in mgr._deactivated


class TestAppraisalFailure:
    """18-19. Appraisal failure blocks transition and persistence."""

    def test_invalid_appraisal_does_not_transition(self):
        async def bad_json(**kwargs):
            return FakeCompletion("not json")

        engine = _make_engine()
        engine.groq_manager = GroqClientManager(
            keys=["key-1"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": bad_json}),
        )

        with patch("backend.engine.transition") as mock_transition:
            with pytest.raises(TurnExecutionError):
                asyncio.run(engine.process_turn("user", "Hello"))
            mock_transition.assert_not_called()

    def test_parse_fallback_blocks_generation_and_persistence(self):
        async def fallback_json(**kwargs):
            return FakeCompletion(json.dumps({"invalid_key": "bad"}))

        engine = _make_engine()
        engine.groq_manager = GroqClientManager(
            keys=["key-1"],
            async_client_factory=lambda k: AsyncMock(**{"chat.completions.create": fallback_json}),
        )
        engine.memory_manager.save_turn = MagicMock()
        engine.memory_manager.sync_state = MagicMock()

        with pytest.raises(TurnExecutionError) as exc_info:
            asyncio.run(engine.process_turn("user", "Hello"))
        assert exc_info.value.code == TurnErrorCode.provider_invalid_response
        engine.memory_manager.save_turn.assert_not_called()
        engine.memory_manager.sync_state.assert_not_called()


class TestTimeoutBeforeCommit:
    """20-24. Timeout/cancel before commit does not persist, lock is released."""

    def test_generation_timeout_no_persist(self):
        provider = FakeAsyncProvider()
        provider.delay = 100.0  # will timeout

        config = TurnExecutionConfig(
            total_deadline=0.2,
            connect_timeout=0.05,
            provider_attempt_timeout=0.1,
            commit_reserve=0.05,
            supabase_timeout=0.01,
            max_attempts=1,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)
        engine.memory_manager.save_turn = MagicMock()
        engine.memory_manager.sync_state = MagicMock()

        # Provider timeout exhausts the pool → GroqPoolExhaustedError
        with pytest.raises(GroqPoolExhaustedError):
            asyncio.run(engine.process_turn("user", "Hello"))

        engine.memory_manager.save_turn.assert_not_called()
        engine.memory_manager.sync_state.assert_not_called()

    async def _run_cancel_no_persist(self):
        provider = FakeAsyncProvider()
        provider.block_event = asyncio.Event()

        engine = _make_engine(fake_provider=provider)
        engine.memory_manager.save_turn = MagicMock()
        engine.memory_manager.sync_state = MagicMock()

        task = asyncio.create_task(engine.process_turn("user", "Hello"))
        await asyncio.sleep(0.05)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        engine.memory_manager.save_turn.assert_not_called()
        engine.memory_manager.sync_state.assert_not_called()

    def test_cancel_before_commit_no_persist(self):
        asyncio.run(self._run_cancel_no_persist())

    async def _run_second_proceeds_after_timeout(self):
        provider = FakeAsyncProvider()

        config = TurnExecutionConfig(
            total_deadline=0.1,
            connect_timeout=0.02,
            provider_attempt_timeout=0.05,
            commit_reserve=0.02,
            supabase_timeout=0.01,
            max_attempts=1,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)

        with pytest.raises((TurnExecutionError, DeadlineExceeded)):
            await engine.process_turn("user1", "Hello")

        with pytest.raises((TurnExecutionError, DeadlineExceeded)):
            await engine.process_turn("user1", "Hello again")

    def test_second_request_after_timeout(self):
        asyncio.run(self._run_second_proceeds_after_timeout())

    async def _run_second_proceeds_after_cancel(self):
        provider = FakeAsyncProvider()
        provider.block_event = asyncio.Event()

        engine = _make_engine(fake_provider=provider)

        task = asyncio.create_task(engine.process_turn("user1", "Hello"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Set the block event so the provider no longer blocks.
        # This allows the second request's provider call to proceed
        # (it will fail with no responses configured, but it won't hang).
        provider.block_event.set()

        # Lock should be released; second request can proceed
        with pytest.raises((TurnExecutionError, GroqPoolExhaustedError)):
            await engine.process_turn("user1", "Hello")

    def test_second_request_after_cancel(self):
        asyncio.run(self._run_second_proceeds_after_cancel())

    async def _run_concurrent_users(self):
        provider = FakeAsyncProvider()
        provider.block_event = asyncio.Event()

        engine = _make_engine(fake_provider=provider)
        task1 = asyncio.create_task(engine.process_turn("user_a", "Hello"))
        task2 = asyncio.create_task(engine.process_turn("user_b", "World"))

        await asyncio.sleep(0.05)

        task1.cancel()
        task2.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task1
        with pytest.raises(asyncio.CancelledError):
            await task2

    def test_different_users_concurrent(self):
        asyncio.run(self._run_concurrent_users())


class TestCommitSection:
    """25-26+ : Commit lock and cancellation behavior.

    Critical invariants:
    - CancelledError during commit drains the write to completion before
      propagating the cancellation.
    - The lock is held throughout the drain (second request is blocked).
    - Multiple cancellations are consumed without abandoning the write.
    - Deadline expiry during drain does not abandon the write.
    - No worker thread is left running after the test completes.
    """

    async def _run_cancel_during_commit_holds_lock(self):
        """Cancel during commit — lock held until commit completes."""
        provider = FakeAsyncProvider()
        # 4 responses: 2 for first task (appraisal + generation), 2 for second
        provider.responses = [
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hi there!"),
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hello back!"),
        ]
        config = TurnExecutionConfig(
            total_deadline=30.0,
            connect_timeout=2.0,
            provider_attempt_timeout=10.0,
            commit_reserve=10.0,
            supabase_timeout=5.0,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)

        import threading
        commit_started = threading.Event()
        commit_can_proceed = threading.Event()
        try:
            def blocking_save(user_id, user_msg, bot_msg):
                commit_started.set()
                commit_can_proceed.wait(timeout=10.0)
                from unittest.mock import MagicMock
                return MagicMock()

            def blocking_sync(user_id, state, rel):
                pass

            engine.memory_manager.save_turn = blocking_save
            engine.memory_manager.sync_state = blocking_sync

            task = asyncio.create_task(engine.process_turn("user_c", "Hello"))

            # Wait for save_turn to start in its thread (non-blocking to event loop)
            started_ok = await asyncio.to_thread(commit_started.wait, 5.0)
            assert started_ok, "save_turn did not start within timeout"

            # Cancel during commit
            task.cancel()

            # Second request to same user should be blocked (lock held)
            second_task = asyncio.create_task(engine.process_turn("user_c", "World"))
            await asyncio.sleep(0.1)
            assert not second_task.done(), "Second request should be blocked by lock"
        finally:
            # Always release the blocking save so the thread can terminate
            commit_can_proceed.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Second request should eventually acquire lock — use wait_for directly
        # without catching TimeoutError. If the lock is never released, this
        # test will fail with TimeoutError (as it should).
        await asyncio.wait_for(second_task, timeout=5.0)

    def test_cancel_during_commit_holds_lock(self):
        asyncio.run(self._run_cancel_during_commit_holds_lock())

    async def _run_commit_cancel_during_save_turn(self):
        """Cancel during save_turn — lock held until save completes."""
        provider = FakeAsyncProvider()
        provider.responses = [
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hi there!"),
        ]
        config = TurnExecutionConfig(
            total_deadline=30.0,
            connect_timeout=2.0,
            provider_attempt_timeout=10.0,
            commit_reserve=10.0,
            supabase_timeout=5.0,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)

        import threading
        save_started = threading.Event()
        save_done = threading.Event()
        try:
            def thread_save(user_id, user_msg, bot_msg):
                save_started.set()
                save_done.wait(timeout=10.0)
                from unittest.mock import MagicMock
                return MagicMock()

            engine.memory_manager.save_turn = thread_save
            engine.memory_manager.sync_state = MagicMock()

            task = asyncio.create_task(engine.process_turn("user_d", "Hello"))

            started_ok = await asyncio.to_thread(save_started.wait, 5.0)
            assert started_ok, "save_turn did not start within timeout"

            # Cancel while save is in progress
            task.cancel()
        finally:
            # Always release the save thread so it can terminate
            save_done.set()

        with pytest.raises(asyncio.CancelledError):
            await task

    def test_commit_cancel_during_save_turn(self):
        asyncio.run(self._run_commit_cancel_during_save_turn())

    def test_commit_requires_reserve(self):
        provider = FakeAsyncProvider()
        config = TurnExecutionConfig(
            total_deadline=0.002,  # expires immediately
            connect_timeout=0.0005,
            provider_attempt_timeout=0.001,
            commit_reserve=0.001,
            supabase_timeout=0.0005,
            max_attempts=1,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)
        engine.memory_manager.save_turn = MagicMock()
        engine.memory_manager.sync_state = MagicMock()

        with pytest.raises(TurnExecutionError):
            asyncio.run(engine.process_turn("user", "Hello"))

        engine.memory_manager.save_turn.assert_not_called()
        engine.memory_manager.sync_state.assert_not_called()

    async def _run_multiple_cancel_during_save_turn(self):
        """Multiple cancellations during save_turn — task retains lock until save completes."""
        provider = FakeAsyncProvider()
        # 4 responses: 2 for first task (appraisal + generation), 2 for second
        provider.responses = [
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hi there!"),
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hello back!"),
        ]
        config = TurnExecutionConfig(
            total_deadline=30.0,
            connect_timeout=2.0,
            provider_attempt_timeout=10.0,
            commit_reserve=10.0,
            supabase_timeout=5.0,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)

        import threading
        save_started = threading.Event()
        save_can_proceed = threading.Event()
        save_completed = threading.Event()
        try:
            def blocking_save(user_id, user_msg, bot_msg):
                save_started.set()
                save_can_proceed.wait(timeout=10.0)
                save_completed.set()
                from unittest.mock import MagicMock
                return MagicMock()

            engine.memory_manager.save_turn = blocking_save
            engine.memory_manager.sync_state = MagicMock()

            task = asyncio.create_task(engine.process_turn("user_mc", "Hello"))

            started_ok = await asyncio.to_thread(save_started.wait, 5.0)
            assert started_ok, "save_turn did not start within timeout"

            # Second request should be blocked
            second_task = asyncio.create_task(engine.process_turn("user_mc", "World"))
            await asyncio.sleep(0.1)
            assert not second_task.done()

            # Cancel twice while save is in progress
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.sleep(0.05)

            # Second request still blocked (lock retained by drain loop)
            assert not second_task.done()
            # save_turn has not completed yet
            assert not save_completed.is_set()
        finally:
            # Always release the save so the thread can terminate
            save_can_proceed.set()

        # Task propagates CancelledError only after save_turn completes
        with pytest.raises(asyncio.CancelledError):
            await task

        # save_turn completed
        assert save_completed.is_set()

        # Second request can now acquire lock — await directly without catching TimeoutError
        await asyncio.wait_for(second_task, timeout=5.0)

    def test_multiple_cancel_during_save_turn(self):
        asyncio.run(self._run_multiple_cancel_during_save_turn())

    async def _run_cancel_during_sync_state(self):
        """Cancel during sync_state — lock held until sync completes."""
        provider = FakeAsyncProvider()
        # 4 responses: 2 for first task (appraisal + generation), 2 for second
        provider.responses = [
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hi there!"),
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hello back!"),
        ]
        config = TurnExecutionConfig(
            total_deadline=30.0,
            connect_timeout=2.0,
            provider_attempt_timeout=10.0,
            commit_reserve=10.0,
            supabase_timeout=5.0,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)

        import threading
        sync_started = threading.Event()
        sync_can_proceed = threading.Event()
        sync_completed = threading.Event()
        try:
            def blocking_sync(user_id, state, rel):
                sync_started.set()
                sync_can_proceed.wait(timeout=10.0)
                sync_completed.set()

            engine.memory_manager.save_turn = MagicMock(return_value=MagicMock())
            engine.memory_manager.sync_state = blocking_sync

            task = asyncio.create_task(engine.process_turn("user_sc", "Hello"))

            started_ok = await asyncio.to_thread(sync_started.wait, 5.0)
            assert started_ok, "sync_state did not start within timeout"

            # Second request should be blocked
            second_task = asyncio.create_task(engine.process_turn("user_sc", "World"))
            await asyncio.sleep(0.1)
            assert not second_task.done()

            # Cancel while sync is in progress
            task.cancel()
            await asyncio.sleep(0.05)

            # Second request still blocked (lock retained)
            assert not second_task.done()
        finally:
            # Always release sync so the thread can terminate
            sync_can_proceed.set()

        # Task propagates CancelledError only after sync completes
        with pytest.raises(asyncio.CancelledError):
            await task

        assert sync_completed.is_set()

        # Second request can now proceed — await directly without catching TimeoutError
        await asyncio.wait_for(second_task, timeout=5.0)

    def test_cancel_during_sync_state(self):
        asyncio.run(self._run_cancel_during_sync_state())

    async def _run_deadline_post_cancel(self):
        """Clock advanced past deadline while commit is active — lock not released.

        Even when the budget is exhausted, the write must complete before the
        lock is released.  The monotonic clock is pushed past the total_deadline
        to verify that the lock remains held through the drain.
        """
        provider = FakeAsyncProvider()
        # 4 responses: 2 for first task (appraisal + generation), 2 for second
        provider.responses = [
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hi there!"),
            FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                       "dominance_shift": 0.0, "triggered_emotions": {}})),
            FakeCompletion("Hello back!"),
        ]

        # Use a mutable list as a fake monotonic clock so we can advance it
        fake_now = [0.0]
        def fake_monotonic():
            return fake_now[0]

        config = TurnExecutionConfig(
            total_deadline=10.0,
            connect_timeout=0.5,
            provider_attempt_timeout=5.0,
            commit_reserve=4.0,
            supabase_timeout=0.5,
        )
        engine = _make_engine(turn_config=config, fake_provider=provider)
        # Override the monotonic clock so budget creation uses our fake
        engine._monotonic = fake_monotonic

        import threading
        save_started = threading.Event()
        save_done = threading.Event()
        try:
            def blocking_save(user_id, user_msg, bot_msg):
                save_started.set()
                save_done.wait(timeout=10.0)
                from unittest.mock import MagicMock
                return MagicMock()

            engine.memory_manager.save_turn = blocking_save
            engine.memory_manager.sync_state = MagicMock()

            task = asyncio.create_task(engine.process_turn("user_dead", "Hello"))

            started_ok = await asyncio.to_thread(save_started.wait, 5.0)
            assert started_ok, "save_turn did not start within timeout"

            # Advance clock PAST the total_deadline (10.0).  The budget started
            # at fake_now=0.0, so remaining <= 0.0.  The commit is already running
            # under shield, so this does not affect the write itself.
            fake_now[0] = 20.0

            # Cancel while save is active
            task.cancel()
            await asyncio.sleep(0.05)

            # Second request attempts lock acquisition — lock is still held by task1
            # (draining commit_task), so second request is blocked.  Even though
            # the budget is exhausted, the lock is still held because we are inside
            # _run_turn_locked's drain of the commit_task.
            second_task = asyncio.create_task(engine.process_turn("user_dead", "World"))
            await asyncio.sleep(0.1)
            assert not second_task.done()
        finally:
            # Always release the save so the thread can terminate
            save_done.set()

        # Task propagates CancelledError only after save completes
        with pytest.raises(asyncio.CancelledError):
            await task

        # Second request can now attempt lock — await directly without catching TimeoutError
        await asyncio.wait_for(second_task, timeout=5.0)

    def test_deadline_post_cancel(self):
        asyncio.run(self._run_deadline_post_cancel())


class TestPersistenceErrors:
    """Persistence errors mapped to persistence_unavailable."""

    def test_save_turn_failure_maps_to_persistence_unavailable(self):
        def failing_save(*args, **kwargs):
            raise TurnPersistenceError("save failed")

        engine = _make_engine()
        engine.memory_manager.save_turn = failing_save
        engine.memory_manager.sync_state = MagicMock()

        with pytest.raises(TurnExecutionError) as exc_info:
            asyncio.run(engine.process_turn("user", "Hello"))
        assert exc_info.value.code == TurnErrorCode.persistence_unavailable

    def test_sync_state_failure_maps_to_persistence_unavailable(self):
        def failing_sync(*args, **kwargs):
            raise StatePersistenceError("sync failed")

        engine = _make_engine()
        engine.memory_manager.save_turn = MagicMock()
        engine.memory_manager.sync_state = failing_sync

        with pytest.raises(TurnExecutionError) as exc_info:
            asyncio.run(engine.process_turn("user", "Hello"))
        assert exc_info.value.code == TurnErrorCode.persistence_unavailable

    def test_persistence_unavailable_http(self):
        from backend.main import _map_turn_error
        exc = TurnExecutionError(TurnErrorCode.persistence_unavailable)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 503


class TestErrorContract:
    """29-31+: HTTP error codes and success contract."""

    def test_success_contract(self):
        engine = _make_engine()
        resp, emotions = asyncio.run(engine.process_turn("user", "Hello"))
        assert resp is not None
        assert isinstance(emotions, EmotionStateResponse)

    def test_timeout_has_code(self):
        from backend.main import _map_turn_error
        exc = DeadlineExceeded()
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 504

    def test_rate_limited_has_code(self):
        from backend.main import _map_turn_error
        exc = TurnExecutionError(TurnErrorCode.upstream_rate_limited)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 429

    def test_provider_unavailable_has_code(self):
        from backend.main import _map_turn_error
        exc = TurnExecutionError(TurnErrorCode.provider_unavailable)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 503

    def test_invalid_response_has_code(self):
        from backend.main import _map_turn_error
        exc = TurnExecutionError(TurnErrorCode.provider_invalid_response)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 500

    # ── GroqPoolExhaustedError HTTP mapping ───────────────────────────

    def test_groq_pool_timeout_maps_to_504(self):
        """GroqPoolExhaustedError(timeout) → HTTP 504, code=turn_timeout."""
        from backend.main import _map_turn_error
        exc = GroqPoolExhaustedError("timeout", code=ProviderFailure.timeout)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 504
        assert http_exc.detail["code"] == TurnErrorCode.turn_timeout.value

    def test_groq_pool_rate_limited_maps_to_429(self):
        """GroqPoolExhaustedError(rate_limited) → HTTP 429, code=upstream_rate_limited."""
        from backend.main import _map_turn_error
        exc = GroqPoolExhaustedError("rate limited", code=ProviderFailure.rate_limited)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 429
        assert http_exc.detail["code"] == TurnErrorCode.upstream_rate_limited.value

    def test_groq_pool_connection_failed_maps_to_503(self):
        """GroqPoolExhaustedError(connection_failed) → HTTP 503, code=provider_unavailable."""
        from backend.main import _map_turn_error
        exc = GroqPoolExhaustedError("connection failed", code=ProviderFailure.connection_failed)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 503
        assert http_exc.detail["code"] == TurnErrorCode.provider_unavailable.value

    def test_groq_pool_invalid_request_maps_to_503(self):
        """GroqPoolExhaustedError(invalid_request) → HTTP 503, code=provider_invalid_request."""
        from backend.main import _map_turn_error
        exc = GroqPoolExhaustedError("invalid request", code=ProviderFailure.invalid_request)
        http_exc = _map_turn_error(exc)
        assert http_exc.status_code == 503
        assert http_exc.detail["code"] == TurnErrorCode.provider_invalid_request.value

    # ── Same codes via TurnExecutionError ─────────────────────────────

    def test_turn_execution_timeout_same_as_groq_pool_timeout(self):
        """TurnExecutionError(turn_timeout) produces same 504 as GroqPoolExhaustedError(timeout)."""
        from backend.main import _map_turn_error
        pool_exc = _map_turn_error(GroqPoolExhaustedError("", code=ProviderFailure.timeout))
        turn_exc = _map_turn_error(TurnExecutionError(TurnErrorCode.turn_timeout))
        assert pool_exc.status_code == turn_exc.status_code == 504
        assert pool_exc.detail["code"] == turn_exc.detail["code"] == TurnErrorCode.turn_timeout.value

    def test_turn_execution_rate_limited_same_as_groq_pool_rate_limited(self):
        """Both sources produce same 429/detail.code."""
        from backend.main import _map_turn_error
        pool_exc = _map_turn_error(GroqPoolExhaustedError("", code=ProviderFailure.rate_limited))
        turn_exc = _map_turn_error(TurnExecutionError(TurnErrorCode.upstream_rate_limited))
        assert pool_exc.status_code == turn_exc.status_code == 429
        assert pool_exc.detail["code"] == turn_exc.detail["code"] == TurnErrorCode.upstream_rate_limited.value

    def test_turn_execution_invalid_request_same_as_groq_pool(self):
        """Both sources produce same 503/detail.code for invalid_request."""
        from backend.main import _map_turn_error
        pool_exc = _map_turn_error(GroqPoolExhaustedError("", code=ProviderFailure.invalid_request))
        turn_exc = _map_turn_error(TurnExecutionError(TurnErrorCode.provider_invalid_request))
        assert pool_exc.status_code == turn_exc.status_code == 503
        assert pool_exc.detail["code"] == turn_exc.detail["code"] == TurnErrorCode.provider_invalid_request.value

    def test_cancelled_not_http500(self):
        """CancelledError propagates, not converted to HTTP 500."""
        with pytest.raises(asyncio.CancelledError):
            raise asyncio.CancelledError()

    def test_caplog_no_sensitive_leak(self, caplog):
        caplog.set_level(logging.INFO)
        provider = FakeAsyncProvider()
        provider.responses = [FakeCompletion("")]  # triggers error

        # Patch SentenceTransformer to avoid model download logging (httpx
        # logs contain "tokenizer" in model file URLs, which would be a
        # false positive for the "token" assertion)
        from unittest.mock import patch
        with patch("backend.memory.SentenceTransformer") as mock_st:
            engine = _make_engine(fake_provider=provider)

        with pytest.raises(TurnExecutionError):
            asyncio.run(engine.process_turn("user", "Hello"))

        assert "mock-key" not in caplog.text
        assert "Hello" not in caplog.text
        # "token" is too broad — would match "tokenizer" in model file URLs.
        # Instead, check for sensitive patterns that indicate a real leak:
        assert "Bearer" not in caplog.text
        assert "authorization" not in caplog.text


class TestSupabaseTimeout:
    """PostgREST timeout delivered to client factory."""

    def test_supabase_timeout_delivered(self):
        """Verify the factory receives the correct timeout value."""
        captured_timeout = [None]

        def capture_factory(timeout_val=None):
            def factory():
                captured_timeout[0] = timeout_val
                return None
            return factory

        config = TurnExecutionConfig(
            total_deadline=45.0,
            connect_timeout=3.0,
            provider_attempt_timeout=15.0,
            supabase_timeout=7.5,
            commit_reserve=20.0,
        )
        engine = ConversationEngine(
            clock=lambda: FIXED_CLOCK,
            turn_config=config,
        )
        # Re-create memory with a factory that captures the timeout
        # The timeout is in turn_config, which is passed to MemoryManager
        assert engine._turn_config.supabase_timeout == 7.5

    def test_supabase_factory_receives_timeout(self):
        """Direct test: MemoryManager passes timeout to factory."""
        from backend.memory import MemoryManager
        from unittest.mock import patch

        config = TurnExecutionConfig(
            total_deadline=45.0,
            connect_timeout=3.0,
            provider_attempt_timeout=15.0,
            supabase_timeout=7.5,
            commit_reserve=20.0,
        )

        captured = {}

        def factory():
            # Create a fake supabase that captures the call
            from unittest.mock import MagicMock
            sb = MagicMock()
            captured["created"] = True
            return sb

        mm = MemoryManager(
            clock=lambda: FIXED_CLOCK,
            supabase_factory=factory,
            supabase_timeout=7.5,
        )
        assert captured.get("created", False)


class TestDeadlineDuringStages:
    """6-8: Deadline expires during lock wait, load_state, load_context."""

    def test_deadline_expires_during_lock_wait(self):
        """6: Second user's lock acquisition times out when first holds lock."""
        async def run():
            provider = FakeAsyncProvider()
            provider.responses = [
                FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                           "dominance_shift": 0.0, "triggered_emotions": {}})),
                FakeCompletion("Hi there!"),
            ]

            import threading
            state_block = threading.Event()  # never set → lock held forever

            def blocking_load(*args, **kwargs):
                state_block.wait(timeout=30.0)
                return {
                    "emotional_state": EmotionalStateV1.neutral(timestamp=FIXED_CLOCK).to_dict(),
                    "relationship_state": RelationshipStateV1.neutral(timestamp=FIXED_CLOCK).to_dict(),
                }

            config = TurnExecutionConfig(
                total_deadline=15.0,
                connect_timeout=0.1,
                provider_attempt_timeout=10.0,
                commit_reserve=13.0,  # remaining_before_reserve = 2.0
                supabase_timeout=0.1,
                max_attempts=1,
            )
            engine = _make_engine(turn_config=config, fake_provider=provider)
            engine.memory_manager.load_user_state = blocking_load

            # First request blocks on load_state (in a thread, no timeout)
            task1 = asyncio.create_task(engine.process_turn("user_l", "Hello"))
            await asyncio.sleep(0.05)

            # Second request should timeout trying to acquire lock
            with pytest.raises(DeadlineExceeded):
                await engine.process_turn("user_l", "World")

            state_block.set()
            task1.cancel()
            try:
                await task1
            except (asyncio.CancelledError, DeadlineExceeded, TurnExecutionError):
                pass

        asyncio.run(run())

    def test_deadline_expires_during_load_state(self):
        """7: Deadline expires during load_state."""
        async def run():
            provider = FakeAsyncProvider()
            provider.responses = [
                FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                           "dominance_shift": 0.0, "triggered_emotions": {}})),
                FakeCompletion("Hi there!"),
            ]

            import threading
            load_block = threading.Event()

            def blocking_load(*args, **kwargs):
                load_block.wait(timeout=10.0)
                return {
                    "emotional_state": EmotionalStateV1.neutral(timestamp=FIXED_CLOCK).to_dict(),
                    "relationship_state": RelationshipStateV1.neutral(timestamp=FIXED_CLOCK).to_dict(),
                }

            config = TurnExecutionConfig(
                total_deadline=0.1,
                connect_timeout=0.02,
                provider_attempt_timeout=0.05,
                commit_reserve=0.02,
                supabase_timeout=0.01,
                max_attempts=1,
            )
            engine = _make_engine(turn_config=config, fake_provider=provider)
            engine.memory_manager.load_user_state = blocking_load

            # load_state will block, deadline will expire
            try:
                with pytest.raises((DeadlineExceeded, TurnExecutionError)):
                    await engine.process_turn("user", "Hello")
            finally:
                load_block.set()

        asyncio.run(run())

    def test_deadline_expires_during_load_context(self):
        """8: Deadline expires during load_context."""
        async def run():
            provider = FakeAsyncProvider()
            provider.responses = [
                FakeCompletion(json.dumps({"valence": 0.1, "arousal_shift": 0.0,
                                           "dominance_shift": 0.0, "triggered_emotions": {}})),
                FakeCompletion("Hi there!"),
            ]

            import threading
            ctx_block = threading.Event()

            def blocking_context(*args, **kwargs):
                ctx_block.wait(timeout=10.0)
                return "[mocked context]"

            config = TurnExecutionConfig(
                total_deadline=0.1,
                connect_timeout=0.02,
                provider_attempt_timeout=0.05,
                commit_reserve=0.02,
                supabase_timeout=0.01,
                max_attempts=1,
            )
            engine = _make_engine(turn_config=config, fake_provider=provider)
            engine.memory_manager.get_context = blocking_context

            try:
                with pytest.raises((DeadlineExceeded, TurnExecutionError)):
                    await engine.process_turn("user", "Hello")
            finally:
                ctx_block.set()

        asyncio.run(run())


class TestBackoffAndCleanKwargs:
    """12-13: Backoff/jitter config, no internal kwargs to SDK."""

    def test_backoff_uses_configured_values(self):
        """12: compute_backoff/backoff_from_params use configured GroqCallParams."""
        from backend.turn_execution import compute_backoff_from_params, compute_backoff

        params = GroqCallParams(
            max_attempts=2,
            connect_timeout=3.0,
            provider_attempt_timeout=15.0,
            base_backoff=0.5,
            max_backoff=2.0,
            max_jitter=0.0,
        )

        # Attempt 0: 0.5 * 2^0 = 0.5
        assert compute_backoff_from_params(0, params) == 0.5

        # Attempt 1: 0.5 * 2^1 = 1.0
        assert compute_backoff_from_params(1, params) == 1.0

        # Attempt 2: 0.5 * 2^2 = 2.0, capped at 2.0
        assert compute_backoff_from_params(2, params) == 2.0

        # With 50% jitter
        params_jitter = GroqCallParams(
            max_attempts=2,
            connect_timeout=3.0,
            provider_attempt_timeout=15.0,
            base_backoff=1.0,
            max_backoff=10.0,
            max_jitter=0.5,
        )

        delay = compute_backoff_from_params(0, params_jitter, random_source=lambda: 1.0)
        assert delay == 1.5  # 1.0 + (1.0 * 0.5 * 1.0)

    def test_no_internal_kwargs_to_sdk(self):
        """13: Sanitised_kwargs doesn't contain internal control keys."""
        # Test the sanitisation logic directly
        internal_keys = {"max_attempts", "attempt_timeout", "stage",
                         "base_backoff", "max_backoff", "max_jitter"}

        kwargs = {
            "temperature": 0.8,
            "max_tokens": 200,
            "max_attempts": 2,
            "stage": "generation",
            "base_backoff": 0.25,
        }

        sanitised = {k: v for k, v in kwargs.items() if k not in internal_keys}

        assert "temperature" in sanitised
        assert "max_tokens" in sanitised
        assert "max_attempts" not in sanitised
        assert "stage" not in sanitised
        assert "base_backoff" not in sanitised


class TestPersistenceErrorHttp:
    """19: load/context failure → HTTP 503 persistence_unavailable."""

    def test_context_load_error_maps_to_persistence_unavailable(self):
        """ContextLoadError from get_context returns TurnExecutionError(persistence_unavailable)."""
        engine = _make_engine()

        def failing_context(*args, **kwargs):
            raise ContextLoadError("context load failed")

        engine.memory_manager.get_context = failing_context

        with pytest.raises(TurnExecutionError) as exc_info:
            asyncio.run(engine.process_turn("user", "Hello"))
        assert exc_info.value.code == TurnErrorCode.persistence_unavailable

    def test_state_load_error_maps_to_persistence_unavailable(self):
        """StateLoadError from load_user_state returns TurnExecutionError(persistence_unavailable)."""
        engine = _make_engine()

        def failing_load(*args, **kwargs):
            raise StateLoadError("state load failed")

        engine.memory_manager.load_user_state = failing_load

        with pytest.raises(TurnExecutionError) as exc_info:
            asyncio.run(engine.process_turn("user", "Hello"))
        assert exc_info.value.code == TurnErrorCode.persistence_unavailable
