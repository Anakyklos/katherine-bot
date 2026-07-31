"""
Atomic turn commit implementation (#271).

This module provides the transactional commit functionality that atomically:
1. Acquires a per-user lock (64-bit advisory key, database-side)
2. Validates CAS (Compare-And-Swap) on profile revision
3. Creates profile if missing (race-safe)
4. Validates (user_id, request_id) uniqueness
5. Rejects divergent payload for ALL requests (different hash = always
   conflict); reclaims only expired/expired-lease requests with SAME hash
6. Inserts user and assistant messages
7. Updates profile snapshots and increments revision exactly once
8. Completes turn_requests with a reproducible public result
9. Inserts idempotent outbox events
10. Commits all or nothing

The actual atomic work is delegated to a PostgreSQL RPC function
`public.commit_turn` that runs everything in a single transaction.
This Python module provides:
- Pure Python contracts and serialization for the RPC
- Input validation that mirrors database constraints
- Result deserialization into the public CommittedTurn contract

Public result contract (identical for fresh commit and replay):

- user_id, request_id, committed_revision are stable
- replay_payload is the authoritative public result
- user_message_id is derived from request_id
- assistant_message_id comes from replay_payload.message_id
- internal chat_logs references are NOT part of the public contract (they can
  be nulled by pruning, so replay must never depend on them)
- outbox_events expose only stable, immutable references (CommittedOutboxRef);
  operational state (status, attempts, next_attempt_at, leases, updated_at,
  processed_at, error_code) is never returned

Security notes:
- No network calls, LLM calls, or embedding happens inside the transaction
- All user_id values are treated as authenticated; the caller is
  responsible for authentication before invoking this module
- Identity inside snapshots (user_id / bond_label) is rejected at any depth:
  the database never trusts identity provided inside JSON documents
- Payload validation is defense-in-depth: the database enforces the
  true contract, but Python validation prevents accidental leaks
- Unexpected PostgreSQL failures propagate as PersistenceError with a
  sanitized constant message; they are never classified as validation or
  conflict errors
"""

from __future__ import annotations

import json
import re

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

