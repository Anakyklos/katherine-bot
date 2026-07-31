"""
Minimal serialization contracts for the transactional turn schema (#270).

This module is the only place where the canonical payload hash and the
typed record shapes for the new server-owned tables are defined. It is
pure Python (standard library only) and must be importable without:

- FastAPI / Pydantic
- Groq SDK
- Supabase / PostgREST
- sentence_transformers
- ``ConversationEngine``, ``memory``, ``engine`` or any other backend module
- environment variables
- network or filesystem access
- clock or randomness
- any global user-specific state

The database is the source of truth for *validation*: statuses, leases,
attempts and payload size bounds are enforced by CHECK constraints in
``supabase/migrations/20240101000004_transactional_turn_schema.sql``.
These models never re-implement those rules; they only serialize and
deserialize records so the persistence contract can be tested.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------
# Canonical payload hash
# ---------------------------------------------------------------------------

#: Keys that may never appear inside a stored payload document. Mirrors the
#: forbidden-key lists enforced recursively by the migration CHECK
#: constraints; kept here so the serialization contract is testable without
#: a database. ``message`` / ``user_message`` / ``assistant_message`` /
#: ``content`` are forbidden because the outbox must never duplicate
#: messages or prompts.
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

#: Explicit allowlist of top-level keys for ``turn_requests.replay_payload``.
#: Mirrors the SQL CHECK ``jsonb_keys_subset_of`` allowlist. Only public
#: result fields are permitted — never prompts or internal state.
REPLAY_PAYLOAD_ALLOWED_KEYS = frozenset(
    {"response", "emotion_state", "message_id", "request_id", "duration_ms"}
)

#: Explicit allowlist of top-level keys for ``outbox_events.payload``.
#: Mirrors the SQL CHECK ``jsonb_keys_subset_of`` allowlist. Event payloads
#: carry only stable references and public event metadata.
OUTBOX_PAYLOAD_ALLOWED_KEYS = frozenset(
    {"ref", "request_id", "turn_id", "message_id", "entity_id", "kind", "version"}
)


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    """Compute the canonical SHA-256 hash of a request payload.

    The digest is computed over the canonical JSON serialization of
    *payload*: keys sorted, compact separators (``,`` / ``:``), and
    ``ensure_ascii=False`` — the same canonical JSON convention used by
    ``backend/trusted_context.py`` and ``backend/memory.py``.

    ``allow_nan=False``: ``NaN``/``Infinity``/``-Infinity`` are rejected
    because they are not interoperable JSON and ``jsonb`` refuses them;
    hashing them would break idempotency for values the database would
    reject.

    Returns a lowercase 64-character hexadecimal digest that satisfies the
    database CHECK ``payload_hash_sha256 ~ '^[0-9a-f]{64}$'``.

    Raises:
        TypeError: If *payload* is not a mapping or contains values that
            cannot be JSON-serialized (including non-finite floats). The
            offending value is never included in the exception message.
    """
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    try:
        canonical = json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TypeError("payload is not JSON-serializable") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Deep immutability helpers
# ---------------------------------------------------------------------------
# The dataclasses are frozen, but a frozen dataclass only prevents attribute
# reassignment. To make ``payload`` / ``replay_payload`` truly immutable we
# deep-freeze them on construction (MappingProxyType for mappings, tuples
# for sequences) and deep-unfreeze them back to JSON-serializable plain
# structures in ``to_db_row``. Two records built from the same source dict
# therefore never share mutable state, and callers can never mutate a
# record's payload after the fact.


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    return value


def _deep_unfreeze(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {k: _deep_unfreeze(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_deep_unfreeze(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Turn request record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnRequestRecord:
    """Typed view of a ``public.turn_requests`` row.

    Fields mirror the database columns. Server-owned columns (``id``,
    ``created_at``, ``updated_at``, ``completed_at``) are optional: when
    serializing an insert, ``None`` fields are omitted so the database
    defaults apply.
    """

    user_id: str
    request_id: str
    payload_hash_sha256: str
    status: str
    expected_revision: int = 0
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    committed_revision: Optional[int] = None
    user_message_chat_log_id: Optional[int] = None
    assistant_message_chat_log_id: Optional[int] = None
    replay_payload: Optional[Mapping[str, Any]] = None
    error_code: Optional[str] = None
    id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.replay_payload is not None:
            object.__setattr__(self, "replay_payload", _deep_freeze(self.replay_payload))

    def to_db_row(self) -> dict[str, Any]:
        """Serialize to a database row, omitting ``None`` fields.

        Omitting ``None`` lets the database apply its defaults for
        server-owned columns and keeps nullable columns nullable without
        explicit NULL payloads.
        """
        return {
            "user_id": self.user_id,
            "request_id": self.request_id,
            "payload_hash_sha256": self.payload_hash_sha256,
            "status": self.status,
            "expected_revision": self.expected_revision,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "committed_revision": self.committed_revision,
            "user_message_chat_log_id": self.user_message_chat_log_id,
            "assistant_message_chat_log_id": self.assistant_message_chat_log_id,
            "replay_payload": (
                _deep_unfreeze(self.replay_payload)
                if self.replay_payload is not None
                else None
            ),
            "error_code": self.error_code,
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_db_row(cls, row: Mapping[str, Any]) -> "TurnRequestRecord":
        """Build a record from a database row (missing keys default to None)."""
        return cls(
            user_id=row["user_id"],
            request_id=row["request_id"],
            payload_hash_sha256=row["payload_hash_sha256"],
            status=row["status"],
            expected_revision=row.get("expected_revision", 0),
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            committed_revision=row.get("committed_revision"),
            user_message_chat_log_id=row.get("user_message_chat_log_id"),
            assistant_message_chat_log_id=row.get("assistant_message_chat_log_id"),
            replay_payload=row.get("replay_payload"),
            error_code=row.get("error_code"),
            id=row.get("id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            completed_at=row.get("completed_at"),
        )

    def to_insert_row(self) -> dict[str, Any]:
        """Serialize for INSERT, dropping server-owned ``None`` columns.

        Only the columns a server client is allowed to set are emitted;
        ``id``, ``created_at``, ``updated_at`` and ``completed_at`` are
        omitted when ``None`` so the database defaults and triggers own
        them.
        """
        row = self.to_db_row()
        for server_owned in ("id", "created_at", "updated_at", "completed_at"):
            if row[server_owned] is None:
                del row[server_owned]
        return row


# ---------------------------------------------------------------------------
# Outbox event record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxEventRecord:
    """Typed view of a ``public.outbox_events`` row.

    Mirrors the database columns. Server-owned columns are optional and
    omitted from INSERT serialization when ``None``.
    """

    event_type: str
    user_id: str
    payload: Mapping[str, Any]
    status: str
    idempotency_key: str
    contract_version: int = 1
    attempts: int = 0
    turn_request_id: Optional[str] = None
    next_attempt_at: Optional[str] = None
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    error_code: Optional[str] = None
    id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    processed_at: Optional[str] = None
    dead_lettered_at: Optional[str] = None
    retention_until: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _deep_freeze(self.payload))

    def to_db_row(self) -> dict[str, Any]:
        """Serialize to a database row, omitting ``None`` fields."""
        return {
            "event_type": self.event_type,
            "user_id": self.user_id,
            "payload": _deep_unfreeze(self.payload),
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "contract_version": self.contract_version,
            "attempts": self.attempts,
            "turn_request_id": self.turn_request_id,
            "next_attempt_at": self.next_attempt_at,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "error_code": self.error_code,
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "processed_at": self.processed_at,
            "dead_lettered_at": self.dead_lettered_at,
            "retention_until": self.retention_until,
        }

    @classmethod
    def from_db_row(cls, row: Mapping[str, Any]) -> "OutboxEventRecord":
        """Build a record from a database row (missing keys default)."""
        return cls(
            event_type=row["event_type"],
            user_id=row["user_id"],
            payload=row["payload"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            contract_version=row.get("contract_version", 1),
            attempts=row.get("attempts", 0),
            turn_request_id=row.get("turn_request_id"),
            next_attempt_at=row.get("next_attempt_at"),
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            error_code=row.get("error_code"),
            id=row.get("id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            processed_at=row.get("processed_at"),
            dead_lettered_at=row.get("dead_lettered_at"),
            retention_until=row.get("retention_until"),
        )

    def to_insert_row(self) -> dict[str, Any]:
        """Serialize for INSERT, dropping server-owned ``None`` columns."""
        row = self.to_db_row()
        for server_owned in (
            "id",
            "created_at",
            "updated_at",
            "processed_at",
            "dead_lettered_at",
            "retention_until",
        ):
            if row[server_owned] is None:
                del row[server_owned]
        return row
