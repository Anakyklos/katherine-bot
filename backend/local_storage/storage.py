"""The ``LocalStorage`` facade: atomic turn commit and local persistence.

See the package docstring for the design invariants. This module is the
implementation core: connection lifecycle, migration runner, the atomic
turn commit (CAS on ``profiles.revision``), replay, privacy operations,
retention trims, backup and metrics.

Concurrency policy (single process, no daemon):

- The store owns one ``threading.RLock``. **Every write path** takes the
  lock and opens its transaction with ``BEGIN IMMEDIATE`` so the write
  lock is acquired before the first statement (a deferred transaction
  that upgrades mid-way can fail with SQLITE_BUSY and leave a half-open
  turn).
- Each thread gets its own connection (``threading.local``); SQLite
  connections are not safe to share across threads. With WAL, reader
  threads always see the last **committed** snapshot.
- Writes from other *processes* are not part of this contract: the
  database file belongs to the desktop application process. WAL's
  busy-timeout still avoids transient reader/writer contention inside
  the process.

Recovery policy: a ``pending`` turn with no live in-process writer can
only exist after a crash. On open, every ``pending`` row is moved to
``failed`` with ``error_code='interrupted'`` inside one transaction:
replays of an interrupted request return ``request_replay_unavailable``
(paridade com o contrato web: nunca reexecuta automaticamente).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from . import migrations as migrations_module
from .errors import (
    ConflictError,
    PersistenceError,
    StorageCorruptError,
    ValidationError,
)

#: Application directory name under XDG data home.
APP_DIR_NAME = "katherine"

#: Database file name.
DATABASE_FILE_NAME = "katherine.db"

#: Message length bound, mirroring the web contract (memory.MAX_MESSAGE_LENGTH).
MAX_MESSAGE_LENGTH = 10_000

#: Request id bound (defensive; mirrors the web bounded identifier rule).
MAX_REQUEST_ID_LENGTH = 128

#: Busy timeout for transient reader/writer contention (milliseconds).
_BUSY_TIMEOUT_MS = 5_000

_REQUEST_ID_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
)


# ---------------------------------------------------------------------------
# Errors live in errors.py; re-export for the package contract.
# ---------------------------------------------------------------------------


def default_database_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve the XDG-compliant default database path.

    ``env`` is an injectable environment mapping (defaults to
    ``os.environ``) so tests never touch the real environment. The
    parent directories are created with mode 0o700 (private to the
    user) before returning; failures surface as ``PersistenceError``.
    """
    environment = os.environ if env is None else dict(env)
    data_home = environment.get("XDG_DATA_HOME", "").strip()
    if data_home:
        base = Path(data_home)
    else:
        home = environment.get("HOME", "").strip()
        if not home:
            raise PersistenceError("invalid_environment", "HOME is not set")
        base = Path(home) / ".local" / "share"
    path = base / APP_DIR_NAME / DATABASE_FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise PersistenceError("storage_unavailable", "persistence error") from None
    return path


@dataclass(frozen=True)
class LoadedUserState:
    """Snapshot of the single local profile plus its revision token."""

    revision: int
    persona_config: Optional[str]
    user_profile: Mapping[str, Any]
    emotional_state: Mapping[str, Any]
    relationship_state: Mapping[str, Any]


@dataclass(frozen=True)
class CommittedTurn:
    """Public result of one successful atomic turn commit."""

    request_id: str
    revision: int
    user_message_id: int
    assistant_message_id: int
    response: str
    emotion_state: Mapping[str, Any]


@dataclass(frozen=True)
class ReplayOutcome:
    """Structured replay lookup result (web contract parity)."""

    status: str
    committed: Optional[CommittedTurn] = None


def _validate_request_id(request_id: Any) -> str:
    if not isinstance(request_id, str) or not request_id:
        raise ValidationError("invalid_request_id", "request id must be a non-empty string")
    if len(request_id) > MAX_REQUEST_ID_LENGTH:
        raise ValidationError("invalid_request_id", "request id too long")
    if not all(ch in _REQUEST_ID_ALLOWED for ch in request_id):
        raise ValidationError("invalid_request_id", "request id has invalid characters")
    return request_id