from backend.transactional_schema import (
    FORBIDDEN_PAYLOAD_KEYS,
    REPLAY_PAYLOAD_ALLOWED_KEYS,
    OUTBOX_PAYLOAD_ALLOWED_KEYS,
    canonical_payload_hash,
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

# Snapshot forbidden keys: the base forbidden set PLUS identity fields.
# Identity is never trusted from JSON: user_id and bond_label are rejected at
# any depth (mirrors SQL jsonb_snapshot_contract).
SNAPSHOT_FORBIDDEN_KEYS = FORBIDDEN_PAYLOAD_KEYS | frozenset({"user_id", "bond_label"})

# Minimal fundamental structure per snapshot kind (mirrors SQL
# jsonb_snapshot_contract). The full domain validation lives in the domain
# layer; this only checks that the envelope is structurally coherent.
EMOTIONAL_FUNDAMENTAL_KEYS = frozenset({"pleasure", "arousal", "dominance"})
RELATIONSHIP_FUNDAMENTAL_KEYS = frozenset({"trust", "affection", "tension"})

# Explicit ASCII regexes mirroring the SQL CHECK constraints. str.isalnum()
# accepts Unicode characters that the database rejects, so plain isalnum()
# checks are never used.
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_LEASE_OWNER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# UUID regex (same as database: lowercase hex, canonical form).
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Stable domain conflict codes returned by the RPC.
CONFLICT_CODES = frozenset(
    {
        "revision_mismatch",
        "request_payload_conflict",
        "request_in_progress",
        "lease_conflict",
    }
)


def _is_valid_uuid(value: Any) -> bool:
    return isinstance(value, str) and _UUID_RE.match(value) is not None


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _has_forbidden(obj: Any, forbidden: frozenset[str]) -> bool:
    """Recursively check for any forbidden key at any depth."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k in forbidden or _has_forbidden(v, forbidden):
                return True
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            if _has_forbidden(v, forbidden):
                return True
    return False


@dataclass(frozen=True)
class CommittedOutboxRef:
    """Stable, immutable reference to a persisted outbox event.

    Only these fields are part of the public contract. Operational state
    (status, attempts, next_attempt_at, lease_owner, lease_expires_at,
    updated_at, processed_at, error_code) is never exposed.
    """

    id: str
    event_type: str
    idempotency_key: str
    turn_request_id: str
    contract_version: int


@dataclass(frozen=True)
class CommittedTurn:
    """Public result of a successful atomic turn commit.

    Mirrors the database output of the commit_turn RPC. All server-owned IDs
    and timestamps are populated by the database. The structure is identical
    whether it comes from a fresh commit or an idempotent replay.
    """

    user_id: str
    request_id: str
    committed_revision: int
    user_message_id: str
    assistant_message_id: str
    replay_payload: Mapping[str, Any]
    outbox_events: tuple[CommittedOutboxRef, ...]
    created_at: str
    completed_at: str

    def __post_init__(self) -> None:
        # Deep-freeze nested structures for true immutability
        object.__setattr__(self, "replay_payload", _deep_freeze(self.replay_payload))
        object.__setattr__(self, "outbox_events", tuple(self.outbox_events))

    def to_db_row(self) -> dict[str, Any]:
        """Serialize for database result consumption (public contract only)."""
        return {
            "user_id": self.user_id,
            "request_id": self.request_id,
            "committed_revision": self.committed_revision,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "replay_payload": _deep_unfreeze(self.replay_payload),
            "outbox_events": [
                {
                    "id": ref.id,
                    "event_type": ref.event_type,
                    "idempotency_key": ref.idempotency_key,
                    "turn_request_id": ref.turn_request_id,
                    "contract_version": ref.contract_version,
                }
                for ref in self.outbox_events
            ],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class ConflictError(Exception):
    """Raised when a concurrent modification or in-progress state is detected.

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


class PersistenceError(Exception):
    """Raised when an unexpected database persistence error occurs.

    This is deliberately separate from ConflictError (concurrent modification)
    and ValidationError (input validation). It represents internal failures
    that the caller should escalate/retry with a sanitized constant message;
    no SQLERRM, SQLSTATE, constraint names or payload details are exposed.
    """

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _validate_uuid(value: Any, field_name: str) -> None:
    """Validate that a value is a lowercase hex UUID string."""
    if not isinstance(value, str):
        raise ValidationError(f"invalid_{field_name}", "must be a string")
    if not _UUID_RE.match(value):
        raise ValidationError(f"invalid_{field_name}", "must be a valid lowercase UUID")


def _validate_snapshot_payload(payload: Optional[Mapping[str, Any]], field_name: str) -> None:
    """Validate snapshot payloads against identity/internal forbidden keys.

    Mirrors SQL jsonb_snapshot_contract:
    * non-null must be a JSON object (mapping)
    * schema_version must be an integer exactly 1 (rejects bool, "1", float)
    * user_id / bond_label rejected at any depth
    * prompts / metacognition / messages / internal instructions rejected
    * size limited to 8 KB
    * the minimal fundamental structure for each snapshot kind is required
    """
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValidationError(f"invalid_{field_name}", "must be a mapping or None")

    if "schema_version" not in payload:
        raise ValidationError(
            f"invalid_{field_name}",
            "must contain schema_version field",
        )

    schema_version = payload["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValidationError(
            f"invalid_{field_name}",
            "schema_version must be an integer (not bool, string or float)",
        )
    if schema_version != 1:
        raise ValidationError(
            f"invalid_{field_name}",
            "schema_version must be 1",
        )

    if _has_forbidden(payload, SNAPSHOT_FORBIDDEN_KEYS):
        raise ValidationError(
            f"invalid_{field_name}",
            "contains forbidden keys (identity or internal fields)",
        )

    fundamental = (
        EMOTIONAL_FUNDAMENTAL_KEYS
        if field_name == "emotional_state"
        else RELATIONSHIP_FUNDAMENTAL_KEYS
    )
    missing = fundamental - set(payload.keys())
    if missing:
        raise ValidationError(
            f"invalid_{field_name}",
            f"missing fundamental fields: {', '.join(sorted(missing))}",
        )

    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ValidationError(f"invalid_{field_name}", "not JSON-serializable")

    if len(serialized.encode("utf-8")) > _MAX_REPLAY_PAYLOAD_BYTES:
        raise ValidationError(
            f"invalid_{field_name}",
            f"exceeds {_MAX_REPLAY_PAYLOAD_BYTES} bytes",
        )


def _validate_idempotency_key(key: Any) -> None:
    """Validate an idempotency key with the explicit ASCII regex."""
    if not isinstance(key, str):
        raise ValidationError("invalid_idempotency_key", "must be a string")
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValidationError(
            "invalid_idempotency_key",
            f"must match ^[A-Za-z0-9_.:-]{{1,{_MAX_IDEMPOTENCY_KEY_LENGTH}}}$",
        )


def _validate_lease_owner(owner: Optional[str]) -> None:
    """Validate a lease owner with the explicit ASCII regex."""
    if owner is None:
        return
    if not isinstance(owner, str):
        raise ValidationError("invalid_lease_owner", "must be a string or None")
    if not _LEASE_OWNER_RE.fullmatch(owner):
        raise ValidationError(
            "invalid_lease_owner",
            f"must match ^[A-Za-z0-9_.:-]{{1,{_MAX_LEASE_OWNER_LENGTH}}}$",
        )


def _validate_error_code(code: Optional[str]) -> None:
    """Validate an error code with the explicit ASCII regex."""
    if code is None:
        return
    if not isinstance(code, str):
        raise ValidationError("invalid_error_code", "must be a string or None")
    if not _ERROR_CODE_RE.fullmatch(code):
        raise ValidationError(
            "invalid_error_code",
            f"must match ^[a-z0-9_]{{1,{_MAX_ERROR_CODE_LENGTH}}}$",
        )


def _validate_outbox_event_payload(payload: Mapping[str, Any], event_index: int) -> None:
    """Validate outbox event payload against allowlist, forbidden keys and
    the value contract (mirrors SQL checks)."""
    for key in payload.keys():
        if key not in OUTBOX_PAYLOAD_ALLOWED_KEYS:
            raise ValidationError(
                "invalid_outbox_events",
                f"event {event_index}: payload contains disallowed key '{key}'",
            )

    if _has_forbidden(payload, FORBIDDEN_PAYLOAD_KEYS):
        raise ValidationError(
            "invalid_outbox_events",
            f"event {event_index}: payload contains forbidden keys",
        )

    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ValidationError(
            "invalid_outbox_events",
            f"event {event_index}: payload is not JSON-serializable",
        )
    if len(serialized.encode("utf-8")) > _MAX_OUTBOX_PAYLOAD_BYTES:
        raise ValidationError(
            "invalid_outbox_events",
            f"event {event_index}: payload exceeds {_MAX_OUTBOX_PAYLOAD_BYTES} bytes",
        )

    # Value contract (mirrors SQL jsonb_outbox_payload_value_contract):
    # reference/identifier fields are scalar strings matching the explicit
    # ASCII charset ^[A-Za-z0-9_.:-]{1,128}$ (rejects Unicode accepted by
    # str.isalnum()); version is an integer in [1, 1000].
    for key, value in payload.items():
        if key == "version":
            if not _is_positive_int(value) or value > 1000:
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {event_index}: payload.version must be an integer in 1-1000",
                )
        else:
            if not isinstance(value, str):
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {event_index}: payload.{key} must be a string",
                )
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {event_index}: payload.{key} must match ^[A-Za-z0-9_.:-]{{1,128}}$",
                )


