"""
Transactional, idempotent privacy data operations (#314).

This module provides the server-side Python contract for the four privacy
primitives exposed by the PostgreSQL RPC layer (migration
``20260808220000_privacy_data_operations``):

- ``delete_history``: atomically removes turn history (chat_logs,
  turn_requests, derived outbox_events and archival_extractions).
- ``delete_memories``: atomically removes memories and archival/candidate
  material that may still represent ungoverned memory.
- ``reset_emotional_state``: replaces ONLY the emotional snapshot with a
  validated v1 neutral snapshot produced by the domain.
- ``reset_relationship_state``: replaces ONLY the relationship snapshot with a
  validated v1 neutral snapshot produced by the domain.

Design guarantees (implemented and enforced by the database):

- Identity comes ONLY from ``authenticated_user_id`` (server-side boundary).
  No ``user_id`` inside a payload/snapshot is ever trusted.
- Every applied operation records a durable ledger row keyed by
  (user_id, operation_id) storing the sanitized public result and the
  fingerprint of the operation payload. Replay of the same operation_id with
  the same operation and payload returns the stored result WITHOUT repeating
  the mutation or incrementing ``profiles.revision``.
- The same operation_id with a different operation or payload fails with a
  sanitized ``operation_conflict``.
- Concurrency uses the SAME per-user advisory transaction lock as
  ``commit_turn``; concurrent operations of one user are serialized, distinct
  users never contend on a global lock.
- Results and errors are sanitized: only status/operation/operation_id/
  user_id/revision and aggregate safe counts are returned.

This module is pure Python (no network, no client construction at import) and
mirrors ``backend.atomic_turn_commit``: validation runs BEFORE the RPC, and
unexpected persistence failures surface as ``PersistenceError`` with a
sanitized constant message.
"""

from __future__ import annotations

import re
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from backend.atomic_turn_commit import (
    PersistenceError,
    ValidationError,
    ConflictError,
    _validate_snapshot_payload,
)
from backend.emotional_domain import EmotionalStateV1
from backend.relationship import RelationshipStateV1

# Canonical operation names (must match the database CHECK constraint).
OPERATION_DELETE_HISTORY = "delete_history"
OPERATION_DELETE_MEMORIES = "delete_memories"
OPERATION_RESET_EMOTIONAL_STATE = "reset_emotional_state"
OPERATION_RESET_RELATIONSHIP_STATE = "reset_relationship_state"

PRIVACY_OPERATIONS = frozenset(
    {
        OPERATION_DELETE_HISTORY,
        OPERATION_DELETE_MEMORIES,
        OPERATION_RESET_EMOTIONAL_STATE,
        OPERATION_RESET_RELATIONSHIP_STATE,
    }
)

# Snapshot kind used for payload validation of each reset operation.
_RESET_SNAPSHOT_FIELD = {
    OPERATION_RESET_EMOTIONAL_STATE: "emotional_state",
    OPERATION_RESET_RELATIONSHIP_STATE: "relationship_state",
}

# UUID regex (same as the database: lowercase hex, canonical form).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# RPC function name per operation.
_RPC_NAME = {
    OPERATION_DELETE_HISTORY: "delete_history",
    OPERATION_DELETE_MEMORIES: "delete_memories",
    OPERATION_RESET_EMOTIONAL_STATE: "reset_emotional_state",
    OPERATION_RESET_RELATIONSHIP_STATE: "reset_relationship_state",
}

# Expected field set of a SUCCESS envelope (strict contract validation).
_SUCCESS_FIELDS = frozenset(
    {
        "status",
        "operation",
        "operation_id",
        "user_id",
        "revision",
        "counts",
    }
)

# Aggregate safe counts returned by every operation (all keys always present).
_COUNT_FIELDS = frozenset(
    {
        "chat_logs",
        "turn_requests",
        "outbox_events",
        "archival_extractions",
        "memories",
        "profiles",
    }
)


class PrivacyOperationError(Exception):
    """Base class for privacy operation domain failures."""


@dataclass(frozen=True)
class PrivacyOperationResult:
    """Sanitized public result of an applied privacy operation.

    Mirrors the database output of the four RPC primitives. Contains only
    status/operation/operation_id/user_id/revision and aggregate safe counts.
    Never contains message or memory content, internal IDs, prompts or HMACs.
    """

    operation: str
    operation_id: str
    user_id: str
    revision: int
    counts: Mapping[str, int]

    def to_db_row(self) -> dict[str, Any]:
        """Serialize for database result consumption (public contract only)."""
        return {
            "status": "applied",
            "operation": self.operation,
            "operation_id": self.operation_id,
            "user_id": self.user_id,
            "revision": self.revision,
            "counts": dict(self.counts),
        }


