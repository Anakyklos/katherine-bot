"""Payload contracts for the local SQLite store (#335).

These validators enforce, at the local store boundary, the same invariants
the PostgreSQL transactional schema enforces for the web runtime
(``backend.transactional_schema`` / ``backend.atomic_turn_commit``), adapted
to the single-user desktop context:

- **canonical serialization**: deterministic JSON (sorted keys, compact
  separators, UTF-8, ``allow_nan=False``) for hashing and storage;
- **replay payload**: public allowlist, forbidden keys at any depth, finite
  JSON, explicit byte bound;
- **outbox events**: shape, event type, idempotency key, payload allowlist,
  forbidden keys at any depth, value types, finite JSON, explicit byte bound
  — the outbox carries references, never conversation content;
- **snapshots**: exact v1 domain structure validated by the real domain
  models (``EmotionalStateV1`` / ``RelationshipStateV1``), which already
  reject identity fields, internal content, unknown keys, out-of-range
  values, non-finite numbers and unbounded size;
- **neutral snapshots**: the canonical neutral v1 snapshot produced by the
  domain constructors is the only accepted reset payload.

The module is deliberately local to the storage package: it must not import
the Supabase/HTTP stack, and importing it never triggers network access.
Domain models are imported lazily inside functions so importing the package
contract surface stays cheap and dependency-free.

All errors are stable ``code`` + constant ``message`` pairs (the local
``ValidationError``): a rejected value, key or payload content is never
echoed into the error.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .errors import ValidationError

# ---------------------------------------------------------------------------
# Bounds (mirror the web contract sizes; identical semantics locally)
# ---------------------------------------------------------------------------

# Maximum serialized payload size in bytes (UTF-8), applied to the replay
# payload, each outbox event payload, and each snapshot.
# The web contract bounds replay_payload and outbox payloads at 8192 bytes
# (``atomic_turn_commit`` / SQL CHECK). Snapshots have no web byte bound;
# locally they are bounded at the same 8192 bytes. This comfortably admits
# every domain-valid snapshot: the largest possible relationship snapshot
# (32 triggers x 128 chars, ~4.3 KB canonical) validates.
MAX_REPLAY_PAYLOAD_BYTES = 8192
MAX_OUTBOX_PAYLOAD_BYTES = 8192
MAX_SNAPSHOT_BYTES = 8192

# Outbox event identifiers.
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_EVENT_TYPE_LENGTH = 64

# Maximum number of outbox events accepted per turn commit.
MAX_OUTBOX_EVENTS = 32

# Explicit ASCII regexes (``str.isalnum`` accepts Unicode the web contract
# rejects; the local contract keeps the same charset).
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# ---------------------------------------------------------------------------
# Allowlists and forbidden keys (parity with transactional_schema)
# ---------------------------------------------------------------------------

# Keys that may never appear inside a stored payload document, at any depth.
# ``message`` / ``user_message`` / ``assistant_message`` / ``content`` are
# forbidden so the outbox and replay payloads can never duplicate prompts or
# conversation content.
FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "prompt",
        "system_prompt",
        "meta_cognition",
        "internal_instructions",
        "message",
        "user_message",
        "assistant_message",
        "content",
    }
)

# Local snapshot forbidden keys: the base forbidden set plus identity fields.
# Identity is never trusted from JSON documents.
SNAPSHOT_FORBIDDEN_KEYS = FORBIDDEN_PAYLOAD_KEYS | frozenset(
    {"user_id", "bond_label"}
)

# Explicit allowlist of top-level keys for ``turn_requests.replay_payload``.
# Only public result fields are permitted.
REPLAY_PAYLOAD_ALLOWED_KEYS = frozenset(
    {"response", "emotion_state", "message_id", "request_id", "duration_ms"}
)

# Explicit allowlist of top-level keys for ``outbox_events.payload``.
# Event payloads carry only stable references and public event metadata.
OUTBOX_PAYLOAD_ALLOWED_KEYS = frozenset(
    {"ref", "request_id", "turn_id", "message_id", "entity_id", "kind", "version"}
)


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Serialize deterministically: sorted keys, compact separators, UTF-8.

    ``allow_nan=False``: ``NaN`` / ``Infinity`` / ``-Infinity`` are rejected
    (not interoperable JSON; hashing them would break idempotency). Raises
    the sanitized ``ValidationError`` — the offending value is never echoed.
    """
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValidationError(
            "invalid_payload", "payload is not canonical JSON serializable"
        ) from None


