"""
Atomic turn commit implementation (#271).

This module provides the transactional commit functionality that atomically:
1. Acquires a per-user lock
2. Validates CAS (Compare-And-Swap) on profile revision
3. Creates profile if missing (race-safe)
4. Validates (user_id, request_id) uniqueness
5. Rejects divergent payload for active requests; reclaims expired/expired-lease requests
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

import re

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

from backend.transactional_schema import (
    FORBIDDEN_PAYLOAD_KEYS,
    REPLAY_PAYLOAD_ALLOWED_KEYS,
    canonical_payload_hash,
    TurnRequestRecord,
    OutboxEventRecord,
    _deep_freeze,
    _deep_unfreeze,
)


# Maximum lengths enforced by the database and mirrored here for early rejection.
_MAX_IDEMPOTENCY_KEY_LENGTH = 128
_MAX_LEASE_OWNER_LENGTH = 64
_MAX_ERROR_CODE_LENGTH = 64

# Maximum payload sizes (bytes, UTF-8 encoded).
_MAX_REPLAY_PAYLOAD_BYTES = 8192
_MAX_OUTBOX_PAYLOAD_BYTES = 8192

# Snapshot validation forbidden keys (same as payload validation)
_SNAPSHOT_FORBIDDEN_KEYS = FORBIDDEN_PAYLOAD_KEYS


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
    All nested structures are deeply immutable.
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

    def __post_init__(self) -> None:
        # Deep-freeze nested structures for true immutability
        object.__setattr__(self, "replay_payload", _deep_freeze(self.replay_payload))
        object.__setattr__(self, "outbox_events", tuple(_deep_freeze(e) for e in self.outbox_events))

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
            "replay_payload": _deep_unfreeze(self.replay_payload),
            "outbox_events": [e.to_db_row() for e in self.outbox_events],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class ConflictError(Exception):
    """Raised when a concurrent modification is detected.

    Carries the database-provided details so the caller can implement
    retry/backoff logic.
    """

    __slots__ = ("code", "message", "expected_revision", "actual_revision", "request_id")

    def __init__(
        self,
        code: str,
        message: str,
        expected_revision: int,
        actual_revision: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> None:
        self.code = code
        self.message = message
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.request_id = request_id

    def __str__(self) -> str:
        parts = [f"{self.code}: {self.message}"]
        if self.expected_revision is not None:
            parts.append(f"expected_revision={self.expected_revision}")
        if self.actual_revision is not None:
            parts.append(f"actual_revision={self.actual_revision}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


class ValidationError(Exception):
    """Raised when input validation fails before the transaction."""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


# Valid status values for turn_requests (mirrors database CHECK).
TURN_REQUEST_STATUS_PENDING = "pending"
TURN_REQUEST_STATUS_COMPLETED = "completed"
TURN_REQUEST_STATUS_EXPIRED = "expired"

# Valid status values for outbox_events (mirrors database CHECK).
OUTBOX_STATUS_PENDING = "pending"

# UUID regex pattern (same as database: [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})
_UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _validate_uuid(value: str, field_name: str) -> None:
    """Validate that a string is a valid lowercase hex UUID."""
    if not isinstance(value, str):
        raise ValidationError(f"invalid_{field_name}", f"must be a string")
    if not _UUID_PATTERN.match(value):
        raise ValidationError(f"invalid_{field_name}", "must be a valid lowercase UUID")


def _validate_snapshot_payload(payload: Optional[Mapping[str, Any]], field_name: str) -> None:
    """Validate snapshot payloads (emotional_state, relationship_state) against forbidden keys and schema."""
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValidationError(f"invalid_{field_name}", f"must be a mapping or None")
    
    # Require schema_version field
    if "schema_version" not in payload:
        raise ValidationError(
            f"invalid_{field_name}",
            "must contain schema_version field",
        )
    
    # schema_version must be integer 1
    schema_version = payload["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ValidationError(
            f"invalid_{field_name}",
            "schema_version must be an integer",
        )
    if schema_version != 1:
        raise ValidationError(
            f"invalid_{field_name}",
            "schema_version must be 1",
        )
    
    # Check for forbidden keys recursively
    def _has_forbidden(obj: Any) -> bool:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                if k in _SNAPSHOT_FORBIDDEN_KEYS or _has_forbidden(v):
                    return True
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                if _has_forbidden(v):
                    return True
        return False

    if _has_forbidden(payload):
        raise ValidationError(
            f"invalid_{field_name}",
            "contains forbidden keys",
        )
    
    # Check size for snapshots (same limit as replay_payload)
    try:
        import json
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ValidationError(f"invalid_{field_name}", "not JSON-serializable")
    
    if len(serialized.encode("utf-8")) > _MAX_REPLAY_PAYLOAD_BYTES:
        raise ValidationError(
            f"invalid_{field_name}",
            f"exceeds {_MAX_REPLAY_PAYLOAD_BYTES} bytes",
        )


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
    # Must be lowercase alphanumeric with underscores only
    if not all((c.isalnum() and c.islower()) or c == "_" for c in code):
        raise ValidationError(
            "invalid_error_code",
            "must contain only lowercase alphanumeric characters and '_'",
        )


def _validate_outbox_event_payload(payload: Mapping[str, Any], event_index: int) -> None:
    """Validate outbox event payload against allowlist and forbidden keys."""
    # Allowed keys for outbox event payload (same as database validation)
    _OUTBOX_PAYLOAD_ALLOWED_KEYS = {
        'ref', 'request_id', 'turn_id', 'message_id', 'entity_id', 'kind', 'version'
    }
    
    # Check for allowed keys
    for key in payload.keys():
        if key not in _OUTBOX_PAYLOAD_ALLOWED_KEYS:
            raise ValidationError(
                "invalid_outbox_events",
                f"event {event_index}: payload contains disallowed key '{key}'",
            )
    
    # Check for forbidden keys recursively
    def _has_forbidden(obj: Any) -> bool:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                if k in _SNAPSHOT_FORBIDDEN_KEYS or _has_forbidden(v):
                    return True
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                if _has_forbidden(v):
                    return True
        return False
    
    if _has_forbidden(payload):
        raise ValidationError(
            "invalid_outbox_events",
            f"event {event_index}: payload contains forbidden keys",
        )
    
    # Validate value contract for each field
    for key, value in payload.items():
        if key in ('ref', 'request_id', 'turn_id', 'message_id', 'entity_id', 'kind'):
            # Must be scalar string, max 128 chars
            if not isinstance(value, str) or not value:
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {event_index}: payload.{key} must be a non-empty string",
                )
            if len(value) > 128:
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {event_index}: payload.{key} must be <= 128 characters",
                )
        elif key == 'version':
            # Must be integer in range [1, 1000]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {event_index}: payload.version must be an integer",
                )
            if value < 1 or value > 1000:
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {event_index}: payload.version must be in range 1-1000",
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

    # Validate request_id is a valid UUID
    _validate_uuid(request_id, "request_id")

    # expected_revision must be an integer, not bool (bool is subclass of int in Python)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise ValidationError(
            "invalid_expected_revision", "must be a non-negative integer"
        )
    if expected_revision < 0:
        raise ValidationError(
            "invalid_expected_revision", "must be a non-negative integer"
        )

    if not isinstance(user_message, str):
        raise ValidationError("invalid_user_message", "must be a string")

    if not isinstance(assistant_message, str):
        raise ValidationError("invalid_assistant_message", "must be a string")

    # Validate snapshots
    _validate_snapshot_payload(emotional_state, "emotional_state")
    _validate_snapshot_payload(relationship_state, "relationship_state")

    if not isinstance(public_response, str):
        raise ValidationError("invalid_public_response", "must be a string")

    if not isinstance(outbox_events, list):
        raise ValidationError("invalid_outbox_events", "must be a list")

    # Event type regex pattern (same as database: ^[a-z0-9_]{1,64}$)
    _EVENT_TYPE_PATTERN = re.compile(r'^[a-z0-9_]{1,64}$')
    
    # Outbox payload forbidden keys (same as database validation)
    _OUTBOX_FORBIDDEN_KEYS = FORBIDDEN_PAYLOAD_KEYS
    
    for i, (event_type, payload, idempotency_key) in enumerate(outbox_events):
        # Validate event_type
        if not isinstance(event_type, str) or not event_type:
            raise ValidationError(
                "invalid_outbox_events", f"event {i}: event_type must be a non-empty string"
            )
        if not _EVENT_TYPE_PATTERN.match(event_type):
            raise ValidationError(
                "invalid_outbox_events",
                f"event {i}: event_type must match ^[a-z0-9_]{{1,64}}$",
            )
        
        # Validate payload
        if not isinstance(payload, Mapping):
            raise ValidationError(
                "invalid_outbox_events", f"event {i}: payload must be a mapping"
            )
        
        # Validate payload keys (allowlist and forbidden)
        _validate_outbox_event_payload(payload, i)
        
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

    The RPC function signature in SQL is (corrected order):
    ```sql
    commit_turn(
        p_authenticated_user_id text,
        p_request_id uuid,
        p_expected_revision bigint,
        p_user_message text,
        p_assistant_message text,
        p_payload_hash_sha256 text,
        p_emotional_state jsonb,
        p_relationship_state jsonb,
        p_public_response text,
        p_replay_payload jsonb,
        p_outbox_events jsonb DEFAULT '[]'::jsonb,
        p_lease_owner text DEFAULT NULL
    ) RETURNS jsonb
    ```
    
    Note: Parameter order has been corrected so all required parameters
    come before optional parameters (to avoid SQLSTATE 42P13).

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
        "p_payload_hash_sha256": payload_hash_sha256,
        "p_emotional_state": dict(emotional_state) if emotional_state else None,
        "p_relationship_state": dict(relationship_state) if relationship_state else None,
        "p_public_response": public_response,
        "p_replay_payload": dict(replay_payload),
        "p_outbox_events": outbox_array,
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
            # Only raise ConflictError for known conflict codes
            # database_error should raise a different exception
            if code in ("revision_mismatch", "request_payload_conflict"):
                raise ConflictError(
                    code=code,
                    message=message,
                    expected_revision=expected if isinstance(expected, int) else None,
                    actual_revision=actual if isinstance(actual, int) else None,
                    request_id=request if isinstance(request, str) else None,
                )
            else:
                # For database_error and other unknown errors, raise ValidationError
                raise ValidationError(code, message)
        else:
            raise ValidationError("invalid_error_format", "error field must be a mapping")

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

    # Validate field types - fail on type mismatch, don't silently convert
    if not isinstance(user_id, str):
        raise ValidationError("invalid_user_id", "user_id must be a string")
    if not isinstance(request_id, str):
        raise ValidationError("invalid_request_id", "request_id must be a string")
    if not isinstance(committed_revision, int):
        raise ValidationError("invalid_committed_revision", "committed_revision must be an integer")
    if not isinstance(user_message_chat_log_id, int):
        raise ValidationError("invalid_user_message_chat_log_id", "user_message_chat_log_id must be an integer")
    if not isinstance(assistant_message_chat_log_id, int):
        raise ValidationError("invalid_assistant_message_chat_log_id", "assistant_message_chat_log_id must be an integer")
    if not isinstance(user_message_id, str):
        raise ValidationError("invalid_user_message_id", "user_message_id must be a string")
    if not isinstance(assistant_message_id, str):
        raise ValidationError("invalid_assistant_message_id", "assistant_message_id must be a string")
    if not isinstance(replay_payload, Mapping):
        raise ValidationError("invalid_replay_payload", "replay_payload must be a mapping")
    if not isinstance(outbox_events_raw, list):
        raise ValidationError("invalid_outbox_events", "outbox_events must be a list")
    if not isinstance(created_at, str):
        raise ValidationError("invalid_created_at", "created_at must be a string")
    if not isinstance(completed_at, str):
        raise ValidationError("invalid_completed_at", "completed_at must be a string")

    # Parse outbox events
    outbox_events: list[OutboxEventRecord] = []
    for evt in outbox_events_raw:
        if isinstance(evt, Mapping):
            outbox_events.append(OutboxEventRecord.from_db_row(evt))
        else:
            raise ValidationError("invalid_outbox_event", "each outbox event must be a mapping")

    return CommittedTurn(
        user_id=user_id,
        request_id=request_id,
        committed_revision=committed_revision,
        user_message_chat_log_id=user_message_chat_log_id,
        assistant_message_chat_log_id=assistant_message_chat_log_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        replay_payload=dict(replay_payload),  # Ensure it's a mutable dict for deep-freeze
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
    # that determine the turn's identity, content, and side effects (outbox, replay)
    canonical_payload = {
        "authenticated_user_id": authenticated_user_id,
        "request_id": request_id,
        "expected_revision": expected_revision,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "emotional_state": emotional_state,
        "relationship_state": relationship_state,
        "public_response": public_response,
        "replay_payload": replay_payload,
        "outbox_events": [
            {"event_type": et, "payload": pl, "idempotency_key": ik}
            for et, pl, ik in outbox_events
        ],
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
