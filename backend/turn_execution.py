"""
Turn execution domain — pure, infrastructure-free bounded execution primitives.

This module defines the typed configuration, monotonic deadline tracking, failure
codes, and domain exceptions for bounded turn execution. It has no dependency on
FastAPI, Groq, Supabase, sentence_transformers, or network I/O.

The only standard library imports allowed are ``dataclasses``, ``enum``,
``math``, ``time``, and ``typing``.
"""

from __future__ import annotations

import asyncio
import math
import random as _random
import time as _real_time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, TypeVar, Any


T = TypeVar("T")


# ─── Failure codes ───────────────────────────────────────────────────────────

class TurnErrorCode(str, Enum):
    """Stable, sanitised internal failure codes.

    These codes are safe to log and expose via HTTP ``detail.code``. They
    contain no model names, provider details, exception text, secrets, or
    user content.
    """
    turn_timeout = "turn_timeout"
    upstream_rate_limited = "upstream_rate_limited"
    provider_unavailable = "provider_unavailable"
    provider_invalid_request = "provider_invalid_request"
    provider_invalid_response = "provider_invalid_response"
    persistence_unavailable = "persistence_unavailable"
    internal_error = "internal_error"


# ─── Domain exceptions ───────────────────────────────────────────────────────

class TurnExecutionError(Exception):
    """Sanitised domain exception for turn execution failures.

    ``code`` carries a ``TurnErrorCode``. The ``message`` is a generic string
    that never contains raw exception text, secrets, or user content.
    """
    def __init__(self, code: TurnErrorCode, message: str = "Turn execution failed.") -> None:
        self.code = code
        super().__init__(message)


class DeadlineExceeded(TurnExecutionError):
    """Raised when the turn deadline is exceeded before completion."""

    def __init__(self) -> None:
        super().__init__(TurnErrorCode.turn_timeout, "Turn deadline exceeded.")


# ─── Groq call parameters ────────────────────────────────────────────────────

@dataclass(frozen=True)
class GroqCallParams:
    """Explicit typed parameters for provider calls.

    These are derived from ``TurnExecutionConfig`` and passed explicitly
    to ``GroqClientManager.chat_completion_async()``. No kwargs or
    dict-based forwarding.
    """
    max_attempts: int = 2
    connect_timeout: float = 3.0
    provider_attempt_timeout: float = 15.0
    base_backoff: float = 0.25
    max_backoff: float = 0.75
    max_jitter: float = 0.10


def compute_effective_attempt_timeout(
    config: GroqCallParams,
    budget: TurnBudget,
) -> float:
    """Compute the effective timeout for a provider attempt.

    Returns the minimum of:
    * configured attempt timeout
    * remaining budget before commit reserve
    """
    return min(config.provider_attempt_timeout, budget.remaining_before_reserve)


# ─── Bounded blocking operation helper ───────────────────────────────────────


