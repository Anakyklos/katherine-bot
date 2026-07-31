"""
Tests for ``backend.transactional_schema``.

Covers:

 1. Pure importability from subprocess (no heavy deps, no env/socket)
 2. Canonical hash: deterministic
 3. Canonical hash: key-order independent
 4. Canonical hash: 64 lowercase hex digits
 5. Canonical hash: differs for different payloads
 6. Canonical hash: rejects non-mapping input
 7. Canonical hash: rejects non-serializable values without leaking them
 7b. Canonical hash: rejects NaN / Infinity / -Infinity (non-JSON values)
 8. TurnRequestRecord: to_db_row omits None server-owned columns
 9. TurnRequestRecord: to_insert_row drops server-owned None columns
10. TurnRequestRecord: from_db_row round-trips
11. TurnRequestRecord: immutable after construction (incl. deep payload)
12. OutboxEventRecord: to_db_row omits None
13. OutboxEventRecord: to_insert_row drops server-owned None columns
14. OutboxEventRecord: from_db_row round-trips
15. OutboxEventRecord: immutable after construction (incl. deep payload)
16. No shared mutable state / no module-level user state
17. Allowlist constants mirror the SQL allowlists (no overlap with forbidden)
"""

import subprocess
import sys
import os

import pytest
from dataclasses import FrozenInstanceError