def _validate_replay_payload(payload: Mapping[str, Any]) -> None:
    """Validate replay payload against the allowlist, forbidden keys and the
    required public fields (response and message_id)."""
    if not isinstance(payload, Mapping):
        raise ValidationError("invalid_replay_payload", "must be a mapping")

    if _has_forbidden(payload, FORBIDDEN_PAYLOAD_KEYS):
        raise ValidationError(
            "invalid_replay_payload",
            "contains forbidden keys",
        )

    for key in payload:
        if key not in REPLAY_PAYLOAD_ALLOWED_KEYS:
            raise ValidationError(
                "invalid_replay_payload",
                f"unknown key: {key}",
            )

    response = payload.get("response")
    if not isinstance(response, str):
        raise ValidationError(
            "invalid_replay_payload",
            "must contain a string response field",
        )

    message_id = payload.get("message_id")
    if not _is_valid_uuid(message_id):
        raise ValidationError(
            "invalid_replay_payload",
            "must contain a valid UUID message_id field",
        )

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

    Raises:
        ValidationError: If any input fails validation.
    """
    if not isinstance(authenticated_user_id, str) or not authenticated_user_id:
        raise ValidationError("invalid_user_id", "must be a non-empty string")

    _validate_uuid(request_id, "request_id")

    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
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

    _validate_snapshot_payload(emotional_state, "emotional_state")
    _validate_snapshot_payload(relationship_state, "relationship_state")

    if not isinstance(public_response, str):
        raise ValidationError("invalid_public_response", "must be a string")

    if not isinstance(outbox_events, list):
        raise ValidationError("invalid_outbox_events", "must be a list")

    # Each event must be a sequence of exactly three elements BEFORE unpacking,
    # so shape errors surface as ValidationError, never ValueError.
    for i, event in enumerate(outbox_events):
        if not isinstance(event, (list, tuple)) or len(event) != 3:
            raise ValidationError(
                "invalid_outbox_events",
                f"event {i} must be a sequence of exactly three elements",
            )
        event_type, payload, idempotency_key = event

        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
            raise ValidationError(
                "invalid_outbox_events",
                f"event {i}: event_type must match ^[a-z0-9_]{{1,64}}$",
            )

        if not isinstance(payload, Mapping):
            raise ValidationError(
                "invalid_outbox_events", f"event {i}: payload must be a mapping"
            )

        _validate_outbox_event_payload(payload, i)
        _validate_idempotency_key(idempotency_key)

    _validate_replay_payload(replay_payload)

    # Single authoritative source for the public response: replay_payload.response
    # must equal public_response (mirrors the SQL constraint).
    if replay_payload.get("response") != public_response:
        raise ValidationError(
            "invalid_public_response",
            "public_response must equal replay_payload.response",
        )


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

    Empty mappings are preserved as {} and never implicitly converted to NULL.
    """
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
        "p_request_id": request_id,
        "p_expected_revision": expected_revision,
        "p_user_message": user_message,
        "p_assistant_message": assistant_message,
        "p_payload_hash_sha256": payload_hash_sha256,
        "p_emotional_state": dict(emotional_state) if emotional_state is not None else None,
        "p_relationship_state": dict(relationship_state) if relationship_state is not None else None,
        "p_public_response": public_response,
        "p_replay_payload": dict(replay_payload),
        "p_outbox_events": outbox_array,
        "p_lease_owner": lease_owner,
    }


