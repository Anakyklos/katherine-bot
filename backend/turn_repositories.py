"""
Minimal repository adapters for the ProcessTurn use case (#272).

Each adapter owns one narrow persistence responsibility and is a thin,
duck-typed wrapper around the Supabase client (or ``None`` when the store is
unconfigured). All methods are synchronous: the ProcessTurn use case dispatches
them through ``run_blocking_read`` / ``run_blocking_write`` so reads never
consume the commit reserve and writes are never abandoned on cancellation.

Error contract:
- ``PersistenceError`` — unexpected store failure (sanitized constant message)
- ``ConflictError`` / ``ValidationError`` — stable domain results parsed from
  the RPC envelopes (never by parsing exception text)
- ``TurnExecutionError(internal_error)`` — persisted data violates the
  contract (duplicate profile row, invalid revision, malformed snapshot)

The adapters never create profiles and never write on read paths: initial
profile creation happens exclusively inside ``commit_turn`` (race-safe).
"""

from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .atomic_turn_commit import (
    CommittedTurn,
    ConflictError,
    PersistenceError,
    ValidationError,
    _parse_error_envelope,
    _validate_lease_owner,
    commit_turn,
    parse_commit_turn_result,
)
from .emotional_domain import EmotionalStateV1
from .relationship import RelationshipStateV1
from .turn_execution import TurnErrorCode, TurnExecutionError

REPLAY_STATUS_COMPLETED = "completed"
REPLAY_STATUS_IN_PROGRESS = "request_in_progress"
REPLAY_STATUS_UNAVAILABLE = "request_replay_unavailable"

_REPLAY_STATUSES = frozenset({REPLAY_STATUS_IN_PROGRESS, REPLAY_STATUS_UNAVAILABLE})


def _unwrap_rpc_data(data: Any) -> Mapping[str, Any]:
    """Normalize a PostgREST RPC response into a single mapping.

    Raises ``ValidationError`` (internal contract error) when the shape is
    not a single JSON object; the malformed payload is never echoed.
    """
    if isinstance(data, list):
        if len(data) != 1:
            raise ValidationError("invalid_rpc_result", "unexpected result shape")
        data = data[0]
    if not isinstance(data, Mapping):
        raise ValidationError("invalid_rpc_result", "result must be a mapping")
    return data


@dataclass(frozen=True)
class LoadedUserState:
    """Snapshot data loaded from ``profiles`` together with ``revision``.

    A missing profile yields default in-memory v1 state with ``revision == 0``
    and is NEVER inserted during loading (creation happens inside
    ``commit_turn``, which is race-safe).
    """

    user_id: str
    revision: int
    persona_config: Any
    user_profile: Mapping[str, Any]
    emotional_state: Mapping[str, Any]
    relationship_state: Mapping[str, Any]