async def run_blocking_with_deadline(
    stage_label: str,
    budget: TurnBudget,
    supabase_timeout: float,
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run a blocking (thread-bound) operation with deadline enforcement.

    The operation is dispatched via ``asyncio.to_thread()`` and the coroutine
    waits for it with a timeout derived from the remaining budget. This
    ensures that no thread operation runs indefinitely.

    The effective timeout is ``min(budget.remaining, supabase_timeout)``.
    We use ``supabase_timeout`` as an upper bound because the underlying
    Supabase/PostgREST transport already respects that timeout, and waiting
    longer than that would be wasteful.

    On **timeout**: raises ``DeadlineExceeded`` (``TurnErrorCode.turn_timeout``).
    On **cancellation**: propagates ``asyncio.CancelledError``.
    On **any other exception**: wraps into ``TurnExecutionError(persistence_unavailable)``.

    The underlying thread may continue executing after a timeout. This is
    acceptable for read operations (``load_user_state``, ``get_context``)
    because there is no state to corrupt. For write operations (``save_turn``,
    ``sync_state``), callers must ensure the lock is retained until the write
    is known to have completed (see commit section in engine.py).

    Args:
        stage_label: Stage name for observability (e.g. ``"load_user_state"``).
        budget: ``TurnBudget`` from ``turn_execution`` — provides remaining time.
        supabase_timeout: Per-call timeout for Supabase operations.
        func: Synchronous callable to invoke in a thread.
        *args: Positional arguments forwarded to *func*.
        **kwargs: Keyword arguments forwarded to *func*.

    Returns:
        The return value of *func*.

    Raises:
        DeadlineExceeded: When the operation times out.
        TurnExecutionError(persistence_unavailable): On operation failure.
        asyncio.CancelledError: When the coroutine is cancelled.
    """
    remaining = budget.remaining
    timeout = min(remaining, supabase_timeout)

    if timeout <= 0.0:
        raise DeadlineExceeded()

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=timeout,
        )
        return result
    except asyncio.TimeoutError:
        raise DeadlineExceeded()
    except asyncio.CancelledError:
        raise
    except TurnExecutionError:
        raise
    except Exception:
        raise TurnExecutionError(
            TurnErrorCode.persistence_unavailable,
            f"Stage {stage_label} failed.",
        )


# ─── Turn stage enum for observability ──────────────────────────────────────

class TurnStage(str, Enum):
    load_state = "load_state"
    load_context = "load_context"
    appraisal = "appraisal"
    transition = "transition"
    generation = "generation"
    commit = "commit"


class StageOutcome(str, Enum):
    success = "success"
    timeout = "timeout"
    cancelled = "cancelled"
    failed = "failed"


@dataclass(frozen=True)
class StageEvent:
    """Low-cardinality structured observation for a completed stage.

    Examples::

        StageEvent(stage=TurnStage.appraisal, outcome=StageOutcome.success, duration_ms=120.0)
        StageEvent(stage=TurnStage.generation, outcome=StageOutcome.timeout, attempt=1)
    """
    stage: TurnStage
    outcome: StageOutcome
    code: Optional[TurnErrorCode] = None
    duration_ms: Optional[float] = None
    attempt: Optional[int] = None


# ─── Monotonic deadline / budget ─────────────────────────────────────────────

@dataclass(frozen=True)
class TurnBudget:
    """Remaining time budget derived from a monotonic deadline.

    All times are in seconds, sourced from ``time.monotonic`` (or an injected
    clock for testing).
    """
    deadline: float       # absolute monotonic deadline
    reserve: float        # reserved time for commit section
    now_provider: Callable[[], float] = field(repr=False)

    @property
    def remaining(self) -> float:
        """Seconds until deadline (capped at 0.0)."""
        return max(0.0, self.deadline - self.now_provider())

    @property
    def remaining_before_reserve(self) -> float:
        """Seconds before the commit reserve must be preserved."""
        return max(0.0, self.remaining - self.reserve)

    @property
    def has_reserve(self) -> bool:
        """Whether the full commit reserve is still available."""
        return self.remaining >= self.reserve

    def assert_enough_for(self, label: str, needed: float) -> None:
        """Raise ``TurnExecutionError(turn_timeout)`` if *needed* exceeds remaining."""
        remaining = self.remaining
        if needed > remaining:
            raise DeadlineExceeded()

    def remaining_for_attempt(self, attempt_timeout: float) -> float:
        """Return the effective timeout for a provider attempt.

        Returns the minimum of *attempt_timeout* and the budget available
        before the commit reserve must be preserved.
        """
        return min(attempt_timeout, self.remaining_before_reserve)


# ─── Turn execution environment config ───────────────────────────────────────

@dataclass(frozen=True)
class TurnExecutionConfig:
    """Immutable, validated configuration for bounded turn execution.

    Defaults can be overridden via environment variables. The parser
    (``from_env``) fails closed for all invalid inputs.

    Defaults
    ========
    total_deadline: ``45.0`` seconds — total monotonic deadline for a full turn.
    connect_timeout: ``3.0`` seconds — connection timeout for provider calls.
    provider_attempt_timeout: ``15.0`` seconds — max duration of one provider attempt.
    supabase_timeout: ``5.0`` seconds — per-call timeout for Supabase/PostgREST ops.
    commit_reserve: ``10.0`` seconds — reserved time for the commit section.
    max_attempts: ``2`` — max provider retry attempts per logical call.
    base_backoff: ``0.25`` seconds — exponential backoff base.
    max_backoff: ``0.75`` seconds — backoff cap.
    max_jitter: ``0.10`` (10%) — jitter as fraction of backoff.
    frontend_timeout_ms: ``50_000`` milliseconds — suggested frontend AbortController timeout.

    Invariants enforced on construction (and on every ``from_env`` parse):
    * ``connect_timeout <= provider_attempt_timeout``
    * ``provider_attempt_timeout < total_deadline``
    * ``supabase_timeout > 0``
    * ``commit_reserve >= 2 * supabase_timeout``
    * ``commit_reserve < total_deadline``
    * ``max_attempts`` is int (not bool), >= 1
    * backoff and jitter never exceed remaining budget (validated at runtime)
    """
    total_deadline: float = 45.0
    connect_timeout: float = 3.0
    provider_attempt_timeout: float = 15.0
    supabase_timeout: float = 5.0
    commit_reserve: float = 10.0
    max_attempts: int = 2
    base_backoff: float = 0.25
    max_backoff: float = 0.75
    max_jitter: float = 0.10
    frontend_timeout_ms: int = 50_000

    def __post_init__(self) -> None:
        """Validate all invariants."""
        self._assert_finite_positive("total_deadline", self.total_deadline)
        self._assert_finite_positive("connect_timeout", self.connect_timeout)
        self._assert_finite_positive("provider_attempt_timeout", self.provider_attempt_timeout)
        self._assert_finite_positive("supabase_timeout", self.supabase_timeout)
        self._assert_finite_positive("commit_reserve", self.commit_reserve)
        self._assert_finite_positive("base_backoff", self.base_backoff)
        self._assert_finite_positive("max_backoff", self.max_backoff)
        self._assert_finite_nonnegative("max_jitter", self.max_jitter)

        # max_attempts must be int, not bool
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an int, not bool.")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1.")
        if self.max_attempts > 5:
            raise ValueError("max_attempts must be <= 5 (safety limit).")

        # frontend_timeout_ms
        if isinstance(self.frontend_timeout_ms, bool) or not isinstance(self.frontend_timeout_ms, int):
            raise ValueError("frontend_timeout_ms must be an int.")
        if self.frontend_timeout_ms < 1000:
            raise ValueError("frontend_timeout_ms must be >= 1000.")
        if self.frontend_timeout_ms > 300_000:
            raise ValueError("frontend_timeout_ms must be <= 300_000 (safety limit).")

        # Invariant: connect_timeout <= provider_attempt_timeout
        if self.connect_timeout > self.provider_attempt_timeout:
            raise ValueError(
                "connect_timeout must be <= provider_attempt_timeout."
            )

        # Invariant: provider_attempt_timeout < total_deadline
        if self.provider_attempt_timeout >= self.total_deadline:
            raise ValueError(
                "provider_attempt_timeout must be < total_deadline."
            )

        # Invariant: commit_reserve >= 2 * supabase_timeout
        if self.commit_reserve < 2 * self.supabase_timeout:
            raise ValueError(
                "commit_reserve must be >= 2 * supabase_timeout."
            )

        # Invariant: commit_reserve < total_deadline
        if self.commit_reserve >= self.total_deadline:
            raise ValueError("commit_reserve must be < total_deadline.")

        # Invariant: base_backoff <= max_backoff
        if self.base_backoff > self.max_backoff:
            raise ValueError("base_backoff must be <= max_backoff.")

        # Invariant: max_jitter in [0, 1]
        if not (0.0 <= self.max_jitter <= 1.0):
            raise ValueError("max_jitter must be in [0.0, 1.0].")

        # Sanity limits on timeouts
        if self.total_deadline > 300.0:
            raise ValueError("total_deadline must be <= 300 seconds (safety limit).")
        if self.connect_timeout > 30.0:
            raise ValueError("connect_timeout must be <= 30 seconds (safety limit).")
        if self.provider_attempt_timeout > 120.0:
            raise ValueError("provider_attempt_timeout must be <= 120 seconds (safety limit).")
        if self.supabase_timeout > 30.0:
            raise ValueError("supabase_timeout must be <= 30 seconds (safety limit).")
        if self.commit_reserve > 120.0:
            raise ValueError("commit_reserve must be <= 120 seconds (safety limit).")

    @staticmethod
    def _assert_finite_positive(name: str, value: object) -> None:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite positive number, got bool.")
        if not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite positive number, got {type(value).__name__}.")
        f = float(value)
        if not math.isfinite(f):
            raise ValueError(f"{name} must be finite, got {f}.")
        if f <= 0:
            raise ValueError(f"{name} must be positive, got {f}.")

    @staticmethod
    def _assert_finite_nonnegative(name: str, value: object) -> None:
        """Like _assert_finite_positive but allows zero (for jitter)."""
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite non-negative number, got bool.")
        if not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite non-negative number, got {type(value).__name__}.")
        f = float(value)
        if not math.isfinite(f):
            raise ValueError(f"{name} must be finite, got {f}.")
        if f < 0:
            raise ValueError(f"{name} must be non-negative, got {f}.")

    def to_groq_params(self) -> GroqCallParams:
        """Derive ``GroqCallParams`` from this config."""
        return GroqCallParams(
            max_attempts=self.max_attempts,
            connect_timeout=self.connect_timeout,
            provider_attempt_timeout=self.provider_attempt_timeout,
            base_backoff=self.base_backoff,
            max_backoff=self.max_backoff,
            max_jitter=self.max_jitter,
        )

    @classmethod
    def defaults(cls) -> TurnExecutionConfig:
        """Factory returning the default configuration."""
        return cls()

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> TurnExecutionConfig:
        """Parse configuration from an environment dict (defaults to ``os.environ``).

        Fails closed for:
        * Present but empty values
        * Boolean
        * Invalid text
        * NaN or infinity
        * Zero or negative values
        * Integer out of valid range
        * Incoherent combinations (validated by __post_init__)
        """
        import os as _os
        source = env if env is not None else _os.environ

        kwargs: dict = {}

        # Helper: parse float from env or return default
        def _parse_float(key: str, default: float) -> float:
            val = source.get(key)
            if val is None:
                return default
            val = val.strip()
            if not val:
                raise ValueError(f"Environment variable {key!r} is empty.")
            if val.lower() in ("true", "false", "yes", "no"):
                raise ValueError(f"Environment variable {key!r} cannot be a boolean string ({val!r}).")
            try:
                f = float(val)
            except (ValueError, TypeError):
                raise ValueError(f"Environment variable {key!r} has invalid value {val!r}.")
            if not math.isfinite(f):
                raise ValueError(f"Environment variable {key!r} must be finite, got {f!r}.")
            if f <= 0:
                raise ValueError(f"Environment variable {key!r} must be positive, got {f!r}.")
            return f

        def _parse_int(key: str, default: int) -> int:
            val = source.get(key)
            if val is None:
                return default
            val = val.strip()
            if not val:
                raise ValueError(f"Environment variable {key!r} is empty.")
            if val.lower() in ("true", "false", "yes", "no"):
                raise ValueError(f"Environment variable {key!r} cannot be a boolean string ({val!r}).")
            try:
                i = int(val)
            except (ValueError, TypeError):
                raise ValueError(f"Environment variable {key!r} has invalid value {val!r}.")
            if i <= 0:
                raise ValueError(f"Environment variable {key!r} must be positive, got {i!r}.")
            return i

        kwargs["total_deadline"] = _parse_float("TURN_TOTAL_DEADLINE", 45.0)
        kwargs["connect_timeout"] = _parse_float("TURN_CONNECT_TIMEOUT", 3.0)
        kwargs["provider_attempt_timeout"] = _parse_float("TURN_PROVIDER_ATTEMPT_TIMEOUT", 15.0)
        kwargs["supabase_timeout"] = _parse_float("TURN_SUPABASE_TIMEOUT", 5.0)
        kwargs["commit_reserve"] = _parse_float("TURN_COMMIT_RESERVE", 10.0)
        kwargs["base_backoff"] = _parse_float("TURN_BASE_BACKOFF", 0.25)
        kwargs["max_backoff"] = _parse_float("TURN_MAX_BACKOFF", 0.75)
        kwargs["max_jitter"] = _parse_float("TURN_MAX_JITTER", 0.10)
        kwargs["max_attempts"] = _parse_int("TURN_MAX_ATTEMPTS", 2)
        kwargs["frontend_timeout_ms"] = _parse_int("TURN_FRONTEND_TIMEOUT_MS", 50_000)

        return cls(**kwargs)


# ─── Deadline / budget factory ───────────────────────────────────────────────

def create_budget(
    config: TurnExecutionConfig,
    now_provider: Callable[[], float] = _real_time.monotonic,
) -> TurnBudget:
    """Create a ``TurnBudget`` from config starting at *now_provider*()."""
    return TurnBudget(
        deadline=now_provider() + config.total_deadline,
        reserve=config.commit_reserve,
        now_provider=now_provider,
    )


def compute_backoff(
    attempt: int,
    base: float = 0.25,
    cap: float = 0.75,
    jitter_fraction: float = 0.10,
    random_source: Callable[[], float] = _random.random,
) -> float:
    """Compute exponential backoff with jitter for a given attempt number.

    ``attempt`` is 0-indexed (first retry is attempt 0).
    Returns a value in [0, cap].

    The total (delay + jitter) is capped at ``cap``, so jitter never pushes
    the actual delay past the configured maximum.

    Values are validated: base > 0, cap > 0, base <= cap, jitter_fraction in [0,1].
    """
    if attempt < 0:
        return 0.0
    delay = base * (2 ** attempt)
    delay = min(delay, cap)
    jitter = delay * jitter_fraction * random_source()
    return min(delay + jitter, cap)


def compute_backoff_from_params(attempt: int, params: GroqCallParams, random_source: Callable[[], float] = _random.random) -> float:
    """Compute backoff from a ``GroqCallParams`` object."""
    return compute_backoff(
        attempt,
        base=params.base_backoff,
        cap=params.max_backoff,
        jitter_fraction=params.max_jitter,
        random_source=random_source,
    )