# Expected field set of a SUCCESS envelope (strict contract validation).
_SUCCESS_FIELDS = frozenset(
    {
        "user_id",
        "request_id",
        "committed_revision",
        "user_message_id",
        "assistant_message_id",
        "replay_payload",
        "outbox_events",
        "created_at",
        "completed_at",
    }
)

# Expected field set of a single outbox reference.
_OUTBOX_REF_FIELDS = frozenset(
    {
        "id",
        "event_type",
        "idempotency_key",
        "turn_request_id",
        "contract_version",
    }
)


def _parse_error_envelope(error_info: Any) -> None:
    """Parse an error envelope and raise the matching exception.

    Domain conflicts raise ConflictError; validation failures raise
    ValidationError; unexpected/unknown failures raise PersistenceError with a
    sanitized constant message (fail closed).
    """
    if not isinstance(error_info, Mapping):
        raise ValidationError("invalid_error_format", "error field must be a mapping")

    code = error_info.get("code")
    if not isinstance(code, str) or not code:
        raise ValidationError("invalid_error_format", "error code must be a non-empty string")

    message = error_info.get("message")
    if not isinstance(message, str):
        message = "internal error"

    expected = error_info.get("expected_revision")
    actual = error_info.get("actual_revision")
    request = error_info.get("request_id")

    expected_int = expected if (isinstance(expected, int) and not isinstance(expected, bool)) else None
    actual_int = actual if (isinstance(actual, int) and not isinstance(actual, bool)) else None
    request_str = request if isinstance(request, str) else None

    if code in CONFLICT_CODES:
        raise ConflictError(
            code=code,
            message=message,
            expected_revision=expected_int if expected_int is not None else 0,
            actual_revision=actual_int,
            request_id=request_str,
        )

    if code == "validation_failed":
        raise ValidationError(code, message)

    # database_error or any unknown/other code: fail closed, sanitized.
    raise PersistenceError("database_error", "persistence error")