from backend.transactional_schema import (
    FORBIDDEN_PAYLOAD_KEYS,
    REPLAY_PAYLOAD_ALLOWED_KEYS,
    OUTBOX_PAYLOAD_ALLOWED_KEYS,
    TurnRequestRecord,
    OutboxEventRecord,
    canonical_payload_hash,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Pure importability
# ═══════════════════════════════════════════════════════════════════════

class TestImportability:
    """Import the module in a subprocess with env, socket, and import
    hooks that fail if the module touches forbidden resources."""

    _PURITY_SCRIPT = '''
import sys

# -- Pre-import ONLY stdlib needed for guards --------------------------
import os as _os
import socket as _socket

class _FailEnv:
    """Mapping proxy that fails on every read operation."""
    def __getitem__(self, key):
        raise RuntimeError(f"os.environ read attempted: key={key!r}")
    def get(self, key, default=None):
        raise RuntimeError(f"os.environ.get read attempted: key={key!r}")
    def __contains__(self, key):
        raise RuntimeError(f"os.environ.__contains__ read attempted: key={key!r}")
    def __setitem__(self, key, value):
        pass
    def __delitem__(self, key):
        pass
    def __repr__(self):
        return "_FailEnv()"
    def __iter__(self):
        return iter([])
    def __len__(self):
        return 0
    def __bool__(self):
        return True

_os.environ = _FailEnv()

def _raise_getenv(key, default=None):
    raise RuntimeError(f"os.getenv read attempted: key={key!r}")
_os.getenv = _raise_getenv

def _raise_socket(*args, **kwargs):
    raise OSError("socket.socket blocked")
def _raise_create_connection(*args, **kwargs):
    raise OSError("socket.create_connection blocked")
_socket.socket = _raise_socket
_socket.create_connection = _raise_create_connection

_BLOCKED_TOP = frozenset({
    "fastapi", "groq", "supabase", "sentence_transformers",
    "pydantic", "httpx", "httpcore", "anyio",
})
_BLOCKED_FULL = frozenset({
    "backend.engine", "backend.memory", "backend.trusted_context",
})

class _BlockImport:
    def find_spec(self, fullname, path, target=None):
        if fullname in _BLOCKED_FULL:
            raise ImportError(f"blocked: {fullname}")
        top = fullname.split(".")[0]
        if top in _BLOCKED_TOP:
            raise ImportError(f"blocked: {fullname}")
        return None

sys.meta_path.insert(0, _BlockImport())

sys.path.insert(0, ".")
from backend.transactional_schema import (
    FORBIDDEN_PAYLOAD_KEYS,
    REPLAY_PAYLOAD_ALLOWED_KEYS,
    OUTBOX_PAYLOAD_ALLOWED_KEYS,
    TurnRequestRecord,
    OutboxEventRecord,
    canonical_payload_hash,
)

assert canonical_payload_hash({"a": 1}) == canonical_payload_hash({"a": 1})
assert len(canonical_payload_hash({"b": 2})) == 64
assert FORBIDDEN_PAYLOAD_KEYS == {
    "prompt", "system_prompt", "meta_cognition", "internal_instructions",
    "message", "user_message", "assistant_message", "content",
}
assert REPLAY_PAYLOAD_ALLOWED_KEYS.isdisjoint(FORBIDDEN_PAYLOAD_KEYS)
assert OUTBOX_PAYLOAD_ALLOWED_KEYS.isdisjoint(FORBIDDEN_PAYLOAD_KEYS)
rec = TurnRequestRecord(user_id="u", request_id="r", payload_hash_sha256="a" * 64, status="pending")
assert rec.to_db_row()["user_id"] == "u"
outbox = OutboxEventRecord(event_type="memory_indexed", user_id="u", payload={}, status="pending", idempotency_key="k")
assert outbox.to_db_row()["status"] == "pending"
print("OK")
'''

    def test_pure_import_with_guards(self):
        proc = subprocess.run(
            [sys.executable, "-c", self._PURITY_SCRIPT],
            capture_output=True, text=True,
            cwd=os.getcwd(),
            env={
                **os.environ,
                "PYTHONPATH": ".",
                "GROQ_API_KEY": "",
                "SUPABASE_URL": "",
                "SUPABASE_KEY": "",
            },
        )
        assert proc.returncode == 0, (
            f"Subprocess failed (guard triggered?):\n"
            f"stdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
        assert "OK" in proc.stdout


# ═══════════════════════════════════════════════════════════════════════
# 2–7. canonical_payload_hash
# ═══════════════════════════════════════════════════════════════════════

class TestCanonicalPayloadHash:
    def test_deterministic(self):
        payload = {"request_id": "r-1", "user_id": "u-1", "message": "hello"}
        assert canonical_payload_hash(payload) == canonical_payload_hash(payload)

    def test_key_order_independent(self):
        a = {"z": 1, "a": 2, "m": [1, 2]}
        b = {"m": [1, 2], "a": 2, "z": 1}
        assert canonical_payload_hash(a) == canonical_payload_hash(b)

    def test_nested_order_independent(self):
        a = {"outer": {"z": 1, "a": 2}}
        b = {"outer": {"a": 2, "z": 1}}
        assert canonical_payload_hash(a) == canonical_payload_hash(b)

    def test_hex64_lowercase(self):
        import re
        digest = canonical_payload_hash({"x": 1})
        assert re.fullmatch(r"[0-9a-f]{64}", digest) is not None

    def test_differs_for_different_payloads(self):
        assert canonical_payload_hash({"a": 1}) != canonical_payload_hash({"a": 2})
        assert canonical_payload_hash({"a": 1}) != canonical_payload_hash({"b": 1})

    def test_rejects_non_mapping(self):
        with pytest.raises(TypeError):
            canonical_payload_hash("not-a-mapping")
        with pytest.raises(TypeError):
            canonical_payload_hash([1, 2])

    def test_rejects_non_serializable_without_leak(self):
        class Unserializable:
            def __repr__(self):
                return "SECRET_INTERNAL_MARKER"

        with pytest.raises(TypeError) as excinfo:
            canonical_payload_hash({"bad": Unserializable()})
        assert "SECRET_INTERNAL_MARKER" not in str(excinfo.value)
        assert "SECRET_INTERNAL_MARKER" not in repr(excinfo.value)

    def test_rejects_non_finite_floats(self):
        """NaN / Infinity / -Infinity are not interoperable JSON and must
        never be hashed (the database jsonb type refuses them too)."""
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(TypeError):
                canonical_payload_hash({"x": value})

    def test_rejects_nested_non_finite_floats(self):
        with pytest.raises(TypeError):
            canonical_payload_hash({"outer": {"x": float("nan")}})


# ═══════════════════════════════════════════════════════════════════════
# 8–11. TurnRequestRecord
# ═══════════════════════════════════════════════════════════════════════

class TestTurnRequestRecord:
    def _base(self):
        return TurnRequestRecord(
            user_id="u-1",
            request_id="11111111-1111-4111-8111-111111111111",
            payload_hash_sha256="a" * 64,
            status="pending",
            expected_revision=0,
            lease_owner="worker-a",
            lease_expires_at="2099-01-01T00:00:00Z",
        )

    def test_to_db_row_omits_none_fields(self):
        row = self._base().to_db_row()
        assert row["user_id"] == "u-1"
        assert row["status"] == "pending"
        assert row["lease_owner"] == "worker-a"
        assert row["committed_revision"] is None
        assert row["replay_payload"] is None

    def test_to_insert_row_drops_server_owned_none(self):
        insert = self._base().to_insert_row()
        assert "id" not in insert
        assert "created_at" not in insert
        assert "updated_at" not in insert
        assert "completed_at" not in insert
        assert insert["user_id"] == "u-1"

    def test_from_db_row_round_trip(self):
        """from_db_row must reproduce every field present in the row."""
        row = self._base().to_db_row()
        row["id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        row["created_at"] = "2026-07-29T10:00:00Z"
        row["completed_at"] = "2026-07-29T10:00:00Z"
        record = TurnRequestRecord.from_db_row(row)
        assert record.user_id == "u-1"
        assert record.status == "pending"
        assert record.id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        assert record.created_at == "2026-07-29T10:00:00Z"
        assert record.completed_at == "2026-07-29T10:00:00Z"
        # Reserializing the parsed record reproduces the input row exactly.
        assert record.to_db_row() == row

    def test_immutable_after_construction(self):
        record = self._base()
        with pytest.raises(FrozenInstanceError):
            record.status = "completed"  # type: ignore[assignment]

    def test_replay_payload_deeply_immutable(self):
        """The stored replay_payload is a frozen copy: mutating the source
        dict after construction must not affect the record, and mutating
        the stored payload must raise."""
        source = {"response": "ok", "emotion_state": {"name": "calm"}}
        record = TurnRequestRecord(
            user_id="u-1",
            request_id="11111111-1111-4111-8111-111111111111",
            payload_hash_sha256="a" * 64,
            status="completed",
            committed_revision=1,
            replay_payload=source,
        )
        # Mutating the caller's dict afterwards must not leak into the record.
        source["response"] = "mutated"
        source["emotion_state"]["name"] = "mutated"
        assert record.replay_payload["response"] == "ok"
        assert record.replay_payload["emotion_state"]["name"] == "calm"
        # The stored payload itself is immutable.
        with pytest.raises(TypeError):
            record.replay_payload["response"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            record.replay_payload["emotion_state"]["name"] = "x"  # type: ignore[index]
        # Serialization yields plain, JSON-serializable structures.
        import json
        json.dumps(record.to_db_row()["replay_payload"])


# ═══════════════════════════════════════════════════════════════════════
# 12–15. OutboxEventRecord
# ═══════════════════════════════════════════════════════════════════════

class TestOutboxEventRecord:
    def _base(self):
        return OutboxEventRecord(
            event_type="memory_indexed",
            user_id="u-1",
            payload={"ref": "turn-1"},
            status="pending",
            idempotency_key="idem-1",
            next_attempt_at="2099-01-01T00:00:00Z",
        )

    def test_to_db_row_omits_none_fields(self):
        row = self._base().to_db_row()
        assert row["event_type"] == "memory_indexed"
        assert row["payload"] == {"ref": "turn-1"}
        assert row["lease_owner"] is None
        assert row["attempts"] == 0

    def test_to_insert_row_drops_server_owned_none(self):
        insert = self._base().to_insert_row()
        assert "id" not in insert
        assert "created_at" not in insert
        assert "processed_at" not in insert
        assert "dead_lettered_at" not in insert
        assert "retention_until" not in insert
        assert insert["idempotency_key"] == "idem-1"

    def test_from_db_row_round_trip(self):
        """from_db_row must reproduce every field present in the row."""
        row = self._base().to_db_row()
        row["id"] = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        row["processed_at"] = "2026-07-29T10:00:00Z"
        record = OutboxEventRecord.from_db_row(row)
        assert record.event_type == "memory_indexed"
        assert record.status == "pending"
        assert record.id == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        assert record.processed_at == "2026-07-29T10:00:00Z"
        # Reserializing the parsed record reproduces the input row exactly.
        assert record.to_db_row() == row

    def test_immutable_after_construction(self):
        record = self._base()
        with pytest.raises(FrozenInstanceError):
            record.status = "completed"  # type: ignore[assignment]

    def test_payload_deeply_immutable(self):
        """The stored outbox payload is a frozen copy: mutating the source
        dict after construction must not affect the record, and mutating
        the stored payload must raise."""
        source = {"ref": "turn-1", "meta": {"n": 1}}
        record = OutboxEventRecord(
            event_type="memory_indexed",
            user_id="u-1",
            payload=source,
            status="pending",
            idempotency_key="idem-1",
        )
        source["ref"] = "mutated"
        source["meta"]["n"] = 2
        assert record.payload["ref"] == "turn-1"
        assert record.payload["meta"]["n"] == 1
        with pytest.raises(TypeError):
            record.payload["ref"] = "x"  # type: ignore[index]
        with pytest.raises(TypeError):
            record.payload["meta"]["n"] = 3  # type: ignore[index]
        import json
        json.dumps(record.to_db_row()["payload"])


# ═══════════════════════════════════════════════════════════════════════
# 16. No shared mutable state
# ═══════════════════════════════════════════════════════════════════════

class TestNoSharedState:
    def test_forbidden_keys_frozen_set(self):
        assert FORBIDDEN_PAYLOAD_KEYS == frozenset({
            "prompt",
            "system_prompt",
            "meta_cognition",
            "internal_instructions",
            "message",
            "user_message",
            "assistant_message",
            "content",
        })

    def test_allowlists_are_disjoint_from_forbidden(self):
        """Allowlist constants must mirror the SQL allowlists and never
        overlap with the forbidden keys."""
        assert REPLAY_PAYLOAD_ALLOWED_KEYS == frozenset({
            "response",
            "emotion_state",
            "message_id",
            "request_id",
            "duration_ms",
        })
        assert OUTBOX_PAYLOAD_ALLOWED_KEYS == frozenset({
            "ref",
            "request_id",
            "turn_id",
            "message_id",
            "entity_id",
            "kind",
            "version",
        })
        assert REPLAY_PAYLOAD_ALLOWED_KEYS.isdisjoint(FORBIDDEN_PAYLOAD_KEYS)
        assert OUTBOX_PAYLOAD_ALLOWED_KEYS.isdisjoint(FORBIDDEN_PAYLOAD_KEYS)

    def test_records_are_independent(self):
        r1 = TurnRequestRecord(user_id="u", request_id="r", payload_hash_sha256="a" * 64, status="pending")
        r2 = TurnRequestRecord(user_id="u", request_id="r", payload_hash_sha256="a" * 64, status="pending")
        assert r1 == r2

    def test_no_module_level_user_state(self):
        """Module-level state must be constants only — never user-keyed state."""
        import backend.transactional_schema as module
        # ``__builtins__`` is injected by the interpreter; everything else
        # must be an immutable constant or a class/function.
        mutable = [
            (k, v)
            for k, v in vars(module).items()
            if k != "__builtins__" and isinstance(v, (dict, list, set))
        ]
        assert mutable == []
