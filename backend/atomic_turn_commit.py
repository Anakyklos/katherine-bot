"""
Atomic turn commit implementation (#271).

This module provides the transactional commit functionality that atomically:
1. Acquires a per-user lock
2. Validates CAS (Compare-And-Swap) on profile revision
3. Creates profile if missing (race-safe)
4. Validates (user_id, request_id) uniqueness
5. Rejects divergent payload for existing request
6. Inserts user and assistant messages with stable IDs
7. Updates profile snapshots and increments revision exactly once
8. Completes turn_requests with reproducible result
9. Inserts idempotent outbox events
10. Commits all or nothing

The actual atomic work is delegated to a PostgreSQL RPC function
`public.commit_turn` that runs everything in a single transaction.
This Python module provides:
- Pure Python contracts and serialization for the RPC
- Input validation that mirrors database constraints
- Result deserialization
- Integration with the existing transactional_schema types

Security notes:
- No network calls, LLM calls, or embedding happens inside the transaction
- All user_id values are treated as authenticated; the caller is
  responsible for authentication before invoking this module
- Payload validation is defense-in-depth: the database enforces the
  true contract, but Python validation prevents accidental leaks
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from backend.transactional_schema import (
    FORBIDDEN_PAYLOAD_KEYS,
    REPLAY_PAYLOAD_ALLOWED_KEYS,
    canonical_payload_hash,
    TurnRequestRecord,
    OutboxEventRecord,
)


# Maximum lengths enforced by the database and mirrored here for early rejection.
_MAX_IDEMPOTENCY_KEY_LENGTH = 128
_MAX_LEASE_OWNER_LENGTH = 64
_MAX_ERROR_CODE_LENGTH = 64

# Maximum payload sizes (bytes, UTF-8 encoded).
_MAX_REPLAY_PAYLOAD_BYTES = 8192
_MAX_OUTBOX_PAYLOAD_BYTES = 8192


@dataclass(frozen=True)
class MessageRef:
    """Reference to a message in chat_logs."""

    user_id: str
    chat_log_id: int


@dataclass(frozen=True)
class CommittedTurn:
    """Result of a successful atomic turn commit.

    Fields mirror the database outputs of the commit_turn RPC.
    All server-owned IDs and timestamps are populated by the database.
    """

    user_id: str
    request_id: str
    committed_revision: int
    user_message_chat_log_id: int
    assistant_message_chat_log_id: int
    user_message_id: str
    assistant_message_id: str
    replay_payload: Mapping[str, Any]
    outbox_events: list[OutboxEventRecord]
    created_at: str
    completed_at: str

    def to_db_row(self) -> dict[str, Any]:
        """Serialize for database result consumption."""
        return {
            "user_id": self.user_id,
            "request_id": self.request_id,
            "committed_revision": self.committed_revision,
            "user_message_chat_log_id": self.user_message_chat_log_id,
            "assistant_message_chat_log_id": self.assistant_message_chat_log_id,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "replay_payload": dict(self.replay_payload),
            "outbox_events": [e.to_db_row() for e in self.outbox_events],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class ConflictError(Exception):
    """Raised when a concurrent modification is detected.

    Carries the database-provided details so the caller can implement
    retry/backoff logic. Subclasses Exception so it can be raised/caught
    in normal control flow.
    """

    code: str
    message: str
    expected_revision: Optional[int] = None
    actual_revision: Optional[int] = None
    request_id: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"{self.code}: {self.message}"]
        if self.expected_revision is not None:
            parts.append(f"expected_revision={self.expected_revision}")
        if self.actual_revision is not None:
            parts.append(f"actual_revision={self.actual_revision}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


@dataclass(frozen=True)
class ValidationError(Exception):
    """Raised when input validation fails before the transaction.

    Subclasses Exception so it can be used with `pytest.raises` and normal
    exception handling.
    """

    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


# Valid status values for turn_requests (mirrors database CHECK).
TURN_REQUEST_STATUS_PENDING = "pending"
TURN_REQUEST_STATUS_COMPLETED = "completed"
TURN_REQUEST_STATUS_EXPIRED = "expired"

# Valid status values for outbox_events (mirrors database CHECK).
OUTBOX_STATUS_PENDING = "pending"


def _validate_idempotency_key(key: str) -> None:
    """Validate an idempotency key against database constraints."""
    if not isinstance(key, str):
        raise ValidationError("invalid_idempotency_key", "must be a string")
    if len(key) < 1 or len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValidationError(
            "invalid_idempotency_key",
            f"must be 1-{_MAX_IDEMPOTENCY_KEY_LENGTH} characters",
        )
    # Mirror database regex: alphanumeric, period, underscore, colon, hyphen
    if not all(c.isalnum() or c in "._:-" for c in key):
        raise ValidationError(
            "invalid_idempotency_key",
            "must contain only alphanumeric characters, '.', '_', ':', '-'",
        )


def _validate_lease_owner(owner: Optional[str]) -> None:
    """Validate a lease owner against database constraints."""
    if owner is None:
        return
    if not isinstance(owner, str):
        raise ValidationError("invalid_lease_owner", "must be a string or None")
    if len(owner) < 1 or len(owner) > _MAX_LEASE_OWNER_LENGTH:
        raise ValidationError(
            "invalid_lease_owner",
            f"must be 1-{_MAX_LEASE_OWNER_LENGTH} characters",
        )
    if not all(c.isalnum() or c in "._:-" for c in owner):
        raise ValidationError(
            "invalid_lease_owner",
            "must contain only alphanumeric characters, '.', '_', ':', '-'",
        )


def _validate_error_code(code: Optional[str]) -> None:
    """Validate an error code against database constraints."""
    if code is None:
        return
    if not isinstance(code, str):
        raise ValidationError("invalid_error_code", "must be a string or None")
    if len(code) < 1 or len(code) > _MAX_ERROR_CODE_LENGTH:
        raise ValidationError(
            "invalid_error_code",
            f"must be 1-{_MAX_ERROR_CODE_LENGTH} characters",
        )
    # Only allow lowercase letters, digits and underscore
    if not all(c.isalnum() or c == "_" for c in code) or code.lower() != code:
        raise ValidationError(
            "invalid_error_code",
            "must contain only lowercase alphanumeric characters and '_'",
        )


def _validate_replay_payload(payload: Mapping[str, Any]) -> None:
    """Validate replay payload keys against the database allowlist."""
    if not isinstance(payload, Mapping):
        raise ValidationError("invalid_replay_payload", "must be a mapping")

    # Check for forbidden keys (recursive)
    def _has_forbidden(obj: Any) -> bool:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                if k in FORBIDDEN_PAYLOAD_KEYS or _has_forbidden(v):
                    return True
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                if _has_forbidden(v):
                    return True
        return False

    if _has_forbidden(payload):
        raise ValidationError(
            "invalid_replay_payload",
            "contains forbidden keys",
        )

    # Check that all top-level keys are in the allowlist
    for key in payload:
        if key not in REPLAY_PAYLOAD_ALLOWED_KEYS:
            raise ValidationError(
                "invalid_replay_payload",
                f"unknown key: {key}",
            )

    # Check size (encode to UTF-8 to match database octet_length)
    import json

    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ValidationError("invalid_replay_payload", "not JSON-serializable")

    if len(serialized.encode("utf-8")) > _MAX_REPLAY_PAYLOAD_BYTES:
        raise ValidationError(
            "invalid_replay_payload",
            f"exceeds {_MAX_REPLAY_PAYLOAD_BYTES} bytes",
        )


def validate_atomic_commit_input(
    authenticated_user_id: str,
    request_id: str,
    expected_revision: int,
    user_message: str,
    assistant_message: str,
    emotional_state: Optional[Mapping[str, Any]],
    relationship_state: Optional[Mapping[str, Any]],
    public_response: str,
    outbox_events: list[tuple[str, Mapping[str, Any], str]],
    replay_payload: Mapping[str, Any],
) -> None:
    """Validate all inputs before attempting the atomic commit.

    This is defense-in-depth: the database enforces the true contract,
    but early validation prevents accidental information leaks and
    provides clearer error messages.

    Args:
        authenticated_user_id: The authenticated user identifier.
        request_id: The unique request identifier (UUID string).
        expected_revision: The expected profile revision for CAS.
        user_message: The user's message content.
        assistant_message: The assistant's message content.
        emotional_state: The emotional state snapshot (nullable).
        relationship_state: The relationship state snapshot (nullable).
        public_response: The public response content.
        outbox_events: List of (event_type, payload, idempotency_key) tuples.
        replay_payload: The replay payload for turn_requests.

    Raises:
        ValidationError: If any input fails validation.
    """
    # Validate types
    if not isinstance(authenticated_user_id, str) or not authenticated_user_id:
        raise ValidationError("invalid_user_id", "must be a non-empty string")

    if not isinstance(request_id, str) or not request_id:
        raise ValidationError("invalid_request_id", "must be a non-empty string")

    if not isinstance(expected_revision, int) or expected_revision < 0:
        raise ValidationError(
            "invalid_expected_revision", "must be a non-negative integer"
        )

    if not isinstance(user_message, str):
        raise ValidationError("invalid_user_message", "must be a string")

    if not isinstance(assistant_message, str):
        raise ValidationError("invalid_assistant_message", "must be a string")

    if emotional_state is not None and not isinstance(emotional_state, Mapping):
        raise ValidationError("invalid_emotional_state", "must be a mapping or None")

    if relationship_state is not None and not isinstance(
        relationship_state, Mapping
    ):
        raise ValidationError(
            "invalid_relationship_state", "must be a mapping or None"
        )

    if not isinstance(public_response, str):
        raise ValidationError("invalid_public_response", "must be a string")

    if not isinstance(outbox_events, list):
        raise ValidationError("invalid_outbox_events", "must be a list")

    for i, (event_type, payload, idempotency_key) in enumerate(outbox_events):
        if not isinstance(event_type, str) or not event_type:
            raise ValidationError(
                "invalid_outbox_events", f"event {i}: event_type must be a non-empty string"
            )
        if not isinstance(payload, Mapping):
            raise ValidationError(
                "invalid_outbox_events", f"event {i}: payload must be a mapping"
            )
        _validate_idempotency_key(idempotency_key)

    _validate_replay_payload(replay_payload)


def build_commit_turn_rpc_payload(
    authenticated_user_id: str,
    request_id: str,
    expected_revision: int,
    user_message: str,
    assistant_message: str,
    emotional_state: Optional[Mapping[str, Any]],
    relationship_state: Optional[Mapping[str, Any]],
    public_response: str,
    outbox_events: list[tuple[str, Mapping[str, Any], str]],
    replay_payload: Mapping[str, Any],
    payload_hash_sha256: str,
    lease_owner: Optional[str],
) -> dict[str, Any]:
    """Build the payload for the commit_turn PostgreSQL RPC function.

    The RPC function signature in SQL is:
    ```sql
    commit_turn(
        p_authenticated_user_id text,
        p_request_id uuid,
        p_expected_revision bigint,
        p_user_message text,
        p_assistant_message text,
        p_emotional_state jsonb,
        p_relationship_state jsonb,
        p_public_response text,
        p_payload_hash_sha256 text,
        p_outbox_events jsonb,  -- array of event objects
        p_replay_payload jsonb,
        p_lease_owner text DEFAULT NULL
    ) RETURNS jsonb
    ```

    Args:
        All parameters as described in validate_atomic_commit_input.
        payload_hash_sha256: The SHA-256 hash of the canonical request payload.
        lease_owner: The worker identifier claiming this commit.

    Returns:
        A dictionary suitable for passing as RPC parameters.
    """
    # Convert UUID string to PostgreSQL uuid format if needed
    # PostgreSQL accepts standard UUID string format
    rpc_request_id = request_id

    # Build outbox events array
    outbox_array = []
    for event_type, payload, idempotency_key in outbox_events:
        outbox_array.append(
            {
                "event_type": event_type,
                "payload": dict(payload),
                "idempotency_key": idempotency_key,
            }
        )

    return {
        "p_authenticated_user_id": authenticated_user_id,
        "p_request_id": rpc_request_id,
        "p_expected_revision": expected_revision,
        "p_user_message": user_message,
        "p_assistant_message": assistant_message,
        "p_emotional_state": dict(emotional_state) if emotional_state else None,
        "p_relationship_state": (
            dict(relationship_state) if relationship_state else None
        ),
        "p_public_response": public_response,
        "p_outbox_events": outbox_array,
        "p_replay_payload": dict(replay_payload),
        "p_payload_hash_sha256": payload_hash_sha256,
        "p_lease_owner": lease_owner,
    }


def parse_commit_turn_result(result: Mapping[str, Any]) -> CommittedTurn:
    """Parse the result of the commit_turn RPC into a CommittedTurn.

    The RPC returns a JSON object with the following structure:
    {
        "user_id": "...",
        "request_id": "...",
        "committed_revision": 123,
        "user_message_chat_log_id": 456,
        "assistant_message_chat_log_id": 789,
        "user_message_id": "uuid...",
        "assistant_message_id": "uuid...",
        "replay_payload": {...},
        "outbox_events": [...],
        "created_at": "2024-01-01T00:00:00Z",
        "completed_at": "2024-01-01T00:00:00Z"
    }

    Args:
        result: The raw RPC result as a mapping.

    Returns:
        A CommittedTurn instance.

    Raises:
        ConflictError: If the result indicates a conflict.
        ValidationError: If the result is malformed.
    """
    if not isinstance(result, Mapping):
        raise ValidationError("invalid_rpc_result", "result must be a mapping")

    # Check for error response
    if "error" in result:
        error_info = result["error"]
        if isinstance(error_info, Mapping):
            code = error_info.get("code", "unknown_error")
            message = error_info.get("message", "unknown error")
            expected = error_info.get("expected_revision")
            actual = error_info.get("actual_revision")
            request = error_info.get("request_id")
            raise ConflictError(
                code=code,
                message=message,
                expected_revision=expected if isinstance(expected, int) else None,
                actual_revision=actual if isinstance(actual, int) else None,
                request_id=request if isinstance(request, str) else None,
            )
        else:
            raise ConflictError(
                code="unknown_error",
                message=str(error_info),
                expected_revision=0,
            )

    try:
        user_id = result["user_id"]
        request_id = result["request_id"]
        committed_revision = result["committed_revision"]
        user_message_chat_log_id = result["user_message_chat_log_id"]
        assistant_message_chat_log_id = result["assistant_message_chat_log_id"]
        user_message_id = result["user_message_id"]
        assistant_message_id = result["assistant_message_id"]
        replay_payload = result["replay_payload"]
        outbox_events_raw = result["outbox_events"]
        created_at = result["created_at"]
        completed_at = result["completed_at"]
    except KeyError as e:
        raise ValidationError("invalid_rpc_result", f"missing field: {e}")

    # Parse outbox events
    outbox_events: list[OutboxEventRecord] = []
    if isinstance(outbox_events_raw, list):
        for evt in outbox_events_raw:
            if isinstance(evt, Mapping):
                outbox_events.append(OutboxEventRecord.from_db_row(evt))

    return CommittedTurn(
        user_id=user_id,
        request_id=request_id,
        committed_revision=committed_revision,
        user_message_chat_log_id=user_message_chat_log_id,
        assistant_message_chat_log_id=assistant_message_chat_log_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        replay_payload=replay_payload if isinstance(replay_payload, Mapping) else {},
        outbox_events=outbox_events,
        created_at=created_at,
        completed_at=completed_at,
    )


# Type alias for the RPC function callable
# The actual implementation depends on the database client being used
# This is a placeholder for the type signature
RpcCallable = Any


async def commit_turn(
    rpc_client: RpcCallable,
    authenticated_user_id: str,
    request_id: str,
    expected_revision: int,
    user_message: str,
    assistant_message: str,
    emotional_state: Optional[Mapping[str, Any]],
    relationship_state: Optional[Mapping[str, Any]],
    public_response: str,
    outbox_events: list[tuple[str, Mapping[str, Any], str]],
    replay_payload: Mapping[str, Any],
    lease_owner: Optional[str] = None,
) -> CommittedTurn:
    """Execute an atomic turn commit via the PostgreSQL RPC function.

    This is the main entry point for committing a turn atomically.
    All validation is performed before the RPC is invoked, and the
    result is parsed into a CommittedTurn object.

    Args:
        rpc_client: A callable that invokes the PostgreSQL RPC function.
                   Signature: rpc_client(name: str, params: dict) -> Mapping[str, Any]
        authenticated_user_id: The authenticated user identifier.
        request_id: The unique request identifier (UUID string).
        expected_revision: The expected profile revision for CAS.
        user_message: The user's message content.
        assistant_message: The assistant's message content.
        emotional_state: The emotional state snapshot.
        relationship_state: The relationship state snapshot.
        public_response: The public response content.
        outbox_events: List of (event_type, payload, idempotency_key) tuples.
        replay_payload: The replay payload for turn_requests.
        lease_owner: Optional worker identifier claiming this commit.

    Returns:
        A CommittedTurn instance with all committed data.

    Raises:
        ValidationError: If input validation fails.
        ConflictError: If a concurrent modification is detected.
        Exception: Any other database or RPC error.
    """
    # Validate inputs
    validate_atomic_commit_input(
        authenticated_user_id=authenticated_user_id,
        request_id=request_id,
        expected_revision=expected_revision,
        user_message=user_message,
        assistant_message=assistant_message,
        emotional_state=emotional_state,
        relationship_state=relationship_state,
        public_response=public_response,
        outbox_events=outbox_events,
        replay_payload=replay_payload,
    )

    if lease_owner:
        _validate_lease_owner(lease_owner)

    # Compute payload hash
    # The canonical payload for hashing should include all the inputs
    # that determine the turn's identity and content
    canonical_payload = {
        "user_id": authenticated_user_id,
        "request_id": request_id,
        "expected_revision": expected_revision,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "emotional_state": emotional_state,
        "relationship_state": relationship_state,
        "public_response": public_response,
    }
    payload_hash_sha256 = canonical_payload_hash(canonical_payload)

    # Build RPC payload
    rpc_payload = build_commit_turn_rpc_payload(
        authenticated_user_id=authenticated_user_id,
        request_id=request_id,
        expected_revision=expected_revision,
        user_message=user_message,
        assistant_message=assistant_message,
        emotional_state=emotional_state,
        relationship_state=relationship_state,
        public_response=public_response,
        outbox_events=outbox_events,
        replay_payload=replay_payload,
        payload_hash_sha256=payload_hash_sha256,
        lease_owner=lease_owner,
    )

    # Invoke RPC
    result = await rpc_client("commit_turn", rpc_payload)

    # Parse and return
    return parse_commit_turn_result(result)