def new_operation_id() -> str:
    """Return a fresh server-side UUID operation id (lowercase canonical)."""
    return str(_uuid.uuid4())


def neutral_emotional_snapshot(timestamp: float) -> dict[str, Any]:
    """Return the validated domain neutral emotional snapshot (v1)."""
    return EmotionalStateV1.neutral(timestamp=timestamp).to_dict()


def neutral_relationship_snapshot(timestamp: float) -> dict[str, Any]:
    """Return the validated domain neutral relationship snapshot (v1)."""
    return RelationshipStateV1.neutral(timestamp=timestamp).to_dict()


def _validate_operation_id(operation_id: Any) -> None:
    if not isinstance(operation_id, str) or _UUID_RE.match(operation_id) is None:
        raise ValidationError(
            "invalid_operation_id", "operation_id must be a valid lowercase UUID"
        )


def _validate_user_id(user_id: Any) -> None:
    if not isinstance(user_id, str) or not user_id:
        raise ValidationError(
            "invalid_user_id", "authenticated_user_id must be a non-empty string"
        )


def validate_privacy_operation_input(
    operation: str,
    authenticated_user_id: str,
    operation_id: str,
    payload: Optional[Mapping[str, Any]],
) -> None:
    """Validate all inputs before attempting the operation.

    Defense-in-depth: the database enforces the true contract, but early
    validation prevents accidental information leaks and provides clearer
    error messages.

    Raises:
        ValidationError: If any input fails validation.
    """
    if not isinstance(operation, str) or operation not in PRIVACY_OPERATIONS:
        raise ValidationError("invalid_operation", "operation is invalid")

    _validate_user_id(authenticated_user_id)
    _validate_operation_id(operation_id)

    if payload is None or not isinstance(payload, Mapping):
        raise ValidationError(
            "invalid_operation_payload", "operation_payload must be a mapping"
        )

    field_name = _RESET_SNAPSHOT_FIELD.get(operation)
    if field_name is not None:
        # Resets REQUIRE a valid v1 snapshot produced by the domain. The
        # shared snapshot validator enforces the exact v1 contract used by
        # commit_turn (schema_version == 1, exact field set, ranges, no
        # identity/internal keys, size bound).
        _validate_snapshot_payload(payload, field_name)