class UserStateRepository:
    """Loads profile snapshots and ``revision`` without creating profiles."""

    def __init__(self, client_provider: Callable[[], Any]) -> None:
        self._client_provider = client_provider

    def load(self, user_id: str, default_timestamp: float) -> LoadedUserState:
        client = self._client_provider()
        if client is None:
            raise PersistenceError("database_error", "persistence error")
        try:
            response = (
                client.table("profiles")
                .select("revision, persona_config, user_profile, emotional_state, relationship_state")
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            raise PersistenceError("database_error", "persistence error") from None

        data = getattr(response, "data", None)
        if not isinstance(data, list):
            raise PersistenceError("database_error", "persistence error")

        if len(data) == 0:
            # Missing profile: default in-memory v1 state, revision 0, no insert.
            effective_timestamp = default_timestamp if default_timestamp is not None else _time.time()
            neutral_emotional = EmotionalStateV1.neutral(timestamp=effective_timestamp).to_dict()
            neutral_relationship = RelationshipStateV1.neutral(
                timestamp=effective_timestamp
            ).to_dict()
            return LoadedUserState(
                user_id=user_id,
                revision=0,
                persona_config=None,
                user_profile={},
                emotional_state=neutral_emotional,
                relationship_state=neutral_relationship,
            )

        if len(data) > 1:
            # Duplicate profile row: fail closed (contract corruption).
            raise TurnExecutionError(
                TurnErrorCode.internal_error, "Invalid persisted profile state."
            )

        row = data[0]
        if not isinstance(row, Mapping):
            raise TurnExecutionError(
                TurnErrorCode.internal_error, "Invalid persisted profile state."
            )

        revision = row.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise TurnExecutionError(
                TurnErrorCode.internal_error, "Invalid persisted profile revision."
            )

        emotional = row.get("emotional_state")
        relationship = row.get("relationship_state")
        profile = row.get("user_profile")
        persona = row.get("persona_config")

        if emotional is not None and not isinstance(emotional, Mapping):
            raise TurnExecutionError(
                TurnErrorCode.internal_error, "Invalid persisted emotional state."
            )
        if relationship is not None and not isinstance(relationship, Mapping):
            raise TurnExecutionError(
                TurnErrorCode.internal_error, "Invalid persisted relationship state."
            )
        if profile is not None and not isinstance(profile, Mapping):
            profile = {}
        if not isinstance(persona, str):
            persona = None

        return LoadedUserState(
            user_id=user_id,
            revision=revision,
            persona_config=persona,
            user_profile=dict(profile) if profile is not None else {},
            emotional_state=dict(emotional) if emotional is not None else {},
            relationship_state=dict(relationship) if relationship is not None else {},
        )


class TurnCommitRepository:
    """Invokes ``commit_turn`` exactly once and interprets its result.

    Reuses the validation / hashing / parsing of ``atomic_turn_commit`` (via
    ``asyncio.run`` inside the worker thread) so the RPC payload and the
    interpretation of ``CommittedTurn`` / ``ConflictError`` / ``ValidationError``
    / ``PersistenceError`` have a single source of truth.
    """

    def __init__(self, client_provider: Callable[[], Any]) -> None:
        self._client_provider = client_provider

    def commit(self, **kwargs: Any) -> CommittedTurn:
        client = self._client_provider()
        if client is None:
            raise PersistenceError("database_error", "persistence error")

        lease_owner = kwargs.get("lease_owner")
        if lease_owner is not None:
            _validate_lease_owner(lease_owner)

        async def _rpc_client(name: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
            response = client.rpc(name, dict(params)).execute()
            return _unwrap_rpc_data(response.data)

        try:
            return asyncio.run(commit_turn(_rpc_client, **kwargs))
        except (ConflictError, ValidationError, PersistenceError):
            raise
        except Exception:
            # Unexpected persistence failure: never expose the underlying error.
            raise PersistenceError("database_error", "persistence error") from None


@dataclass(frozen=True)
class ReplayOutcome:
    """Structured result of a replay lookup.

    ``status`` is one of ``completed`` / ``request_in_progress`` /
    ``request_replay_unavailable``. ``committed`` is populated only for
    ``completed`` and carries the canonical public contract.
    """

    status: str
    committed: Optional[CommittedTurn] = None


def parse_replay_committed_turn_result(result: Mapping[str, Any]) -> ReplayOutcome:
    """Parse the ``replay_committed_turn`` RPC result (single format).

    Completed rows reuse the canonical ``parse_commit_turn_result`` builder;
    structured statuses are validated strictly. Malformed envelopes fail
    closed with ``ValidationError``; domain error envelopes are converted via
    the canonical error parser (never by parsing exception text).
    """
    if not isinstance(result, Mapping):
        raise ValidationError("invalid_rpc_result", "result must be a mapping")

    if "error" in result:
        _parse_error_envelope(result["error"])

    status = result.get("status")
    if status is not None:
        if status not in _REPLAY_STATUSES:
            raise ValidationError("invalid_rpc_result", "unknown replay status")
        return ReplayOutcome(status=status, committed=None)

    return ReplayOutcome(
        status=REPLAY_STATUS_COMPLETED,
        committed=parse_commit_turn_result(result),
    )


class TurnReplayRepository:
    """Retrieves an already-completed turn result without calling the provider."""

    def __init__(self, client_provider: Callable[[], Any]) -> None:
        self._client_provider = client_provider

    def replay(self, authenticated_user_id: str, request_id: str) -> ReplayOutcome:
        client = self._client_provider()
        if client is None:
            raise PersistenceError("database_error", "persistence error")
        try:
            response = client.rpc(
                "replay_committed_turn",
                {
                    "p_authenticated_user_id": authenticated_user_id,
                    "p_request_id": request_id,
                },
            ).execute()
        except Exception:
            raise PersistenceError("database_error", "persistence error") from None
        return parse_replay_committed_turn_result(_unwrap_rpc_data(response.data))