def _validate_message(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("empty_message", f"{field} must be a non-empty string")
    if len(value) > MAX_MESSAGE_LENGTH:
        raise ValidationError("message_too_long", "message exceeds the maximum length")
    return value


def _canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _time_now_unix() -> float:
    import time as _time

    return _time.time()


def _split_sql_statements(sql: str) -> list[str]:
    """Split a migration into individual statements.

    Handles ``BEGIN ... END`` blocks (trigger bodies) by tracking block
    depth, plus single/double-quoted strings. Statements are stripped;
    empty fragments are dropped. A future migration may therefore
    include triggers with embedded semicolons safely.
    """
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    depth = 0
    word: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if not in_single and not in_double:
            # track BEGIN/END keywords for trigger bodies
            if ch.isalpha():
                word.append(ch)
            else:
                joined = "".join(word).upper()
                if joined == "BEGIN":
                    depth += 1
                elif joined == "END" and depth > 0:
                    depth -= 1
                word = []
            if ch == ";" and depth == 0:
                fragment = "".join(current).strip()
                if fragment:
                    statements.append(fragment)
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


class LocalStorage:
    """Single-process SQLite store for the Katherine desktop app."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._closed = False
        self._open_and_migrate()

    # ── Connection management ────────────────────────────────────────────

    def _new_connection(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self._path),
                timeout=_BUSY_TIMEOUT_MS / 1000.0,
                isolation_level=None,  # explicit transaction control
            )
        except sqlite3.Error:
            raise PersistenceError("storage_unavailable", "persistence error") from None
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return conn

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _connection_for_tests_only(self) -> sqlite3.Connection:
        """Direct connection accessor for tests (never used by the app)."""
        return self._connection()

    # ── Open / migrate / recovery ────────────────────────────────────────

    def _open_and_migrate(self) -> None:
        try:
            self._ensure_parent_dir()
            conn = self._connection()
            self._run_migrations(conn)
            self._recover_interrupted_turns(conn)
        except sqlite3.DatabaseError as exc:
            raise StorageCorruptError("corrupt_database", "database error") from None
        except PersistenceError:
            raise
        except sqlite3.Error:
            raise PersistenceError("storage_unavailable", "persistence error") from None

    def _ensure_parent_dir(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise PersistenceError("storage_unavailable", "persistence error") from None

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply pending migrations atomically, version row included.

        ``executescript`` is deliberately avoided: it issues an implicit
        COMMIT before running, which would let a migration's DDL land
        without its version row (the failure mode of issue #335 test 8).
        Instead each migration is split into statements executed one by
        one inside the same transaction that inserts its version row:
        either the whole migration + its record commit, or nothing does.
        """
        with self._lock:
            conn.executescript(migrations_module.BOOTSTRAP_SQL)
            applied_rows = conn.execute(
                "select version from schema_migrations"
            ).fetchall()
            applied = {row[0] for row in applied_rows}
            for version in migrations_module.pending_versions(applied):
                sql = migrations_module.migration_sql(version)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql_statements(sql):
                        conn.execute(statement)
                    conn.execute(
                        "insert into schema_migrations (version) values (?)",
                        (version,),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise

    def _recover_interrupted_turns(self, conn: sqlite3.Connection) -> None:
        """Fail-close pending rows left by a crash: never auto-recompute."""
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "update turn_requests set status = 'failed', "
                    "error_code = 'interrupted', updated_at = ? "
                    "where status = 'pending'",
                    (_now_iso(),),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def schema_version(self) -> int:
        row = self._connection().execute(
            "select coalesce(max(version), 0) from schema_migrations"
        ).fetchone()
        return int(row[0])

    # ── State load ──────────────────────────────────────────────────────

    def load_user_state(self) -> LoadedUserState:
        try:
            row = self._connection().execute(
                "select revision, persona_config, user_profile, "
                "emotional_state, relationship_state from profiles where id = 1"
            ).fetchone()
        except sqlite3.Error:
            raise PersistenceError("database_error", "persistence error") from None
        if row is None:
            # Missing profile: default in-memory neutral v1 state, revision 0.
            # Contract parity with the web ``UserStateRepository.load`` — the
            # caller gets valid v1 snapshots, never empty dicts.
            from backend.emotional_domain import EmotionalStateV1
            from backend.relationship import RelationshipStateV1

            timestamp = _time_now_unix()
            return LoadedUserState(
                revision=0,
                persona_config=None,
                user_profile={},
                emotional_state=EmotionalStateV1.neutral(timestamp=timestamp).to_dict(),
                relationship_state=RelationshipStateV1.neutral(
                    timestamp=timestamp
                ).to_dict(),
            )
        revision, persona, profile, emotional, relationship = row
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise PersistenceError("invalid_state", "persistence error")
        try:
            profile_doc = json.loads(profile) if profile else {}
            emotional_doc = json.loads(emotional) if emotional else {}
            relationship_doc = json.loads(relationship) if relationship else {}
        except (json.JSONDecodeError, TypeError):
            raise PersistenceError("invalid_state", "persistence error") from None
        if not isinstance(profile_doc, dict):
            profile_doc = {}
        if not isinstance(emotional_doc, dict):
            emotional_doc = {}
        if not isinstance(relationship_doc, dict):
            relationship_doc = {}
        return LoadedUserState(
            revision=revision,
            persona_config=persona if isinstance(persona, str) else None,
            user_profile=profile_doc,
            emotional_state=emotional_doc,
            relationship_state=relationship_doc,
        )

    def load_recent_history(self, limit: int = 10) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValidationError("invalid_limit", "limit must be an int in [1, 500]")
        try:
            rows = self._connection().execute(
                "select id, role, content, created_at from chat_logs "
                "order by created_at desc, id desc limit ?",
                (limit,),
            ).fetchall()
        except sqlite3.Error:
            raise PersistenceError("database_error", "persistence error") from None
        history: list[dict[str, Any]] = []
        for msg_id, role, content, created_at in rows:
            if role not in ("user", "assistant"):
                raise PersistenceError("invalid_state", "persistence error")
            if not isinstance(content, str) or len(content) > MAX_MESSAGE_LENGTH:
                raise PersistenceError("invalid_state", "persistence error")
            history.append(
                {
                    "id": msg_id,
                    "role": role,
                    "content": content,
                    "created_at": created_at,
                }
            )
        history.reverse()
        return history

    # ── Atomic turn commit ───────────────────────────────────────────────

    def commit_turn(
        self,
        *,
        request_id: str,
        user_message: str,
        assistant_message: str,
        emotional_state: Mapping[str, Any],
        relationship_state: Mapping[str, Any],
        public_response: str,
        replay_payload: Mapping[str, Any],
        outbox_events: list[tuple[str, Mapping[str, Any], str]] | None = None,
        expected_revision: int | None = None,
    ) -> CommittedTurn:
        """Commit one conversation turn as a single atomic unit.

        Steps (all inside ``BEGIN IMMEDIATE`` … ``COMMIT``):

        1. idempotency: an existing **completed** request replays
           without writing anything;
        2. insert both chat messages;
        3. upsert the single profile row with a revision CAS
           (``WHERE revision = expected``), bumping the revision;
        4. insert the turn ledger row with the replay payload;
        5. insert outbox events (unique idempotency keys).

        Any failure between steps rolls the entire turn back: messages
        and snapshots can never diverge (issue #335, tests 3 and 4).
        """
        request_id = _validate_request_id(request_id)
        user_message = _validate_message(user_message, "user_message")
        assistant_message = _validate_message(assistant_message, "assistant_message")
        if not isinstance(replay_payload, Mapping):
            raise ValidationError("invalid_replay_payload", "replay payload must be a mapping")
        payload_hash = _canonical_payload_hash(replay_payload)
        now = _now_iso()

        with self._lock:
            conn = self._connection()
            # Idempotency first, without opening a write transaction.
            existing = conn.execute(
                "select status, committed_revision, user_message_chat_log_id, "
                "assistant_message_chat_log_id, replay_payload "
                "from turn_requests where request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None and existing[0] == "completed":
                return self._committed_from_row(
                    request_id, existing[1], existing[2], existing[3], existing[4]
                )

            try:
                conn.execute("BEGIN IMMEDIATE")
                profile_row = conn.execute(
                    "select revision from profiles where id = 1"
                ).fetchone()
                current_revision = profile_row[0] if profile_row else 0
                if expected_revision is None:
                    expected = current_revision
                else:
                    if isinstance(expected_revision, bool) or not isinstance(
                        expected_revision, int
                    ) or expected_revision < 0:
                        raise ValidationError(
                            "invalid_expected_revision",
                            "expected_revision must be a non-negative integer",
                        )
                    expected = expected_revision
                if expected != current_revision:
                    raise ConflictError(
                        "revision_mismatch",
                        "Profile revision changed concurrently.",
                        expected_revision=expected,
                        actual_revision=current_revision,
                        request_id=request_id,
                    )

                user_cursor = conn.execute(
                    "insert into chat_logs (role, content, created_at) "
                    "values ('user', ?, ?)",
                    (user_message, now),
                )
                user_message_id = int(user_cursor.lastrowid)
                assistant_cursor = conn.execute(
                    "insert into chat_logs (role, content, created_at) "
                    "values ('assistant', ?, ?)",
                    (assistant_message, now),
                )
                assistant_message_id = int(assistant_cursor.lastrowid)

                new_revision = current_revision + 1
                conn.execute(
                    "insert into profiles (id, emotional_state, relationship_state, "
                    "revision, updated_at) values (1, ?, ?, ?, ?) "
                    "on conflict (id) do update set "
                    "emotional_state = excluded.emotional_state, "
                    "relationship_state = excluded.relationship_state, "
                    "revision = excluded.revision, "
                    "updated_at = excluded.updated_at",
                    (
                        _json_dumps(emotional_state),
                        _json_dumps(relationship_state),
                        new_revision,
                        now,
                    ),
                )

                conn.execute(
                    "insert into turn_requests (request_id, payload_hash_sha256, "
                    "status, expected_revision, committed_revision, "
                    "user_message_chat_log_id, assistant_message_chat_log_id, "
                    "replay_payload, completed_at, updated_at) "
                    "values (?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        payload_hash,
                        expected,
                        new_revision,
                        user_message_id,
                        assistant_message_id,
                        _json_dumps(replay_payload),
                        now,
                        now,
                    ),
                )

                for event_type, payload, idempotency_key in outbox_events or []:
                    conn.execute(
                        "insert into outbox_events (id, event_type, "
                        "idempotency_key, status, turn_request_id, payload, "
                        "created_at) values (?, ?, ?, 'pending', ?, ?, ?) "
                        "on conflict (idempotency_key) do nothing",
                        (
                            f"{idempotency_key}:{event_type}",
                            event_type,
                            idempotency_key,
                            request_id,
                            _json_dumps(payload),
                            now,
                        ),
                    )

                conn.execute("COMMIT")
            except (ConflictError, ValidationError):
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise PersistenceError("database_error", "persistence error") from None
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

            return CommittedTurn(
                request_id=request_id,
                revision=new_revision,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                response=replay_payload.get("response", ""),
                emotion_state=replay_payload.get("emotion_state", {}),
            )

    def _committed_from_row(
        self,
        request_id: str,
        revision: Optional[int],
        user_message_id: Optional[int],
        assistant_message_id: Optional[int],
        replay_payload_text: Optional[str],
    ) -> CommittedTurn:
        try:
            payload = json.loads(replay_payload_text) if replay_payload_text else {}
        except (json.JSONDecodeError, TypeError):
            raise PersistenceError("invalid_state", "persistence error") from None
        if not isinstance(payload, dict):
            raise PersistenceError("invalid_state", "persistence error")
        return CommittedTurn(
            request_id=request_id,
            revision=int(revision or 0),
            user_message_id=int(user_message_id or 0),
            assistant_message_id=int(assistant_message_id or 0),
            response=payload.get("response", ""),
            emotion_state=payload.get("emotion_state", {}),
        )

    # ── Replay ───────────────────────────────────────────────────────────

    def replay(self, request_id: str) -> ReplayOutcome:
        request_id = _validate_request_id(request_id)
        try:
            row = self._connection().execute(
                "select status, committed_revision, user_message_chat_log_id, "
                "assistant_message_chat_log_id, replay_payload "
                "from turn_requests where request_id = ?",
                (request_id,),
            ).fetchone()
        except sqlite3.Error:
            raise PersistenceError("database_error", "persistence error") from None
        if row is None:
            return ReplayOutcome(status="request_replay_unavailable", committed=None)
        status, revision, user_msg_id, assistant_msg_id, payload_text = row
        if status == "completed":
            return ReplayOutcome(
                status="completed",
                committed=self._committed_from_row(
                    request_id, revision, user_msg_id, assistant_msg_id, payload_text
                ),
            )
        if status == "pending":
            return ReplayOutcome(status="request_in_progress", committed=None)
        return ReplayOutcome(status="request_replay_unavailable", committed=None)

    # ── Memory ──────────────────────────────────────────────────────────

    def store_memory(self, content: str, metadata: Mapping[str, Any]) -> int:
        content = _validate_message(content, "memory content")
        if not isinstance(metadata, Mapping):
            raise ValidationError("invalid_metadata", "metadata must be a mapping")
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "insert into memories (content, metadata, created_at) "
                    "values (?, ?, ?)",
                    (content, _json_dumps(metadata), _now_iso()),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise PersistenceError("database_error", "persistence error") from None
            return int(cursor.lastrowid)

    # ── Privacy operations ──────────────────────────────────────────────

    def delete_history(self) -> dict[str, Any]:
        return self._privacy_delete(
            "delete_history",
            "delete from chat_logs",
            "deleted_messages",
            "select count(*) from chat_logs",
        )

    def delete_memories(self) -> dict[str, Any]:
        return self._privacy_delete(
            "delete_memories",
            "delete from memories",
            "deleted_memories",
            "select count(*) from memories",
        )

    def _privacy_delete(
        self, operation: str, delete_sql: str, result_key: str, count_sql: str
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute(count_sql).fetchone()[0]
                conn.execute(delete_sql)
                result = {"status": "applied", result_key: int(count)}
                conn.execute(
                    "insert into privacy_operations (operation, status, result) "
                    "values (?, 'applied', ?)",
                    (operation, _json_dumps(result)),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise PersistenceError("database_error", "persistence error") from None
        return result

    def reset_emotional_state(self, neutral: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self._reset_state_field(
            "reset_emotional_state", "emotional_state", neutral or {}
        )

    def reset_relationship_state(self, neutral: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        return self._reset_state_field(
            "reset_relationship_state", "relationship_state", neutral or {}
        )

    def _reset_state_field(
        self, operation: str, field: str, neutral: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    f"update profiles set {field} = ?, updated_at = ? where id = 1",
                    (_json_dumps(neutral), _now_iso()),
                )
                result = {"status": "applied"}
                conn.execute(
                    "insert into privacy_operations (operation, status, result) "
                    "values (?, 'applied', ?)",
                    (operation, _json_dumps(result)),
                )
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise PersistenceError("database_error", "persistence error") from None
        return result

    # ── Retention ────────────────────────────────────────────────────────

    def trim_history(self, keep_last: int) -> dict[str, Any]:
        if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 0:
            raise ValidationError("invalid_keep_last", "keep_last must be a non-negative int")
        with self._lock:
            conn = self._connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute("select count(*) from chat_logs").fetchone()[0]
                conn.execute(
                    "delete from chat_logs where id not in "
                    "(select id from chat_logs order by id desc limit ?)",
                    (keep_last,),
                )
                result = {"status": "applied", "remaining": int(min(count, keep_last))}
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise PersistenceError("database_error", "persistence error") from None
        return result

    # ── Backup and metrics ──────────────────────────────────────────────

    def backup_to(self, target: Path | str) -> None:
        """Consistent online backup (never captures half-written state)."""
        target_path = Path(target)
        if target_path.exists():
            raise ValidationError("backup_target_exists", "backup target already exists")
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = sqlite3.connect(str(target_path))
            try:
                with self._lock:
                    self._connection().backup(dest)
            finally:
                dest.close()
        except sqlite3.Error:
            raise PersistenceError("backup_failed", "persistence error") from None
        except OSError:
            raise PersistenceError("backup_failed", "persistence error") from None

    def storage_metrics(self) -> dict[str, Any]:
        conn = self._connection()
        try:
            chat_rows = conn.execute("select count(*) from chat_logs").fetchone()[0]
            memory_rows = conn.execute("select count(*) from memories").fetchone()[0]
            turn_rows = conn.execute("select count(*) from turn_requests").fetchone()[0]
            page_count = conn.execute("PRAGMA page_count").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        except sqlite3.Error:
            raise PersistenceError("database_error", "persistence error") from None
        return {
            "chat_log_rows": int(chat_rows),
            "memory_rows": int(memory_rows),
            "turn_request_rows": int(turn_rows),
            "page_count": int(page_count),
            "page_size": int(page_size),
            "journal_mode": str(journal_mode),
        }

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                self._local.conn = None


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise ValidationError("invalid_payload", "payload is not JSON serializable") from None


def open_local_storage(path: Path | str) -> LocalStorage:
    """Open (creating if needed) and migrate a local database.

    Corruption is an explicit ``StorageCorruptError`` — the store is
    never silently reset. Permission/I-O problems surface as
    ``PersistenceError`` with a constant sanitized message.
    """
    try:
        return LocalStorage(path)
    except StorageCorruptError:
        raise
    except PersistenceError:
        raise
    except Exception:
        raise PersistenceError("storage_unavailable", "persistence error") from None
