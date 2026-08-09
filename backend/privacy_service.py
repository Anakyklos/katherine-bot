"""Application layer exposing the #314 privacy primitives (#315).

This module is the thin, stateless application boundary between the
authenticated HTTP layer (``backend.main``) and the #314 Python frontier
(``backend.privacy_operations``). It deliberately contains no FastAPI
routing, no per-user state, and no re-implementation of the #314 semantics
(ledger, locks, fingerprint, revision, neutrality, replay): every operation
delegates to :func:`backend.privacy_operations.run_privacy_operation`, the
single source of truth for the privacy frontier.

Components
==========

* ``PrivacyOperationResponse`` — the explicit public projection of a
  :class:`backend.privacy_operations.PrivacyOperationResult`. It exposes
  ONLY ``operation``, ``status`` and the aggregate safe counts. It never
  exposes ``user_id``, ``revision``, ``operation_id``, message/memory IDs,
  content, snapshots, HMACs or any internal detail. Fresh executions and
  idempotent replays produce the same projection: there is deliberately no
  ``replayed`` field, because the persistent #314 contract does not provide
  that semantic.
* ``SupabasePrivacyRepository`` — synchronous adapter for
  ``client.rpc(name, params).execute()`` with fail-closed response shape
  validation. It must never be awaited directly on the event loop.
* ``PrivacyService`` — a stateless, process-wide application service. The
  authenticated identity and the operation_id are per-call arguments; no
  snapshot or identifier is retained between requests. It uses an injected
  clock for reset snapshot timestamps, creates a per-action budget from the
  operational turn configuration, dispatches the synchronous repository
  call through ``run_blocking_write`` (preserving the PostgREST transport
  timeout, the operational budget, and correct drain-on-cancellation), and
  delegates the operation to ``run_privacy_operation``.

Ownership and testability
=========================

* The service is stateless and may live in the application container.
  Identity, operation_id and snapshots are always per-call arguments.
* The repository is injectable (a fake can record or stub RPC calls), the
  clock is injectable (reset timestamps are deterministic in tests), and
  the operational ``TurnExecutionConfig`` is injectable (budget/timeout
  behavior is configurable).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Optional, Protocol

from pydantic import BaseModel

from backend.atomic_turn_commit import PersistenceError
from backend.privacy_operations import (
    OPERATION_DELETE_HISTORY,
    OPERATION_DELETE_MEMORIES,
    OPERATION_RESET_EMOTIONAL_STATE,
    OPERATION_RESET_RELATIONSHIP_STATE,
    PrivacyOperationResult,
    neutral_emotional_snapshot,
    neutral_relationship_snapshot,
    run_privacy_operation,
)
from backend.turn_execution import TurnExecutionConfig, create_budget, run_blocking_write

#: Sanitized stage label used by the write helper for every privacy RPC.
_PRIVACY_WRITE_STAGE = "privacy_operation"


class PrivacyOperationResponse(BaseModel):
    """Public projection of an applied privacy operation.

    Deliberately smaller than ``PrivacyOperationResult``: only the operation
    name, the constant ``applied`` status and the aggregate safe counts are
    exposed. Identity, revision, operation_id, internal IDs, content,
    snapshots and secrets never appear here.
    """

    operation: str
    status: str
    counts: dict[str, int]

    @classmethod
    def from_result(cls, result: PrivacyOperationResult) -> "PrivacyOperationResponse":
        """Build the public projection from the internal #314 result."""
        return cls(
            operation=result.operation,
            status="applied",
            counts=dict(result.counts),
        )


