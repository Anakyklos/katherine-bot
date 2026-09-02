"""Explicit legacy-installation import into the local SQLite store.

#336 (issue test 12: "importação de fixture legado não duplica
dados"): when an existing Supabase installation needs to be
preserved, the migration is an explicit export/import — never an
automatic sync — and it is IDEMPOTENT: importing the same fixture
twice never duplicates rows.

Scope of this module (deliberately narrow):

- the input is a validated, structural fixture (see
  ``validate_legacy_fixture``): counts, versions and rows — no
  secrets, no tokens, no service-role material;
- the target is the REAL local SQLite schema (chat_logs,
  turn_requests, profiles) via the same transactional discipline as
  the runtime (``BEGIN IMMEDIATE`` … ``COMMIT``);
- the source is never modified or deleted (the issue forbids
  auto-deleting the origin);
- only structural evidence is returned (counts); message content
  never crosses to logs or errors.

Idempotency mechanism: legacy rows carry a stable ``legacy_id``. The
import writes each row with a deterministic request id derived from
that legacy identity, so a second import of the same fixture maps to
the SAME turn_requests primary key and replays (returns the existing
committed turn) instead of duplicating.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .contracts import FORBIDDEN_PAYLOAD_KEYS
from .errors import PersistenceError, ValidationError
from .storage import LocalStorage

LEGACY_SCHEMA_VERSION = 1
MAX_LEGACY_MESSAGES = 100_000
MAX_LEGACY_CONTENT_CHARS = 20_000

# The fixture's own message fields are legitimate here. The payload
# contract forbids them because a runtime replay payload must never
# duplicate raw messages; a legacy IMPORT fixture is exactly the
# sanctioned transfer channel for historical messages. Only the
# actually dangerous keys stay forbidden:
_FIXTURE_FORBIDDEN_KEYS = FORBIDDEN_PAYLOAD_KEYS - {
    "user_message",
    "assistant_message",
    "content",
    "message",
}


@dataclass(frozen=True)
class ImportReport:
    """Structural evidence only — no message content."""

    imported_turns: int
    skipped_duplicates: int
    total_turns_after: int


def validate_legacy_fixture(fixture: Any) -> dict[str, Any]:
    """Validate the structural shape of a legacy export fixture.

    Expected shape (schema version 1)::

        {
          "schema_version": 1,
          "source": "supabase",
          "turns": [
            {
              "request_id": "…",            # stable legacy identity
              "user_message": "…",
              "assistant_message": "…",
              "created_at": "…",            # ISO-8601, optional
              "replay_payload": {...}       # optional
            },
            ...
          ]
        }

    Rejections (fail-closed, no partial import):

    - not a mapping / wrong schema version / unknown source;
    - missing or non-string request ids or messages;
    - empty turns must still be a list (import is a no-op then);
    - any forbidden key anywhere in the fixture (prompts, tokens,
      secrets) — same guard as the runtime replay contract.
    """
    if not isinstance(fixture, Mapping):
        raise ValidationError("invalid_legacy_fixture", "fixture must be a mapping")
    f = dict(fixture)
    if f.get("schema_version") != LEGACY_SCHEMA_VERSION:
        raise ValidationError(
            "invalid_legacy_fixture_version",
            "unsupported legacy fixture schema version",
        )
    if f.get("source") != "supabase":
        raise ValidationError(
            "invalid_legacy_source",
            "only supabase legacy exports are importable",
        )
    turns = f.get("turns")
    if not isinstance(turns, list):
        raise ValidationError("invalid_legacy_turns", "turns must be a list")
    if len(turns) > MAX_LEGACY_MESSAGES:
        raise ValidationError(
            "legacy_fixture_too_large", "legacy fixture exceeds size bound"
        )
    _assert_no_forbidden_keys(f)
    for turn in turns:
        if not isinstance(turn, Mapping):
            raise ValidationError("invalid_legacy_turn", "turn must be a mapping")
        t = dict(turn)
        request_id = t.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValidationError(
                "invalid_legacy_request_id", "turn request id must be a string"
            )
        for field in ("user_message", "assistant_message"):
            value = t.get(field)
            if not isinstance(value, str) or not value:
                raise ValidationError(
                    "invalid_legacy_message", "turn message must be a non-empty string"
                )
            if len(value) > MAX_LEGACY_CONTENT_CHARS:
                raise ValidationError(
                    "legacy_message_too_long", "turn message exceeds size bound"
                )
        created_at = t.get("created_at")
        if created_at is not None and not isinstance(created_at, str):
            raise ValidationError(
                "invalid_legacy_created_at", "created_at must be a string when present"
            )
        _assert_no_forbidden_keys(t)
    return f


def _assert_no_forbidden_keys(value: Any) -> None:
    """Fail-closed forbidden-key guard (deep, bounded by fixture size)."""
    if isinstance(value, Mapping):
        for key in value.keys():
            if not isinstance(key, str):
                continue
            for forbidden in _FIXTURE_FORBIDDEN_KEYS:
                if key.lower() == forbidden.lower():
                    raise ValidationError(
                        "forbidden_key_in_legacy_fixture",
                        "fixture contains a forbidden key",
                    )
        for item in value.values():
            _assert_no_forbidden_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_forbidden_keys(item)


def import_legacy_fixture(
    store: LocalStorage, fixture: Any
) -> ImportReport:
    """Import a validated legacy fixture into the local store.

    Idempotent by construction: each legacy turn is written under its
    stable legacy request id; re-importing the same fixture hits the
    same primary keys, verifies the persisted turn is IDENTICAL, and
    skips (counted as duplicates) instead of duplicating rows.

    #336 review blocker 2 (divergent collision ≠ duplicate): a
    request id that already exists with DIFFERENT content is NOT
    idempotent replay — it is an undetected collision that would
    silently preserve state diverging from the source. For an
    existing key the importer now compares the canonical content
    hash (the same structural hash persisted at import time):
    identical fixture → idempotent skip; divergent content →
    ``ValidationError`` and the whole import rolls back, leaving
    the store bit-identical to its previous state.

    The import is one transaction — a failure leaves the store
    untouched.
    """
    f = validate_legacy_fixture(fixture)
    turns = f["turns"]

    with store._lock:
        conn = store._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            imported = 0
            skipped = 0
            for turn in turns:
                t = dict(turn)
                request_id = t["request_id"]
                existing = conn.execute(
                    "select payload_hash_sha256 from turn_requests "
                    "where request_id = ?",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    if existing[0] != _legacy_hash(t):
                        # Divergent content under the same stable
                        # legacy id: an ambiguous collision, never a
                        # duplicate. Fail-closed — the store keeps the
                        # state it already had (rollback below).
                        raise ValidationError(
                            "legacy_request_id_conflict",
                            "fixture request id conflicts with a different "
                            "already-imported turn",
                        )
                    # Idempotency: byte-identical re-import — skip,
                    # never duplicate.
                    skipped += 1
                    continue
                created_at = t.get("created_at") or _now_iso()
                user_cursor = conn.execute(
                    "insert into chat_logs (role, content, created_at) "
                    "values ('user', ?, ?)",
                    (t["user_message"], created_at),
                )
                user_id = int(user_cursor.lastrowid)
                assistant_cursor = conn.execute(
                    "insert into chat_logs (role, content, created_at) "
                    "values ('assistant', ?, ?)",
                    (t["assistant_message"], created_at),
                )
                assistant_id = int(assistant_cursor.lastrowid)
                replay_payload = _import_replay_payload(t, user_id, assistant_id)
                conn.execute(
                    "insert into turn_requests "
                    "(request_id, payload_hash_sha256, status, "
                    "expected_revision, committed_revision, "
                    "user_message_chat_log_id, assistant_message_chat_log_id, "
                    "replay_payload, created_at, updated_at, completed_at) "
                    "values (?, ?, 'completed', 0, 0, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        # Structural hash of the imported payload: the
                        # imported turn is terminal history, never
                        # recomputed, so the hash only needs to be
                        # stable and content-derived.
                        _legacy_hash(t),
                        user_id,
                        assistant_id,
                        json.dumps(replay_payload, ensure_ascii=False),
                        created_at,
                        created_at,
                        created_at,
                    ),
                )
                imported += 1
            total = int(
                conn.execute("select count(*) from turn_requests").fetchone()[0]
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise PersistenceError("database_error", "persistence error") from None
        except ValidationError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    return ImportReport(
        imported_turns=imported,
        skipped_duplicates=skipped,
        total_turns_after=total,
    )


def _import_replay_payload(t: Mapping[str, Any], user_id: int, assistant_id: int) -> dict:
    """Best-effort public replay payload for an imported turn."""
    replay = t.get("replay_payload")
    if isinstance(replay, Mapping):
        payload = dict(replay)
    else:
        payload = {}
    payload.setdefault("response", t["assistant_message"])
    payload.setdefault("message_id", assistant_id)
    return payload


def _legacy_hash(t: Mapping[str, Any]) -> str:
    import hashlib

    canonical = json.dumps(
        {
            "request_id": t["request_id"],
            "user_message": t["user_message"],
            "assistant_message": t["assistant_message"],
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