def _require_canonical_bytes(value: Any, code: str, max_bytes: int) -> None:
    """Reject a value that is not finite JSON or exceeds the byte bound.

    Non-finite floats (``NaN`` / ``Infinity``) are rejected with the same
    stable ``code`` the calling validator uses, so callers see one error
    contract per payload kind.
    """
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValidationError(code, "payload is not finite JSON") from None
    if len(serialized.encode("utf-8")) > max_bytes:
        raise ValidationError(code, f"payload exceeds {max_bytes} bytes")


def _has_forbidden(obj: Any, forbidden: frozenset[str]) -> bool:
    """Recursively detect any forbidden key at any depth."""
    if isinstance(obj, Mapping):
        for key, nested in obj.items():
            if key in forbidden or _has_forbidden(nested, forbidden):
                return True
    elif isinstance(obj, (list, tuple)):
        for nested in obj:
            if _has_forbidden(nested, forbidden):
                return True
    return False


# ---------------------------------------------------------------------------
# Replay payload
# ---------------------------------------------------------------------------


def validate_replay_payload(payload: Any) -> Mapping[str, Any]:
    """Validate the public replay payload of one turn.

    Requirements:

    - must be a ``Mapping``;
    - only allowlisted public keys;
    - no forbidden key at any depth (prompts, raw messages, meta-cognition,
      internal instructions, hidden ``content``);
    - ``response`` is required and must be a string;
    - ``message_id`` is required (web parity: the replay payload always
      identifies the committed assistant message). Locally the message id
      is a SQLite rowid, so an integer or a bounded identifier string is
      accepted (the web UUID requirement assumes uuid PKs, which the
      local schema does not use);
    - ``request_id``, when present, must be a bounded identifier string
      and callers cross-check it against the enclosing turn request;
    - finite JSON within the explicit byte bound.
    """
    if not isinstance(payload, Mapping):
        raise ValidationError("invalid_replay_payload", "replay payload must be a mapping")
    if _has_forbidden(payload, FORBIDDEN_PAYLOAD_KEYS):
        raise ValidationError("invalid_replay_payload", "replay payload contains forbidden keys")
    unknown = set(payload) - REPLAY_PAYLOAD_ALLOWED_KEYS
    if unknown:
        raise ValidationError("invalid_replay_payload", "replay payload contains unknown keys")
    response = payload.get("response")
    if not isinstance(response, str):
        raise ValidationError(
            "invalid_replay_payload", "replay payload requires a string response field"
        )
    message_id = payload.get("message_id")
    if isinstance(message_id, bool) or not (
        isinstance(message_id, int)
        or (isinstance(message_id, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", message_id))
    ):
        raise ValidationError(
            "invalid_replay_payload",
            "replay payload requires a message_id (integer rowid or bounded identifier)",
        )
    if "request_id" in payload:
        request_id = payload["request_id"]
        if isinstance(request_id, bool) or not (
            isinstance(request_id, str)
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id)
        ):
            raise ValidationError(
                "invalid_replay_payload",
                "replay payload request_id must be a bounded identifier string",
            )
    _require_canonical_bytes(
        payload, "invalid_replay_payload", MAX_REPLAY_PAYLOAD_BYTES
    )
    return payload


# ---------------------------------------------------------------------------
# Outbox events
# ---------------------------------------------------------------------------


def _validate_idempotency_key(key: Any, index: int) -> None:
    if not isinstance(key, str):
        raise ValidationError("invalid_outbox_events", "idempotency key must be a string")
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValidationError(
            "invalid_outbox_events", "idempotency key charset or length is invalid"
        )


def _validate_outbox_event(event: Any, index: int) -> tuple[str, Mapping[str, Any], str]:
    """Validate one outbox event ``(event_type, payload, idempotency_key)``."""
    if not isinstance(event, (list, tuple)) or len(event) != 3:
        raise ValidationError(
            "invalid_outbox_events", f"event {index} must be a triple"
        )
    event_type, payload, idempotency_key = event
    if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
        raise ValidationError(
            "invalid_outbox_events", f"event {index}: event_type charset or length is invalid"
        )
    if not isinstance(payload, Mapping):
        raise ValidationError(
            "invalid_outbox_events", f"event {index}: payload must be a mapping"
        )
    _validate_idempotency_key(idempotency_key, index)

    unknown = set(payload) - OUTBOX_PAYLOAD_ALLOWED_KEYS
    if unknown:
        raise ValidationError(
            "invalid_outbox_events", f"event {index}: payload contains unknown keys"
        )
    if _has_forbidden(payload, FORBIDDEN_PAYLOAD_KEYS):
        raise ValidationError(
            "invalid_outbox_events", f"event {index}: payload contains forbidden keys"
        )

    # Value contract: reference/identifier fields are bounded scalar strings
    # in the explicit ASCII charset; ``version`` is an integer in [1, 1000].
    # This is what guarantees the outbox carries references, never content.
    for key, value in payload.items():
        if key == "version":
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 1000
            ):
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {index}: payload.version must be an integer in 1-1000",
                )
        else:
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", value
            ):
                raise ValidationError(
                    "invalid_outbox_events",
                    f"event {index}: payload.{key} must be a bounded ASCII reference string",
                )

    _require_canonical_bytes(
        payload, "invalid_outbox_events", MAX_OUTBOX_PAYLOAD_BYTES
    )
    return event_type, payload, idempotency_key


