"""Operational data retention runner (#316).

Application layer between the explicit operational command
(``backend.retention_cli``) and the SQL purge boundary
(``supabase/migrations/20260809030000_operational_data_retention.sql``).
No FastAPI routing, no per-user state, no scheduler, no ``BackgroundTasks``.

Components
==========

* ``RetentionConfig`` — explicit, validated run parameters (batch size,
  per-category row cap, round timeout).
* ``RetentionRunResult`` / ``CategoryResult`` — sanitized public outcome of
  a round: schema version, per-category purged counts and batch counts.
  Never contains identifiers, HMACs, content or SQL.
* ``RetentionRepository`` — injectable protocol; the sync
  ``SupabaseRetentionRepository`` adapter runs the three purge RPCs
  fail-closed (shape-validated, sanitized ``PersistenceError``).
* ``RetentionRunner`` — stateless, injectable runner. ``run_once()``
  processes each category in bounded batches until a partial batch or the
  per-category cap, then terminates. No hidden loop beyond the round.

Writes
======
Every purge RPC is dispatched through ``run_blocking_write``: the real
timeout comes from the PostgREST transport configuration, cancellation
drains the in-flight write (no abandoned writes, no orphaned tasks), and a
per-round monotonic budget bounds the whole category.

Concurrency
===========
Purge is idempotent and safe under concurrent executions by construction:
each statement deletes a bounded, primary-key-selected set; a concurrent
runner that selects overlapping rows simply finds them already gone. No
global advisory lock is used.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from .atomic_turn_commit import PersistenceError
from .observability import (
    EVENT_RETENTION_COMPLETED,
    EVENT_RETENTION_FAILED,
    emit_event,
)
from .retention_policy import (
    RETENTION_MAX_BATCH_SIZE,
    RetentionCategory,
    RetentionPolicy,
    default_retention_policy,
    retention_cutoffs,
)
from .turn_execution import TurnBudget, TurnExecutionConfig, run_blocking_write

logger = logging.getLogger(__name__)

#: Sanitized stage label used by the write helper for every purge call.
_RETENTION_WRITE_STAGE = "retention_purge"

#: Default batch size (rows per purge statement).
DEFAULT_RETENTION_BATCH_SIZE = 500

#: Default maximum rows purged per category per round (bounds one run).
DEFAULT_RETENTION_MAX_ROWS_PER_CATEGORY = 10_000

#: Default per-category monotonic round timeout (seconds).
DEFAULT_RETENTION_ROUND_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class RetentionConfig:
    """Explicit, validated parameters for one retention round."""

    batch_size: int = DEFAULT_RETENTION_BATCH_SIZE
    max_rows_per_category: int = DEFAULT_RETENTION_MAX_ROWS_PER_CATEGORY
    round_timeout_seconds: float = DEFAULT_RETENTION_ROUND_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.batch_size, bool) or not isinstance(self.batch_size, int):
            raise ValueError("retention batch_size must be an int")
        if not 1 <= self.batch_size <= RETENTION_MAX_BATCH_SIZE:
            raise ValueError(
                f"retention batch_size must be between 1 and {RETENTION_MAX_BATCH_SIZE}"
            )
        if isinstance(self.max_rows_per_category, bool) or not isinstance(
            self.max_rows_per_category, int
        ):
            raise ValueError("retention max_rows_per_category must be an int")
        if self.max_rows_per_category < self.batch_size:
            raise ValueError(
                "retention max_rows_per_category must be >= batch_size"
            )
        if (
            isinstance(self.round_timeout_seconds, bool)
            or not isinstance(self.round_timeout_seconds, (int, float))
            or not self.round_timeout_seconds > 0
        ):
            raise ValueError(
                "retention round_timeout_seconds must be a positive number"
            )


@dataclass(frozen=True)
class CategoryResult:
    """Sanitized outcome of one category in a round."""

    category: str
    purged: int = 0
    batches: int = 0


@dataclass(frozen=True)
class RetentionRunResult:
    """Sanitized public outcome of one retention round.

    Contains only the policy schema version, per-category aggregate counts
    and the total. Never contains identifiers, HMACs, content, SQL or
    secrets.
    """

    schema_version: int
    results: Mapping[str, CategoryResult]
    total_purged: int

    @classmethod
    def build(
        cls,
        schema_version: int,
        results: list[CategoryResult],
    ) -> "RetentionRunResult":
        return cls(
            schema_version=schema_version,
            results={result.category: result for result in results},
            total_purged=sum(result.purged for result in results),
        )


class RetentionRepository(Protocol):
    """Synchronous purge RPC contract (thread-bound; never awaited directly)."""

    def purge_admission_reservations(self, cutoff: str, batch_size: int) -> int:
        """Delete up to *batch_size* expired admission rows; return deleted."""
        ...

    def purge_privacy_operations(self, cutoff: str, batch_size: int) -> int:
        """Delete up to *batch_size* expired privacy ledger rows; return deleted."""
        ...

    def purge_outbox_events(self, cutoff: str, batch_size: int) -> int:
        """Delete up to *batch_size* eligible final outbox events; return deleted."""
        ...


class SupabaseRetentionRepository:
    """Synchronous adapter over ``client.rpc(name, params).execute()``.

    The response shape is validated fail-closed INSIDE the try/except: a
    missing/unreachable client, a malformed payload, a non-scalar count or a
    negative count all surface as a sanitized ``PersistenceError``. Upstream
    exceptions (which may carry connection details, identifiers or payload
    content) are never surfaced.
    """

    _RPC_NAMES = {
        RetentionCategory.ADMISSION_RESERVATIONS.value: "purge_admission_reservations",
        RetentionCategory.PRIVACY_OPERATIONS.value: "purge_privacy_operations",
        RetentionCategory.OUTBOX_EVENTS.value: "purge_outbox_events",
    }

    def __init__(self, client: Any) -> None:
        self._client = client

    def _call(self, rpc_name: str, params: Mapping[str, Any]) -> int:
        if self._client is None:
            raise PersistenceError("database_error", "persistence error")
        try:
            response = self._client.rpc(rpc_name, params).execute()
            data = getattr(response, "data", None)
            if isinstance(data, list):
                if len(data) != 1:
                    raise PersistenceError("database_error", "persistence error")
                data = data[0]
            if isinstance(data, int) and not isinstance(data, bool):
                # PostgREST returns a bare integer for scalar-returning RPCs.
                if data < 0:
                    raise PersistenceError("database_error", "persistence error")
                return data
            if not isinstance(data, dict):
                raise PersistenceError("database_error", "persistence error")
            count = data.get(rpc_name)
            if count is None:
                # Tolerate alternative single-value shapes, still fail
                # closed on anything ambiguous.
                ints = [
                    value
                    for value in data.values()
                    if isinstance(value, int) and not isinstance(value, bool)
                ]
                if len(ints) != 1:
                    raise PersistenceError("database_error", "persistence error")
                count = ints[0]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise PersistenceError("database_error", "persistence error")
            return count
        except PersistenceError:
            raise
        except Exception:
            raise PersistenceError("database_error", "persistence error") from None

    def purge_admission_reservations(self, cutoff: str, batch_size: int) -> int:
        return self._call(self._RPC_NAMES[RetentionCategory.ADMISSION_RESERVATIONS.value], {
            "p_cutoff": cutoff,
            "p_batch_size": batch_size,
        })

    def purge_privacy_operations(self, cutoff: str, batch_size: int) -> int:
        return self._call(self._RPC_NAMES[RetentionCategory.PRIVACY_OPERATIONS.value], {
            "p_cutoff": cutoff,
            "p_batch_size": batch_size,
        })

    def purge_outbox_events(self, cutoff: str, batch_size: int) -> int:
        return self._call(self._RPC_NAMES[RetentionCategory.OUTBOX_EVENTS.value], {
            "p_cutoff": cutoff,
            "p_batch_size": batch_size,
        })


class RetentionRunner:
    """Stateless runner for one operational retention round.

    The runner holds no per-user state: the clock and the repository are
    injected, the policy is versioned, and each round is fully described by
    ``run_once()``. A round processes each covered category in batches of
    ``config.batch_size`` until a batch purges fewer rows than the batch
    size OR ``config.max_rows_per_category`` rows have been purged, then
    terminates.
    """

    def __init__(
        self,
        *,
        repository: RetentionRepository,
        policy: Optional[RetentionPolicy] = None,
        config: Optional[RetentionConfig] = None,
        clock: Callable[[], float] = time.time,
        turn_config: Optional[TurnExecutionConfig] = None,
    ) -> None:
        self._repository = repository
        self._policy = policy if policy is not None else default_retention_policy()
        self._config = config if config is not None else RetentionConfig()
        self._clock = clock if clock is not None else time.time
        self._turn_config = (
            turn_config if turn_config is not None else TurnExecutionConfig.defaults()
        )

    async def run_once(self) -> RetentionRunResult:
        """Run one full retention round and return the sanitized result."""
        cutoffs = retention_cutoffs(self._clock(), self._policy)
        category_results: list[CategoryResult] = [
            await self._purge_category(
                RetentionCategory.ADMISSION_RESERVATIONS,
                cutoffs[RetentionCategory.ADMISSION_RESERVATIONS.value],
                self._repository.purge_admission_reservations,
            ),
            await self._purge_category(
                RetentionCategory.PRIVACY_OPERATIONS,
                cutoffs[RetentionCategory.PRIVACY_OPERATIONS.value],
                self._repository.purge_privacy_operations,
            ),
            await self._purge_category(
                RetentionCategory.OUTBOX_EVENTS,
                cutoffs[RetentionCategory.OUTBOX_EVENTS.value],
                self._repository.purge_outbox_events,
            ),
        ]
        return RetentionRunResult.build(
            self._policy.schema_version, category_results
        )

    async def _purge_category(
        self,
        category: RetentionCategory,
        cutoff: str,
        purge_fn: Callable[[str, int], int],
    ) -> CategoryResult:
        """Purge one category in bounded batches.

        The write helper enforces the PostgREST transport timeout and drains
        the in-flight write on cancellation; a per-category monotonic budget
        bounds the round. Failures propagate as sanitized exceptions
        (``PersistenceError`` / ``TurnExecutionError``); the caller maps
        them to a sanitized failure event.
        """
        budget = TurnBudget(
            deadline=time.monotonic() + self._config.round_timeout_seconds,
            reserve=0.0,
            now_provider=time.monotonic,
        )
        total = 0
        batches = 0
        while total < self._config.max_rows_per_category:
            started_at = time.monotonic()
            try:
                count = await run_blocking_write(
                    _RETENTION_WRITE_STAGE,
                    budget,
                    self._turn_config.supabase_timeout,
                    purge_fn,
                    cutoff,
                    self._config.batch_size,
                    allowlist_exceptions=(PersistenceError,),
                )
            except PersistenceError:
                emit_event(
                    logger,
                    EVENT_RETENTION_FAILED,
                    level=logging.ERROR,
                    phase=category.value,
                    code="persistence_error",
                )
                raise
            except Exception:
                emit_event(
                    logger,
                    EVENT_RETENTION_FAILED,
                    level=logging.ERROR,
                    phase=category.value,
                    code="purge_failed",
                )
                raise
            batches += 1
            total += count
            emit_event(
                logger,
                EVENT_RETENTION_COMPLETED,
                phase=category.value,
                result=count,
                duration_ms=(time.monotonic() - started_at) * 1000,
            )
            if count < self._config.batch_size:
                break
        return CategoryResult(
            category=category.value,
            purged=total,
            batches=batches,
        )