def build_privacy_operation_rpc_payload(
    operation: str,
    authenticated_user_id: str,
    operation_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the payload for the privacy operation PostgreSQL RPC function."""
    return {
        "p_authenticated_user_id": authenticated_user_id,
        "p_operation_id": operation_id,
        "p_operation_payload": dict(payload),
    }


def _parse_error_envelope(error_info: Any) -> None:
    """Parse an error envelope and raise the matching exception.

    ``operation_conflict`` raises ConflictError; validation failures raise
    ValidationError; unexpected/unknown failures raise PersistenceError with a
    sanitized constant message (fail closed).
    """
    if not isinstance(error_info, Mapping):
        raise ValidationError("invalid_error_format", "error field must be a mapping")

    code = error_info.get("code")
    if not isinstance(code, str) or not code:
        raise ValidationError(
            "invalid_error_format", "error code must be a non-empty string"
        )

    message = error_info.get("message")
    if not isinstance(message, str):
        message = "internal error"

    if code == "operation_conflict":
        raise ConflictError(
            code=code,
            message=message,
            expected_revision=0,
            actual_revision=None,
            request_id=None,
        )

    if code == "validation_failed":
        raise ValidationError(code, message)

    # Unknown/other code: fail closed, sanitized.
    raise PersistenceError("database_error", "persistence error")


def parse_privacy_operation_result(
    result: Mapping[str, Any],
    expected_operation: str,
    expected_operation_id: str,
) -> PrivacyOperationResult:
    """Parse the RPC result into a PrivacyOperationResult.

    The success envelope is validated strictly: exactly the expected fields
    must be present (missing/extra fields indicate contract corruption), the
    operation and operation_id must match the request, and revision and every
    count must be non-negative integers.

    Raises:
        ConflictError: If the result indicates an operation conflict.
        ValidationError: If the result is malformed.
        PersistenceError: If the result indicates an unexpected database error.
    """
    if not isinstance(result, Mapping):
        raise ValidationError("invalid_rpc_result", "result must be a mapping")

    if "error" in result:
        _parse_error_envelope(result["error"])

    missing = _SUCCESS_FIELDS - set(result.keys())
    if missing:
        raise ValidationError(
            "invalid_rpc_result", f"missing field: {sorted(missing)[0]}"
        )
    extra = set(result.keys()) - _SUCCESS_FIELDS
    if extra:
        raise ValidationError(
            "invalid_rpc_result", f"unexpected field: {sorted(extra)[0]}"
        )

    operation = result["operation"]
    operation_id = result["operation_id"]
    user_id = result["user_id"]
    revision = result["revision"]
    counts_raw = result["counts"]

    if not isinstance(operation, str) or operation != expected_operation:
        raise ValidationError(
            "invalid_rpc_result", "operation does not match the request"
        )
    if not isinstance(operation_id, str) or operation_id != expected_operation_id:
        raise ValidationError(
            "invalid_rpc_result", "operation_id does not match the request"
        )
    if result["status"] != "applied":
        raise ValidationError("invalid_rpc_result", "status must be applied")
    if not isinstance(user_id, str) or not user_id:
        raise ValidationError("invalid_rpc_result", "user_id must be a non-empty string")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise ValidationError("invalid_rpc_result", "revision must be an integer")
    if revision < 0:
        raise ValidationError(
            "invalid_rpc_result", "revision must be non-negative"
        )
    if not isinstance(counts_raw, Mapping):
        raise ValidationError("invalid_rpc_result", "counts must be a mapping")

    counts: dict[str, int] = {}
    count_keys = set(counts_raw.keys())
    if count_keys != _COUNT_FIELDS:
        missing_count = sorted(_COUNT_FIELDS - count_keys)
        extra_count = sorted(count_keys - _COUNT_FIELDS)
        detail = ""
        if missing_count:
            detail += f" missing: {', '.join(missing_count)}"
        if extra_count:
            detail += f" unexpected: {', '.join(extra_count)}"
        raise ValidationError(
            "invalid_rpc_result", f"counts field set mismatch{detail}"
        )
    for key, value in counts_raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(
                "invalid_rpc_result", f"counts.{key} must be a non-negative integer"
            )
        counts[key] = value

    return PrivacyOperationResult(
        operation=operation,
        operation_id=operation_id,
        user_id=user_id,
        revision=revision,
        counts=counts,
    )


# Type alias for the RPC function callable (same contract as
# backend.atomic_turn_commit.RpcCallable).
RpcCallable = Any


async def run_privacy_operation(
    rpc_client: RpcCallable,
    operation: str,
    authenticated_user_id: str,
    operation_id: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> PrivacyOperationResult:
    """Execute one privacy operation via the PostgreSQL RPC function.

    Validation runs BEFORE the RPC is invoked, so invalid input never reaches
    the database. The RPC is awaited exactly once. Unexpected persistence
    failures raised by the RPC client are converted to PersistenceError with a
    sanitized constant message.

    Args:
        rpc_client: An awaitable callable ``rpc_client(name, params) -> Mapping``.
        operation: One of the four canonical privacy operation names.
        authenticated_user_id: Server-side authenticated identity. Never
            trusted from a payload.
        operation_id: Server-side UUID binding this operation attempt.
        payload: Operation payload. For resets this MUST be the validated
            neutral v1 snapshot produced by the domain; for deletions it is
            the (optional) request context and defaults to ``{}``.

    Returns:
        A PrivacyOperationResult with the sanitized public outcome.

    Raises:
        ValidationError: If input validation fails (RPC is NOT called).
        ConflictError: If the operation_id was already used with a different
            operation or payload.
        PersistenceError: If an unexpected persistence failure occurs.
    """
    effective_payload: Mapping[str, Any] = payload if payload is not None else {}

    validate_privacy_operation_input(
        operation=operation,
        authenticated_user_id=authenticated_user_id,
        operation_id=operation_id,
        payload=effective_payload,
    )

    rpc_payload = build_privacy_operation_rpc_payload(
        operation=operation,
        authenticated_user_id=authenticated_user_id,
        operation_id=operation_id,
        payload=effective_payload,
    )

    try:
        result = await rpc_client(_RPC_NAME[operation], rpc_payload)
    except Exception as exc:  # noqa: BLE001 - sanitize any RPC-level failure
        raise PersistenceError("database_error", "persistence error") from None

    return parse_privacy_operation_result(result, operation, operation_id)