def validate_outbox_events(
    events: Any,
) -> list[tuple[str, Mapping[str, Any], str]]:
    """Validate the outbox events of one turn commit (shape + payload)."""
    if events is None:
        return []
    if not isinstance(events, (list, tuple)):
        raise ValidationError("invalid_outbox_events", "outbox events must be a list")
    if len(events) > MAX_OUTBOX_EVENTS:
        raise ValidationError(
            "invalid_outbox_events", f"at most {MAX_OUTBOX_EVENTS} outbox events per turn"
        )
    validated = [
        _validate_outbox_event(event, index) for index, event in enumerate(events)
    ]
    keys = [key for _, _, key in validated]
    if len(keys) != len(set(keys)):
        raise ValidationError(
            "invalid_outbox_events", "idempotency keys must be unique within a turn"
        )
    return validated


# ---------------------------------------------------------------------------
# Domain snapshots
# ---------------------------------------------------------------------------


def validate_emotional_snapshot(payload: Any) -> Mapping[str, Any]:
    """Validate an emotional snapshot through the real domain model.

    The domain model rejects unknown keys, missing keys, identity/internal
    content, out-of-range values, non-finite numbers and bool/str numerics.
    The byte bound guards unbounded growth. This reuses the single existing
    definition of a valid v1 snapshot instead of duplicating the rules.
    """
    from backend.emotional_domain import EmotionalDomainError, EmotionalStateV1

    if not isinstance(payload, Mapping):
        raise ValidationError("invalid_emotional_state", "snapshot must be a mapping")
    if _has_forbidden(payload, SNAPSHOT_FORBIDDEN_KEYS):
        raise ValidationError(
            "invalid_emotional_state", "snapshot contains forbidden keys"
        )
    _require_canonical_bytes(payload, "invalid_emotional_state", MAX_SNAPSHOT_BYTES)
    try:
        EmotionalStateV1.from_dict(dict(payload))
    except EmotionalDomainError:
        raise ValidationError(
            "invalid_emotional_state", "snapshot does not match the v1 domain contract"
        ) from None
    return payload


def validate_relationship_snapshot(payload: Any) -> Mapping[str, Any]:
    """Validate a relationship snapshot through the real domain model."""
    from backend.emotional_domain import EmotionalDomainError
    from backend.relationship import RelationshipDomainError, RelationshipStateV1

    if not isinstance(payload, Mapping):
        raise ValidationError("invalid_relationship_state", "snapshot must be a mapping")
    if _has_forbidden(payload, SNAPSHOT_FORBIDDEN_KEYS):
        raise ValidationError(
            "invalid_relationship_state", "snapshot contains forbidden keys"
        )
    _require_canonical_bytes(
        payload, "invalid_relationship_state", MAX_SNAPSHOT_BYTES
    )
    try:
        RelationshipStateV1.from_dict(dict(payload))
    except (EmotionalDomainError, RelationshipDomainError):
        raise ValidationError(
            "invalid_relationship_state",
            "snapshot does not match the v1 domain contract",
        ) from None
    return payload