class PrivacyRepository(Protocol):
    """Synchronous RPC repository contract.

    ``call`` is thread-bound (``client.rpc(...).execute()`` blocks on
    network I/O) and must be dispatched off the event loop by the service.
    """

    def call(self, rpc_name: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Invoke one privacy RPC and return the parsed result mapping."""
        ...


class SupabasePrivacyRepository:
    """Synchronous adapter over ``client.rpc(name, params).execute()``.

    The Supabase SDK used by this project exposes a synchronous RPC
    (``client.rpc(name, params).execute()``). This adapter isolates that
    blocking call so the service can run it through ``run_blocking_write``;
    it must never be awaited directly on the event loop.

    The response shape is validated fail-closed: PostgREST may return the
    RPC result as a single mapping or as a single-element list. Anything
    else (or a missing/unreachable client) surfaces as a sanitized
    ``PersistenceError``, never as raw payload content.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def call(self, rpc_name: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Invoke one privacy RPC and return the parsed result mapping.

        The response shape is validated fail-closed INSIDE the try/except:
        a missing/unreachable client, a response without ``data``, a list
        that is not exactly one element, or a non-dict payload all surface
        as a sanitized ``PersistenceError``. Upstream exceptions (which may
        carry connection details, identifiers or payload content) are never
        surfaced.
        """
        if self._client is None:
            raise PersistenceError("database_error", "persistence error")
        try:
            response = self._client.rpc(rpc_name, params).execute()
            data = getattr(response, "data", None)
            if isinstance(data, list):
                if len(data) != 1:
                    raise PersistenceError("database_error", "persistence error")
                data = data[0]
            if not isinstance(data, dict):
                raise PersistenceError("database_error", "persistence error")
            return data
        except PersistenceError:
            raise
        except Exception:
            # Sanitized: the upstream exception may carry connection details,
            # identifiers or payload content and is never surfaced.
            raise PersistenceError("database_error", "persistence error") from None


class PrivacyService:
    """Stateless application service for the four #315 privacy actions.

    The service holds no per-user state: the authenticated identity, the
    operation_id and (for resets) the neutral snapshot are constructed per
    call. The clock is injected so reset timestamps are deterministic in
    tests, and the operational turn configuration drives the per-action
    budget and the Supabase transport timeout.

    Every method delegates to ``run_privacy_operation``; the blocking
    Supabase RPC is dispatched through ``run_blocking_write``, so writes are
    never abandoned on cancellation and no orphaned threads/tasks are
    created.
    """

    def __init__(
        self,
        *,
        repository: PrivacyRepository,
        turn_config: TurnExecutionConfig,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repository = repository
        self._turn_config = turn_config
        self._clock = clock

    async def delete_history(
        self, authenticated_user_id: str, operation_id: str
    ) -> PrivacyOperationResponse:
        """Apply ``delete_history`` for the authenticated identity."""
        return await self._run(
            OPERATION_DELETE_HISTORY,
            authenticated_user_id,
            operation_id,
            payload=None,
        )

    async def delete_memories(
        self, authenticated_user_id: str, operation_id: str
    ) -> PrivacyOperationResponse:
        """Apply ``delete_memories`` for the authenticated identity."""
        return await self._run(
            OPERATION_DELETE_MEMORIES,
            authenticated_user_id,
            operation_id,
            payload=None,
        )

    async def reset_emotional_state(
        self, authenticated_user_id: str, operation_id: str
    ) -> PrivacyOperationResponse:
        """Reset the emotional snapshot to the canonical neutral v1 state.

        The neutral snapshot is built through the #314 helper using the
        injected clock, so the reset timestamp is deterministic and never
        hides a ``time.time()`` inside the service.
        """
        payload = neutral_emotional_snapshot(self._clock())
        return await self._run(
            OPERATION_RESET_EMOTIONAL_STATE,
            authenticated_user_id,
            operation_id,
            payload=payload,
        )

    async def reset_relationship_state(
        self, authenticated_user_id: str, operation_id: str
    ) -> PrivacyOperationResponse:
        """Reset the relationship snapshot to the canonical neutral v1 state."""
        payload = neutral_relationship_snapshot(self._clock())
        return await self._run(
            OPERATION_RESET_RELATIONSHIP_STATE,
            authenticated_user_id,
            operation_id,
            payload=payload,
        )

    async def _run(
        self,
        operation: str,
        authenticated_user_id: str,
        operation_id: str,
        payload: Optional[Mapping[str, Any]],
    ) -> PrivacyOperationResponse:
        """Execute one operation with a per-action budget.

        A fresh budget is created for each action from the operational turn
        configuration. The synchronous repository call runs through
        ``run_blocking_write`` (real timeout from the PostgREST transport,
        drain-on-cancellation, no orphaned tasks); the resulting async
        callable is handed to ``run_privacy_operation``, which remains the
        single source of truth for validation, RPC invocation and result
        parsing of the #314 frontier.
        """
        budget = create_budget(self._turn_config)

        async def rpc_client(rpc_name: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
            return await run_blocking_write(
                _PRIVACY_WRITE_STAGE,
                budget,
                self._turn_config.supabase_timeout,
                self._repository.call,
                rpc_name,
                params,
            )

        result = await run_privacy_operation(
            rpc_client=rpc_client,
            operation=operation,
            authenticated_user_id=authenticated_user_id,
            operation_id=operation_id,
            payload=payload,
        )
        return PrivacyOperationResponse.from_result(result)