def parse_commit_turn_result(result: Mapping[str, Any]) -> CommittedTurn:
    """Parse the result of the commit_turn RPC into a CommittedTurn.

    The success envelope is validated strictly: exactly the expected fields
    must be present (missing/extra fields indicate contract corruption), types
    are checked (bool is rejected for integer fields), revisions and IDs must
    be non-negative, and UUID identities are validated.

    Raises:
        ConflictError: If the result indicates a conflict or in-progress state.
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

    user_id = result["user_id"]
    request_id = result["request_id"]
    committed_revision = result["committed_revision"]
    user_message_id = result["user_message_id"]
    assistant_message_id = result["assistant_message_id"]
    replay_payload = result["replay_payload"]
    outbox_events_raw = result["outbox_events"]
    created_at = result["created_at"]
    completed_at = result["completed_at"]

    if not isinstance(user_id, str) or not user_id:
        raise ValidationError("invalid_user_id", "user_id must be a non-empty string")

    _validate_uuid(request_id, "request_id")

    if isinstance(committed_revision, bool) or not isinstance(committed_revision, int):
        raise ValidationError(
            "invalid_committed_revision", "committed_revision must be an integer"
        )
    if committed_revision < 0:
        raise ValidationError(
            "invalid_committed_revision", "committed_revision must be non-negative"
        )

    # user_message_id is derived from request_id: coherence is mandatory.
    if not isinstance(user_message_id, str) or user_message_id != request_id:
        raise ValidationError(
            "invalid_user_message_id", "user_message_id must equal request_id"
        )

    # assistant_message_id comes from the public identifier persisted in
    # replay_payload.message_id: coherence is mandatory.
    if not _is_valid_uuid(assistant_message_id):
        raise ValidationError(
            "invalid_assistant_message_id", "assistant_message_id must be a valid UUID"
        )

    if not isinstance(replay_payload, Mapping):
        raise ValidationError("invalid_replay_payload", "replay_payload must be a mapping")
    _validate_replay_payload(replay_payload)
    if replay_payload.get("message_id") != assistant_message_id:
        raise ValidationError(
            "invalid_assistant_message_id",
            "assistant_message_id must equal replay_payload.message_id",
        )

    if not isinstance(outbox_events_raw, list):
        raise ValidationError("invalid_outbox_events", "outbox_events must be a list")

    outbox_refs: list[CommittedOutboxRef] = []
    for evt in outbox_events_raw:
        if not isinstance(evt, Mapping):
            raise ValidationError("invalid_outbox_event", "each outbox event must be a mapping")
        evt_missing = _OUTBOX_REF_FIELDS - set(evt.keys())
        if evt_missing:
            raise ValidationError(
                "invalid_outbox_event", f"missing outbox ref field: {sorted(evt_missing)[0]}"
            )
        evt_extra = set(evt.keys()) - _OUTBOX_REF_FIELDS
        if evt_extra:
            raise ValidationError(
                "invalid_outbox_event", f"unexpected outbox ref field: {sorted(evt_extra)[0]}"
            )

        ref_id = evt["id"]
        event_type = evt["event_type"]
        idempotency_key = evt["idempotency_key"]
        turn_request_id = evt["turn_request_id"]
        contract_version = evt["contract_version"]

        if not _is_valid_uuid(ref_id):
            raise ValidationError("invalid_outbox_event", "outbox ref id must be a valid UUID")
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
            raise ValidationError("invalid_outbox_event", "outbox ref event_type is invalid")
        _validate_idempotency_key(idempotency_key)
        if not _is_valid_uuid(turn_request_id):
            raise ValidationError(
                "invalid_outbox_event", "outbox ref turn_request_id must be a valid UUID"
            )
        if not _is_positive_int(contract_version):
            raise ValidationError(
                "invalid_outbox_event", "outbox ref contract_version must be a positive integer"
            )

        outbox_refs.append(
            CommittedOutboxRef(
                id=ref_id,
                event_type=event_type,
                idempotency_key=idempotency_key,
                turn_request_id=turn_request_id,
                contract_version=contract_version,
            )
        )

    if not isinstance(created_at, str) or not created_at:
        raise ValidationError("invalid_created_at", "created_at must be a non-empty string")
    if not isinstance(completed_at, str) or not completed_at:
        raise ValidationError("invalid_completed_at", "completed_at must be a non-empty string")

    return CommittedTurn(
        user_id=user_id,
        request_id=request_id,
        committed_revision=committed_revision,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        replay_payload=dict(replay_payload),
        outbox_events=outbox_refs,
        created_at=created_at,
        completed_at=completed_at,
    )


# Type alias for the RPC function callable
# The actual implementation depends on the database client being used
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

    Validation runs BEFORE the RPC is invoked, so invalid input never reaches
    the database. The RPC is awaited exactly once. Unexpected persistence
    failures raised by the RPC client are converted to PersistenceError with a
    sanitized constant message.

    Args:
        rpc_client: An awaitable callable ``rpc_client(name, params) -> Mapping``.
        All other parameters match validate_atomic_commit_input.

    Returns:
        A CommittedTurn instance with the public committed data.

    Raises:
        ValidationError: If input validation fails (RPC is NOT called).
        ConflictError: If a concurrent modification or in-progress state is detected.
        PersistenceError: If an unexpected persistence failure occurs.
    """
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

    if lease_owner is not None:
        _validate_lease_owner(lease_owner)

    # The canonical payload hash covers every input that determines the turn's
    # identity, content and side effects (outbox + replay).
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

    try:
        result = await rpc_client("commit_turn", rpc_payload)
    except Exception as exc:  # noqa: BLE001 - sanitize any RPC-level failure
        # Unexpected persistence failure: never expose the underlying error.
        raise PersistenceError("database_error", "persistence error") from None

    return parse_commit_turn_result(result)