# ---------------------------------------------------------------------------
# Neutral snapshots (canonical reset payloads)
# ---------------------------------------------------------------------------


def validate_neutral_emotional_snapshot(payload: Any) -> Mapping[str, Any]:
    """Require the canonical neutral v1 emotional snapshot produced by the domain.

    Neutrality is produced, not reimplemented: the expected snapshot is built
    through ``EmotionalStateV1.neutral`` with the payload's own timestamp and
    must match exactly. A structurally valid v1 snapshot with non-neutral
    values is rejected.
    """
    from backend.emotional_domain import EmotionalStateV1

    payload = validate_emotional_snapshot(payload)
    timestamp = payload.get("timestamp")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or timestamp <= 0
    ):
        raise ValidationError(
            "invalid_reset_payload", "reset snapshot must be the canonical neutral state"
        )
    expected = EmotionalStateV1.neutral(timestamp=float(timestamp)).to_dict()
    if dict(payload) != expected:
        raise ValidationError(
            "invalid_reset_payload", "reset snapshot must be the canonical neutral state"
        )
    return payload


def validate_neutral_relationship_snapshot(payload: Any) -> Mapping[str, Any]:
    """Require the canonical neutral v1 relationship snapshot produced by the domain."""
    from backend.relationship import RelationshipStateV1

    payload = validate_relationship_snapshot(payload)
    timestamp = payload.get("timestamp")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or timestamp <= 0
    ):
        raise ValidationError(
            "invalid_reset_payload", "reset snapshot must be the canonical neutral state"
        )
    expected = RelationshipStateV1.neutral(timestamp=float(timestamp)).to_dict()
    if dict(payload) != expected:
        raise ValidationError(
            "invalid_reset_payload", "reset snapshot must be the canonical neutral state"
        )
    return payload


def neutral_emotional_snapshot(timestamp: float) -> dict[str, Any]:
    """Build the canonical neutral v1 emotional snapshot (domain-produced)."""
    from backend.emotional_domain import EmotionalStateV1

    return EmotionalStateV1.neutral(timestamp=timestamp).to_dict()


def neutral_relationship_snapshot(timestamp: float) -> dict[str, Any]:
    """Build the canonical neutral v1 relationship snapshot (domain-produced)."""
    from backend.relationship import RelationshipStateV1

    return RelationshipStateV1.neutral(timestamp=timestamp).to_dict()


# ---------------------------------------------------------------------------
# Canonical commit payload (idempotency hash input)
# ---------------------------------------------------------------------------


def build_canonical_commit_payload(
    *,
    request_id: str,
    expected_revision: int | None,
    user_message: str,
    assistant_message: str,
    emotional_state: Mapping[str, Any],
    relationship_state: Mapping[str, Any],
    public_response: str,
    replay_payload: Mapping[str, Any],
    outbox_events: list[tuple[str, Mapping[str, Any], str]],
) -> dict[str, Any]:
    """Build the canonical payload hashed for ``payload_hash_sha256``.

    Covers every input that determines the turn's identity, content and
    effects — the same rule the web contract uses (``build_canonical_commit_
    payload`` in ``backend.atomic_turn_commit``), minus cloud identity fields
    that do not exist locally.
    """
    return {
        "request_id": request_id,
        "expected_revision": expected_revision,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "emotional_state": dict(emotional_state),
        "relationship_state": dict(relationship_state),
        "public_response": public_response,
        "replay_payload": dict(replay_payload),
        "outbox_events": [
            {"event_type": event_type, "payload": dict(payload), "idempotency_key": key}
            for event_type, payload, key in outbox_events
        ],
    }


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Compute the canonical SHA-256 hex digest of a payload."""
    import hashlib

    canonical = canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
