"""
Tests for ``backend/turn_execution.py`` — pure domain module.

Covers:
- Default config validity
- Strict environment parsing (all options, rejection of invalid values)
- Budget creation and deadline calculation with injected clock
- Backoff computation
- Stage events
"""

from __future__ import annotations

import asyncio
import math
import time as _real_time
from unittest.mock import patch

import pytest

from backend.turn_execution import (
    TurnExecutionConfig,
    TurnBudget,
    TurnErrorCode,
    TurnStage,
    StageOutcome,
    StageEvent,
    TurnExecutionError,
    DeadlineExceeded,
    create_budget,
    compute_backoff,
    run_blocking_write,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Default config validity
# ═══════════════════════════════════════════════════════════════════════════════

class TestDefaultConfig:
    def test_defaults_are_valid(self):
        config = TurnExecutionConfig.defaults()
        assert config.total_deadline == 45.0
        assert config.connect_timeout == 3.0
        assert config.provider_attempt_timeout == 15.0
        assert config.supabase_timeout == 5.0
        assert config.commit_reserve == 10.0
        assert config.max_attempts == 2
        assert config.base_backoff == 0.25
        assert config.max_backoff == 0.75
        assert config.max_jitter == 0.10
        assert config.frontend_timeout_ms == 50_000

    def test_defaults_pass_invariants(self):
        config = TurnExecutionConfig.defaults()
        assert config.connect_timeout <= config.provider_attempt_timeout
        assert config.provider_attempt_timeout < config.total_deadline
        assert config.supabase_timeout > 0
        assert config.commit_reserve >= 2 * config.supabase_timeout
        assert config.commit_reserve < config.total_deadline


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Strict environment parsing
# ═══════════════════════════════════════════════════════════════════════════════

class TestFromEnv:
    def test_parses_all_options(self):
        env = {
            "TURN_TOTAL_DEADLINE": "30.0",
            "TURN_CONNECT_TIMEOUT": "2.0",
            "TURN_PROVIDER_ATTEMPT_TIMEOUT": "10.0",
            "TURN_SUPABASE_TIMEOUT": "3.0",
            "TURN_COMMIT_RESERVE": "8.0",
            "TURN_MAX_ATTEMPTS": "3",
            "TURN_BASE_BACKOFF": "0.5",
            "TURN_MAX_BACKOFF": "1.0",
            "TURN_MAX_JITTER": "0.20",
            "TURN_FRONTEND_TIMEOUT_MS": "60000",
        }
        config = TurnExecutionConfig.from_env(env)
        assert config.total_deadline == 30.0
        assert config.connect_timeout == 2.0
        assert config.provider_attempt_timeout == 10.0
        assert config.supabase_timeout == 3.0
        assert config.commit_reserve == 8.0
        assert config.max_attempts == 3
        assert config.base_backoff == 0.5
        assert config.max_backoff == 1.0
        assert config.max_jitter == 0.20
        assert config.frontend_timeout_ms == 60_000

    def test_defaults_when_env_missing(self):
        config = TurnExecutionConfig.from_env({})
        assert config.total_deadline == 45.0
        assert config.max_attempts == 2

    # ── Rejection tests ──────────────────────────────────────────────────────

    @pytest.mark.parametrize("key,value,reason", [
        ("TURN_TOTAL_DEADLINE", "", "empty"),
        ("TURN_TOTAL_DEADLINE", "true", "bool string"),
        ("TURN_TOTAL_DEADLINE", "false", "bool string"),
        ("TURN_TOTAL_DEADLINE", "abc", "invalid text"),
        ("TURN_TOTAL_DEADLINE", "nan", "NaN"),
        ("TURN_TOTAL_DEADLINE", "inf", "infinity"),
        ("TURN_TOTAL_DEADLINE", "-inf", "negative infinity"),
        ("TURN_TOTAL_DEADLINE", "0", "zero"),
        ("TURN_TOTAL_DEADLINE", "-1", "negative"),
        ("TURN_CONNECT_TIMEOUT", "", "empty"),
        ("TURN_CONNECT_TIMEOUT", "true", "bool string"),
        ("TURN_CONNECT_TIMEOUT", "abc", "invalid text"),
        ("TURN_SUPABASE_TIMEOUT", "0", "zero"),
        ("TURN_SUPABASE_TIMEOUT", "-5", "negative"),
        ("TURN_MAX_ATTEMPTS", "", "empty"),
        ("TURN_MAX_ATTEMPTS", "true", "bool string"),
        ("TURN_MAX_ATTEMPTS", "abc", "invalid text"),
        ("TURN_MAX_ATTEMPTS", "0", "zero"),
        ("TURN_MAX_ATTEMPTS", "-1", "negative"),
        ("TURN_FRONTEND_TIMEOUT_MS", "0", "zero"),
        ("TURN_FRONTEND_TIMEOUT_MS", "999", "below 1000"),
        ("TURN_FRONTEND_TIMEOUT_MS", "true", "bool string"),
    ])
    def test_rejects_invalid_env(self, key, value, reason):
        env = {key: value}
        with pytest.raises((ValueError,)):
            TurnExecutionConfig.from_env(env)

    def test_rejects_incoherent_combination(self):
        # connect_timeout > provider_attempt_timeout
        env = {
            "TURN_CONNECT_TIMEOUT": "10.0",
            "TURN_PROVIDER_ATTEMPT_TIMEOUT": "5.0",
        }
        with pytest.raises(ValueError, match="connect_timeout must be <= provider_attempt_timeout"):
            TurnExecutionConfig.from_env(env)

    def test_rejects_provider_timeout_gte_total(self):
        env = {
            "TURN_PROVIDER_ATTEMPT_TIMEOUT": "50.0",
        }
        with pytest.raises(ValueError, match="provider_attempt_timeout must be < total_deadline"):
            TurnExecutionConfig.from_env(env)

    def test_rejects_commit_reserve_lt_2x_supabase(self):
        env = {
            "TURN_COMMIT_RESERVE": "5.0",
            "TURN_SUPABASE_TIMEOUT": "4.0",
        }
        with pytest.raises(ValueError, match="commit_reserve must be >= 2 \\* supabase_timeout"):
            TurnExecutionConfig.from_env(env)

    def test_rejects_commit_reserve_gte_total(self):
        env = {
            "TURN_COMMIT_RESERVE": "50.0",
        }
        with pytest.raises(ValueError, match="commit_reserve must be < total_deadline"):
            TurnExecutionConfig.from_env(env)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Budget creation and deadline
# ═══════════════════════════════════════════════════════════════════════════════

class TestBudget:
    def test_create_budget_with_injected_clock(self):
        fake_now = [1000.0]
        config = TurnExecutionConfig(total_deadline=45.0, commit_reserve=10.0)
        budget = create_budget(config, now_provider=lambda: fake_now[0])
        assert budget.deadline == 1045.0
        assert budget.reserve == 10.0

    def test_remaining_decreases(self):
        fake_now = [1000.0]
        config = TurnExecutionConfig(total_deadline=45.0, commit_reserve=10.0)
        budget = create_budget(config, now_provider=lambda: fake_now[0])
        assert budget.remaining == 45.0
        assert budget.remaining_before_reserve == 35.0

        fake_now[0] = 1010.0
        assert budget.remaining == 35.0
        assert budget.remaining_before_reserve == 25.0

    def test_remaining_capped_at_zero(self):
        fake_now = [1000.0]
        config = TurnExecutionConfig(total_deadline=45.0, commit_reserve=10.0)
        budget = create_budget(config, now_provider=lambda: fake_now[0])
        # Advance time past the deadline
        fake_now[0] = 1100.0
        assert budget.remaining == 0.0

    def test_has_reserve(self):
        fake_now = [1000.0]
        config = TurnExecutionConfig(total_deadline=45.0, commit_reserve=10.0)
        budget = create_budget(config, now_provider=lambda: fake_now[0])
        assert budget.has_reserve is True

        fake_now[0] = 1036.0  # 9s remaining, less than 10s reserve
        assert budget.has_reserve is False

    def test_assert_enough_for_passes(self):
        fake_now = [1000.0]
        config = TurnExecutionConfig(
            total_deadline=10.0,
            provider_attempt_timeout=5.0,
            commit_reserve=3.0,
            supabase_timeout=1.0,
        )
        budget = create_budget(config, now_provider=lambda: fake_now[0])
        budget.assert_enough_for("test", 5.0)  # should not raise

    def test_assert_enough_for_raises(self):
        fake_now = [1000.0]
        config = TurnExecutionConfig(
            total_deadline=10.0,
            provider_attempt_timeout=5.0,
            commit_reserve=3.0,
            supabase_timeout=1.0,
        )
        budget = create_budget(config, now_provider=lambda: fake_now[0])
        with pytest.raises(DeadlineExceeded):
            budget.assert_enough_for("test", 15.0)

    def test_remaining_for_attempt(self):
        fake_now = [1000.0]
        config = TurnExecutionConfig(
            total_deadline=45.0,
            provider_attempt_timeout=15.0,
            commit_reserve=10.0,
            supabase_timeout=5.0,
        )
        budget = create_budget(config, now_provider=lambda: fake_now[0])
        # 35s before reserve > 15s attempt timeout → returns 15.0
        assert budget.remaining_for_attempt(15.0) == 15.0

        fake_now[0] = 1040.0  # 5s remaining, 0 before reserve
        # remaining_before_reserve = max(0, 5 - 10) = 0
        assert budget.remaining_for_attempt(15.0) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Backoff computation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBackoff:
    def test_zero_attempt(self):
        delay = compute_backoff(0, base=0.25, cap=0.75, jitter_fraction=0.0, random_source=lambda: 0.0)
        assert delay == 0.25

    def test_attempt_1(self):
        delay = compute_backoff(1, base=0.25, cap=0.75, jitter_fraction=0.0, random_source=lambda: 0.0)
        assert delay == 0.5

    def test_attempt_capped(self):
        delay = compute_backoff(10, base=0.25, cap=0.75, jitter_fraction=0.0, random_source=lambda: 0.0)
        assert delay == 0.75

    def test_with_jitter(self):
        delay = compute_backoff(0, base=1.0, cap=10.0, jitter_fraction=0.5, random_source=lambda: 1.0)
        assert delay == 1.5  # 1.0 + (1.0 * 0.5 * 1.0)

    def test_negative_attempt_returns_zero(self):
        delay = compute_backoff(-1, base=0.25, cap=0.75, jitter_fraction=0.0, random_source=lambda: 0.0)
        assert delay == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Stage events
# ═══════════════════════════════════════════════════════════════════════════════

class TestStageEvent:
    def test_basic_event(self):
        event = StageEvent(stage=TurnStage.generation, outcome=StageOutcome.success, duration_ms=120.0)
        assert event.stage == TurnStage.generation
        assert event.outcome == StageOutcome.success
        assert event.duration_ms == 120.0

    def test_event_with_code(self):
        event = StageEvent(stage=TurnStage.appraisal, outcome=StageOutcome.failed, code=TurnErrorCode.provider_invalid_response)
        assert event.code == TurnErrorCode.provider_invalid_response

    def test_event_with_attempt(self):
        event = StageEvent(stage=TurnStage.generation, outcome=StageOutcome.timeout, attempt=1)
        assert event.attempt == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Domain errors
# ═══════════════════════════════════════════════════════════════════════════════

class TestDomainErrors:
    def test_turn_execution_error_has_code(self):
        err = TurnExecutionError(TurnErrorCode.provider_unavailable)
        assert err.code == TurnErrorCode.provider_unavailable
        assert "Turn execution failed" in str(err)

    def test_deadline_exceeded_is_subclass(self):
        err = DeadlineExceeded()
        assert isinstance(err, TurnExecutionError)
        assert err.code == TurnErrorCode.turn_timeout


# ═══════════════════════════════════════════════════════════════════════════════
# 7. run_blocking_write() unit test
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunBlockingWrite:
    """Direct unit tests for run_blocking_write safe-by-contract behavior."""

    async def _test_normal_completion(self):
        budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)

        def sync_func(x: int) -> int:
            return x * 2

        result = await run_blocking_write(
            "test", budget, 5.0, sync_func, 21,
        )
        assert result == 42

    def test_normal_completion(self):
        """run_blocking_write returns normally when not cancelled."""
        asyncio.run(self._test_normal_completion())

    async def _test_cancel_budget_exhausted(self):
        budget = TurnBudget(deadline=0.0, reserve=0.0, now_provider=lambda: 100.0)

        def never_called():
            return 42

        with pytest.raises(DeadlineExceeded):
            await run_blocking_write("test", budget, 5.0, never_called)

    def test_cancel_budget_exhausted(self):
        """DeadlineExceeded raised when budget is already exhausted."""
        asyncio.run(self._test_cancel_budget_exhausted())

    async def _test_single_cancel_drains_worker(self):
        """A single CancelledError during the write is drained and propagated."""
        import threading
        started = threading.Event()
        can_finish = threading.Event()

        def sync_worker():
            started.set()
            assert can_finish.wait(timeout=10.0), "timeout waiting for release"
            return 42

        budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)

        task = asyncio.create_task(
            run_blocking_write("test", budget, 5.0, sync_worker)
        )

        # Wait for worker to start in thread
        started_ok = await asyncio.to_thread(started.wait, 5.0)
        assert started_ok, "sync_worker did not start"
        await asyncio.sleep(0.05)

        # Cancel the task
        task.cancel()
        await asyncio.sleep(0.05)

        # Worker should still be running (draining)
        can_finish.set()

        # Task should raise CancelledError after worker completes
        with pytest.raises(asyncio.CancelledError):
            await task

        # No "Task exception was never retrieved" will occur

    def test_single_cancel_drains_worker(self):
        """Single cancel drains worker then propagates CancelledError."""
        asyncio.run(self._test_single_cancel_drains_worker())

    async def _test_double_cancel_drains_worker(self):
        """Two CancelledErrors during the write: both consumed, worker drains."""
        import threading
        started = threading.Event()
        can_finish = threading.Event()
        completed = threading.Event()

        def sync_worker():
            started.set()
            assert can_finish.wait(timeout=10.0), "timeout waiting for release"
            completed.set()
            return 42

        budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)

        task = asyncio.create_task(
            run_blocking_write("test", budget, 5.0, sync_worker)
        )

        started_ok = await asyncio.to_thread(started.wait, 5.0)
        assert started_ok, "sync_worker did not start"
        await asyncio.sleep(0.05)

        # Cancel twice
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)

        # Worker should still be running
        assert not completed.is_set(), "worker completed before release"

        # Release worker
        can_finish.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Worker completed
        assert completed.is_set(), "worker did not complete"

    def test_double_cancel_drains_worker(self):
        """Two cancels during write are consumed, worker drains to completion."""
        asyncio.run(self._test_double_cancel_drains_worker())

    async def _test_worker_exception_converted(self):
        """Worker exception (not allowlisted) converts to persistence_unavailable."""
        budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)

        def failing_worker():
            raise ValueError("something bad")

        with pytest.raises(TurnExecutionError) as exc_info:
            await run_blocking_write("test", budget, 5.0, failing_worker)
        assert exc_info.value.code == TurnErrorCode.persistence_unavailable

    def test_worker_exception_converted(self):
        """Non-allowlisted worker exception → persistence_unavailable."""
        asyncio.run(self._test_worker_exception_converted())

    async def _test_worker_allowlisted_exception(self):
        """Allowlisted exception propagates as-is."""
        class MyAllowlistedError(Exception):
            pass

        budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)

        def failing_worker():
            raise MyAllowlistedError("expected")

        with pytest.raises(MyAllowlistedError):
            await run_blocking_write(
                "test", budget, 5.0, failing_worker,
                allowlist_exceptions=(MyAllowlistedError,),
            )

    def test_worker_allowlisted_exception(self):
        asyncio.run(self._test_worker_allowlisted_exception())

    async def _test_cancel_with_allowlisted_exception(self):
        """Cancellation takes priority even when worker raises allowlisted error."""
        import threading
        started = threading.Event()
        can_finish = threading.Event()

        class MyAllowlistedError(Exception):
            pass

        def sync_worker():
            started.set()
            assert can_finish.wait(timeout=10.0), "timeout"
            raise MyAllowlistedError("expected")

        budget = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)

        task = asyncio.create_task(
            run_blocking_write(
                "test", budget, 5.0, sync_worker,
                allowlist_exceptions=(MyAllowlistedError,),
            )
        )

        started_ok = await asyncio.to_thread(started.wait, 5.0)
        assert started_ok
        await asyncio.sleep(0.05)

        task.cancel()
        await asyncio.sleep(0.05)
        can_finish.set()

        # Cancellation should propagate, not the allowlisted error
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_cancel_with_allowlisted_exception(self):
        asyncio.run(self._test_cancel_with_allowlisted_exception())
