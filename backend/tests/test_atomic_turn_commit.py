"""
Tests for ``backend.atomic_turn_commit`` (#271).

Covers:
 1. Pure importability from subprocess (no heavy deps, no env/socket/clock/
    randomness/filesystem at import)
 2. Input validation: types, ranges, constraints, snapshots, outbox shape
 3. Canonical payload hash consistency
 4. RPC payload building (empty mappings preserved as {})
 5. Result parsing: success and error cases (strict contract validation)
 6. Conflict / validation / persistence error classification
 7. Immutability of result objects
 8. No shared mutable state
 9. Defense-in-depth validation before RPC
10. Async commit_turn entry point (validation, hash, payload, awaited once,
    success/conflict/in-progress/persistence/malformed)
"""

import json
import subprocess
import sys
import os

import pytest
from dataclasses import FrozenInstanceError
from backend.emotional_domain import EmotionalStateV1
from backend.relationship import RelationshipStateV1
from typing import Mapping, Any, Optional

# Import under test
from backend.atomic_turn_commit import (
    CommittedTurn,
    CommittedOutboxRef,
    ConflictError,
    ValidationError,
    PersistenceError,
    validate_atomic_commit_input,
    build_commit_turn_rpc_payload,
    parse_commit_turn_result,
    commit_turn,
    _validate_idempotency_key,
    _validate_lease_owner,
    _validate_error_code,
    _validate_replay_payload,
    _validate_snapshot_payload,
)
from backend.transactional_schema import (
    FORBIDDEN_PAYLOAD_KEYS,
    REPLAY_PAYLOAD_ALLOWED_KEYS,
    canonical_payload_hash,
)

VALID_REQUEST_ID = "12345678-1234-1234-1234-123456789abc"
VALID_MESSAGE_ID = "87654321-4321-4321-4321-cba987654321"


def _valid_replay_payload(response: str = "Hi there!") -> dict:
    return {
        "response": response,
        "message_id": VALID_MESSAGE_ID,
        "duration_ms": 100,
    }


def _valid_emotional_state() -> dict:
    return {
        "schema_version": 1, "pleasure": 0.4, "arousal": 0.3, "dominance": 0.2,
        "libido": 0.2, "aggression": 0.1, "connection": 0.7, "energy": 0.8,
        "tension": 0.1, "coping_mode": "HEALTHY", "timestamp": 1700000000.0,
    }


def _valid_relationship_state() -> dict:
    return {
        "schema_version": 1, "trust": 0.8, "affection": 0.6, "tension": 0.1,
        "triggers": [], "timestamp": 1700000000.0,
    }


# ═══════════════════════════════════════════════════════════════════════
# 1. Pure importability
# ═══════════════════════════════════════════════════════════════════════

_PURITY_SCRIPT = '''
import sys
import os as _os
import socket as _socket
import time as _time
import random as _random

class _FailEnv:
    def __getitem__(self, key):
        raise RuntimeError(f"os.environ read: {key!r}")
    def get(self, key, default=None):
        raise RuntimeError(f"os.environ.get read: {key!r}")
    def __contains__(self, key):
        raise RuntimeError(f"os.environ.__contains__: {key!r}")
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
    raise RuntimeError(f"os.getenv read: {key!r}")
_os.getenv = _raise_getenv

# Block socket (network)
def _raise_socket(*args, **kwargs):
    raise OSError("socket.socket blocked")
def _raise_create_connection(*args, **kwargs):
    raise OSError("socket.create_connection blocked")
_socket.socket = _raise_socket
_socket.create_connection = _raise_create_connection
_os.socket = _raise_socket

# Block clock
def _raise_time(*args, **kwargs):
    raise RuntimeError("clock access blocked")
for _name in ("time", "monotonic", "perf_counter", "process_time"):
    setattr(_time, _name, _raise_time)

# Block randomness
def _raise_random(*args, **kwargs):
    raise RuntimeError("randomness blocked")
for _name in ("random", "randrange", "randint", "uniform", "choices"):
    setattr(_random, _name, _raise_random)

# Block filesystem access
import builtins as _builtins
_original_open = _builtins.open
def _raise_open(*args, **kwargs):
    raise RuntimeError("filesystem access blocked")
_builtins.open = _raise_open
try:
    _os.open = _raise_open
except Exception:
    pass

_BLOCKED_TOP = frozenset({
    "fastapi", "groq", "supabase", "sentence_transformers",
    "pydantic", "httpx", "httpcore", "anyio", "torch", "numpy",
})
_BLOCKED_FULL = frozenset({
    "backend.engine", "backend.memory", "backend.trusted_context",
})

# backend.transactional_schema is explicitly allowed; everything else
# under backend.* that is not the module under test is blocked.
class _BlockImport:
    def find_spec(self, fullname, path, target=None):
        if fullname in _BLOCKED_FULL:
            raise ImportError(f"blocked: {fullname}")
        top = fullname.split(".")[0]
        if top in _BLOCKED_TOP:
            raise ImportError(f"blocked: {fullname}")
        if fullname.startswith("backend.") and fullname not in (
            "backend",
            "backend.atomic_turn_commit",
            "backend.transactional_schema",
        ):
            raise ImportError(f"blocked: {fullname}")
        return None

sys.meta_path.insert(0, _BlockImport())

import backend.atomic_turn_commit
print("IMPORT_OK")
'''


class TestImportability:
    """Import the module in a subprocess with all external resources blocked."""

    def test_import_purity(self):
        """Module imports without touching env, socket, clock, randomness,
        filesystem, or blocked packages."""
        proc = subprocess.run(
            [sys.executable, "-c", _PURITY_SCRIPT],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "IMPORT_OK" in proc.stdout, f"stdout={proc.stdout!r}, stderr={proc.stderr!r}"


# ═══════════════════════════════════════════════════════════════════════
# 2. Validation helpers
# ═══════════════════════════════════════════════════════════════════════


class TestValidateIdempotencyKey:
    def test_valid_key(self):
        _validate_idempotency_key("abc123")
        _validate_idempotency_key("a" * 128)
        _validate_idempotency_key("test-key_with.period:colon")

    def test_too_short(self):
        with pytest.raises(ValidationError) as exc:
            _validate_idempotency_key("")
        assert "invalid_idempotency_key" in str(exc.value)

    def test_too_long(self):
        with pytest.raises(ValidationError) as exc:
            _validate_idempotency_key("a" * 129)
        assert "invalid_idempotency_key" in str(exc.value)

    def test_invalid_characters(self):
        with pytest.raises(ValidationError) as exc:
            _validate_idempotency_key("test space")
        assert "invalid_idempotency_key" in str(exc.value)

    def test_unicode_rejected(self):
        # str.isalnum() would accept these; the explicit ASCII regex must not.
        for key in ("chave_é", "ключ", "キー", "abc\u00e9"):
            with pytest.raises(ValidationError) as exc:
                _validate_idempotency_key(key)
            assert "invalid_idempotency_key" in str(exc.value)

    def test_not_string(self):
        with pytest.raises(ValidationError) as exc:
            _validate_idempotency_key(123)
        assert "invalid_idempotency_key" in str(exc.value)


class TestValidateLeaseOwner:
    def test_valid_owner(self):
        _validate_lease_owner("worker-1")
        _validate_lease_owner("a" * 64)
        _validate_lease_owner(None)

    def test_too_long(self):
        with pytest.raises(ValidationError) as exc:
            _validate_lease_owner("a" * 65)
        assert "invalid_lease_owner" in str(exc.value)

    def test_invalid_characters(self):
        with pytest.raises(ValidationError) as exc:
            _validate_lease_owner("worker space")
        assert "invalid_lease_owner" in str(exc.value)

    def test_unicode_rejected(self):
        for owner in ("worker_ç", "worker\u00e9"):
            with pytest.raises(ValidationError) as exc:
                _validate_lease_owner(owner)
            assert "invalid_lease_owner" in str(exc.value)


class TestValidateErrorCode:
    def test_valid_code(self):
        _validate_error_code("test_error")
        _validate_error_code("a" * 64)
        _validate_error_code(None)

    def test_too_long(self):
        with pytest.raises(ValidationError) as exc:
            _validate_error_code("a" * 65)
        assert "invalid_error_code" in str(exc.value)

    def test_uppercase(self):
        with pytest.raises(ValidationError) as exc:
            _validate_error_code("Test_Error")
        assert "invalid_error_code" in str(exc.value)


class TestValidateReplayPayload:
    def test_valid_payload(self):
        _validate_replay_payload(_valid_replay_payload())

    def test_empty_payload_rejected(self):
        # response is mandatory: {} is not a valid replay payload.
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload({})
        assert "invalid_replay_payload" in str(exc.value)

    def test_missing_message_id(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload({"response": "Hi", "duration_ms": 100})
        assert "invalid_replay_payload" in str(exc.value)

    def test_invalid_message_id(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload(
                {"response": "Hi", "message_id": "not-a-uuid", "duration_ms": 100}
            )
        assert "invalid_replay_payload" in str(exc.value)

    def test_forbidden_key_top_level(self):
        for key in FORBIDDEN_PAYLOAD_KEYS:
            with pytest.raises(ValidationError) as exc:
                _validate_replay_payload({**{"response": "x", "message_id": VALID_MESSAGE_ID}, key: "value"})
            assert "invalid_replay_payload" in str(exc.value)

    def test_forbidden_key_nested(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload(
                {"response": {"message": "nested"}, "message_id": VALID_MESSAGE_ID}
            )
        assert "invalid_replay_payload" in str(exc.value)

    def test_unknown_key(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload(
                {"response": "x", "message_id": VALID_MESSAGE_ID, "unknown_key": "value"}
            )
        assert "invalid_replay_payload" in str(exc.value)

    def test_not_mapping(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload("not a mapping")
        assert "invalid_replay_payload" in str(exc.value)

    def test_response_must_be_string(self):
        with pytest.raises(ValidationError):
            _validate_replay_payload({"response": 123, "message_id": VALID_MESSAGE_ID})

    def test_request_id_must_match_enclosing_request(self):
        with pytest.raises(ValidationError):
            _validate_replay_payload(
                {
                    "response": "Hi",
                    "message_id": VALID_MESSAGE_ID,
                    "request_id": VALID_REQUEST_ID,
                },
                "00000000-0000-0000-0000-000000000000",
            )


class TestValidateSnapshotPayload:
    def test_none_ok(self):
        _validate_snapshot_payload(None, "emotional_state")
        _validate_snapshot_payload(None, "relationship_state")

    def test_valid_emotional(self):
        _validate_snapshot_payload(_valid_emotional_state(), "emotional_state")

    def test_valid_relationship(self):
        _validate_snapshot_payload(_valid_relationship_state(), "relationship_state")

    def test_not_mapping(self):
        with pytest.raises(ValidationError):
            _validate_snapshot_payload("x", "emotional_state")
        with pytest.raises(ValidationError):
            _validate_snapshot_payload([], "relationship_state")

    def test_schema_version_bool_rejected(self):
        for bad in (True, False):
            with pytest.raises(ValidationError) as exc:
                _validate_snapshot_payload(
                    {"schema_version": bad, "pleasure": 0.1, "arousal": 0.2, "dominance": 0.3},
                    "emotional_state",
                )
            assert "schema_version" in str(exc.value)

    def test_schema_version_string_rejected(self):
        with pytest.raises(ValidationError):
            _validate_snapshot_payload(
                {"schema_version": "1", "pleasure": 0.1, "arousal": 0.2, "dominance": 0.3},
                "emotional_state",
            )

    def test_schema_version_float_rejected(self):
        with pytest.raises(ValidationError):
            _validate_snapshot_payload(
                {"schema_version": 1.0, "pleasure": 0.1, "arousal": 0.2, "dominance": 0.3},
                "emotional_state",
            )

    def test_schema_version_not_1(self):
        with pytest.raises(ValidationError):
            _validate_snapshot_payload(
                {"schema_version": 2, "pleasure": 0.1, "arousal": 0.2, "dominance": 0.3},
                "emotional_state",
            )

    def test_user_id_rejected_at_any_depth(self):
        payload = {
            "schema_version": 1,
            "pleasure": 0.1,
            "arousal": 0.2,
            "dominance": {"inner": {"user_id": "attacker"}},
        }
        with pytest.raises(ValidationError):
            _validate_snapshot_payload(payload, "emotional_state")

    def test_bond_label_rejected_at_any_depth(self):
        payload = {
            "schema_version": 1,
            "trust": 0.8,
            "affection": 0.6,
            "tension": [{"bond_label": "x"}],
        }
        with pytest.raises(ValidationError):
            _validate_snapshot_payload(payload, "relationship_state")

    def test_prompt_rejected_at_any_depth(self):
        payload = {
            "schema_version": 1,
            "pleasure": 0.1,
            "arousal": 0.2,
            "dominance": 0.3,
            "nested": {"prompt": "hidden"},
        }
        with pytest.raises(ValidationError):
            _validate_snapshot_payload(payload, "emotional_state")

    def test_missing_fundamental_emotional(self):
        with pytest.raises(ValidationError) as exc:
            _validate_snapshot_payload(
                {"schema_version": 1, "pleasure": 0.1},
                "emotional_state",
            )
        assert "missing fundamental fields" in str(exc.value)

    def test_missing_fundamental_relationship(self):
        with pytest.raises(ValidationError) as exc:
            _validate_snapshot_payload(
                {"schema_version": 1, "trust": 0.8},
                "relationship_state",
            )
        assert "missing fundamental fields" in str(exc.value)

    @pytest.mark.parametrize("coping_mode", ["UNKNOWN", "", None])
    def test_domain_serializer_rejects_invalid_coping_mode(self, coping_mode):
        with pytest.raises(ValueError):
            EmotionalStateV1.create(
                pleasure=0.0, arousal=0.0, dominance=0.0,
                libido=0.0, aggression=0.0, connection=0.5,
                energy=0.8, tension=0.0, coping_mode=coping_mode,
                timestamp=1.0,
            )

    @pytest.mark.parametrize("timestamp", [0, -1, float("nan"), float("inf")])
    def test_domain_serializers_reject_invalid_timestamps(self, timestamp):
        with pytest.raises(ValueError):
            EmotionalStateV1.neutral(timestamp=timestamp)
        with pytest.raises(ValueError):
            RelationshipStateV1.neutral(timestamp=timestamp)

    @pytest.mark.parametrize("triggers", [[""], ["x" * 129], [f"t{i}" for i in range(33)]])
    def test_domain_serializer_rejects_invalid_triggers(self, triggers):
        with pytest.raises(ValueError):
            RelationshipStateV1.create(
                trust=0.5, affection=0.3, tension=0.0,
                triggers=triggers, timestamp=1.0,
            )

    def test_domain_serializer_normalizes_duplicate_triggers(self):
        state = RelationshipStateV1.create(
            trust=0.5, affection=0.3, tension=0.0,
            triggers=["duplicate", "duplicate"], timestamp=1.0,
        )
        assert state.to_dict()["triggers"] == ["duplicate"]


# ═══════════════════════════════════════════════════════════════════════
# 3. Full input validation
# ═══════════════════════════════════════════════════════════════════════


class TestValidateAtomicCommitInput:
    @pytest.fixture
    def valid_inputs(self):
        return {
            "authenticated_user_id": "user_123",
            "request_id": VALID_REQUEST_ID,
            "expected_revision": 0,
            "user_message": "Hello",
            "assistant_message": "Hi there!",
            "emotional_state": _valid_emotional_state(),
            "relationship_state": _valid_relationship_state(),
            "public_response": "Hi there!",
            "outbox_events": [("turn_completed", {"ref": "turn_1"}, "turn_1_priv")],
            "replay_payload": _valid_replay_payload(),
        }

    def test_valid_input(self, valid_inputs):
        validate_atomic_commit_input(**valid_inputs)

    def test_empty_user_id(self, valid_inputs):
        valid_inputs["authenticated_user_id"] = ""
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_user_id" in str(exc.value)

    def test_empty_request_id(self, valid_inputs):
        valid_inputs["request_id"] = ""
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_request_id" in str(exc.value)

    def test_invalid_uuid_request_id(self, valid_inputs):
        valid_inputs["request_id"] = "not-a-valid-uuid"
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_request_id" in str(exc.value)
        assert "UUID" in str(exc.value)

    def test_negative_revision(self, valid_inputs):
        valid_inputs["expected_revision"] = -1
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_expected_revision" in str(exc.value)

    def test_bool_revision_rejected(self, valid_inputs):
        valid_inputs["expected_revision"] = True
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_expected_revision" in str(exc.value)

    def test_non_string_user_message(self, valid_inputs):
        valid_inputs["user_message"] = 123
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_user_message" in str(exc.value)

    def test_non_string_assistant_message(self, valid_inputs):
        valid_inputs["assistant_message"] = None
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_assistant_message" in str(exc.value)

    def test_non_mapping_emotional_state(self, valid_inputs):
        valid_inputs["emotional_state"] = "not a mapping"
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_emotional_state" in str(exc.value)

    def test_non_mapping_relationship_state(self, valid_inputs):
        valid_inputs["relationship_state"] = ["not", "a", "mapping"]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_relationship_state" in str(exc.value)

    def test_non_string_public_response(self, valid_inputs):
        valid_inputs["public_response"] = 123
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_public_response" in str(exc.value)

    def test_public_response_mismatch(self, valid_inputs):
        valid_inputs["public_response"] = "Different response"
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_public_response" in str(exc.value)
        assert "equal replay_payload.response" in str(exc.value)

    def test_non_list_outbox_events(self, valid_inputs):
        valid_inputs["outbox_events"] = "not a list"
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_outbox_events" in str(exc.value)

    def test_outbox_event_wrong_arity_2(self, valid_inputs):
        valid_inputs["outbox_events"] = [("turn_completed", {"ref": "x"})]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_outbox_events" in str(exc.value)
        assert "exactly three elements" in str(exc.value)

    def test_outbox_event_wrong_arity_4(self, valid_inputs):
        valid_inputs["outbox_events"] = [("turn_completed", {"ref": "x"}, "k", "extra")]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_outbox_events" in str(exc.value)

    def test_outbox_event_not_sequence(self, valid_inputs):
        valid_inputs["outbox_events"] = ["not-a-sequence"]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_outbox_events" in str(exc.value)

    def test_empty_outbox_event_type(self, valid_inputs):
        valid_inputs["outbox_events"] = [("", {"ref": "x"}, "key")]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_outbox_events" in str(exc.value)

    def test_non_mapping_outbox_payload(self, valid_inputs):
        valid_inputs["outbox_events"] = [("turn_completed", "not mapping", "key")]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_outbox_events" in str(exc.value)

    def test_outbox_payload_forbidden_key(self, valid_inputs):
        valid_inputs["outbox_events"] = [("turn_completed", {"ref": "x", "prompt": "p"}, "key")]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_outbox_events" in str(exc.value)

    def test_invalid_idempotency_key_in_outbox(self, valid_inputs):
        valid_inputs["outbox_events"] = [
            ("turn_completed", {"ref": "x"}, "invalid key with spaces")
        ]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_idempotency_key" in str(exc.value)

    def test_none_emotional_state(self, valid_inputs):
        valid_inputs["emotional_state"] = None
        validate_atomic_commit_input(**valid_inputs)

    def test_none_relationship_state(self, valid_inputs):
        valid_inputs["relationship_state"] = None
        validate_atomic_commit_input(**valid_inputs)

    def test_emotional_state_missing_schema_version(self, valid_inputs):
        valid_inputs["emotional_state"] = {"pleasure": 0.1, "arousal": 0.2, "dominance": 0.3}
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_emotional_state" in str(exc.value)
        assert "schema_version" in str(exc.value)

    def test_emotional_state_invalid_schema_version(self, valid_inputs):
        valid_inputs["emotional_state"] = {
            "schema_version": 2,
            "pleasure": 0.1,
            "arousal": 0.2,
            "dominance": 0.3,
        }
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert "invalid_emotional_state" in str(exc.value)
        assert "schema_version must be 1" in str(exc.value)

    def test_empty_outbox_events(self, valid_inputs):
        valid_inputs["outbox_events"] = []
        validate_atomic_commit_input(**valid_inputs)

    @pytest.mark.parametrize("event_type", ["a" * 64, "a" * 65, "A", "a-b"])
    def test_event_type_sql_regex_boundaries(self, valid_inputs, event_type):
        valid_inputs["outbox_events"] = [(event_type, {}, "key")]
        if event_type == "a" * 64:
            validate_atomic_commit_input(**valid_inputs)
        else:
            with pytest.raises(ValidationError) as exc:
                validate_atomic_commit_input(**valid_inputs)
            assert exc.value.code == "invalid_outbox_events"

    @pytest.mark.parametrize("key", ["a" * 128, "a" * 129, "a", "a b"])
    def test_outbox_idempotency_key_sql_regex_boundaries(self, valid_inputs, key):
        valid_inputs["outbox_events"] = [("turn_completed", {}, key)]
        if key in {"a" * 128, "a"}:
            validate_atomic_commit_input(**valid_inputs)
        else:
            with pytest.raises(ValidationError) as exc:
                validate_atomic_commit_input(**valid_inputs)
            assert exc.value.code == "invalid_idempotency_key"

    def test_replay_payload_byte_limit(self, valid_inputs):
        valid_inputs["replay_payload"] = _valid_replay_payload("x" * 8200)
        valid_inputs["public_response"] = valid_inputs["replay_payload"]["response"]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert exc.value.code == "invalid_replay_payload"

    def test_outbox_payload_byte_limit(self, valid_inputs):
        valid_inputs["outbox_events"] = [("turn_completed", {"ref": "x" * 8200}, "key")]
        with pytest.raises(ValidationError) as exc:
            validate_atomic_commit_input(**valid_inputs)
        assert exc.value.code == "invalid_outbox_events"

# ═══════════════════════════════════════════════════════════════════════
# 4. RPC payload building
# ═══════════════════════════════════════════════════════════════════════


class TestBuildCommitTurnRpcPayload:
    @pytest.fixture
    def base_params(self):
        return {
            "authenticated_user_id": "user_123",
            "request_id": VALID_REQUEST_ID,
            "expected_revision": 0,
            "user_message": "Hello",
            "assistant_message": "Hi there!",
            "emotional_state": _valid_emotional_state(),
            "relationship_state": _valid_relationship_state(),
            "public_response": "Hi there!",
            "outbox_events": [
                ("turn_completed", {"ref": "turn_1"}, "turn_1_key"),
                ("memory_updated", {"entity_id": "mem_1"}, "mem_1_key"),
            ],
            "replay_payload": _valid_replay_payload(),
            "payload_hash_sha256": "a" * 64,
            "lease_owner": "worker-1",
        }

    def test_builds_complete_payload(self, base_params):
        result = build_commit_turn_rpc_payload(**base_params)

        assert result["p_authenticated_user_id"] == base_params["authenticated_user_id"]
        assert result["p_request_id"] == base_params["request_id"]
        assert result["p_expected_revision"] == base_params["expected_revision"]
        assert result["p_user_message"] == base_params["user_message"]
        assert result["p_assistant_message"] == base_params["assistant_message"]
        assert result["p_emotional_state"] == base_params["emotional_state"]
        assert result["p_relationship_state"] == base_params["relationship_state"]
        assert result["p_public_response"] == base_params["public_response"]
        assert result["p_payload_hash_sha256"] == base_params["payload_hash_sha256"]
        assert result["p_lease_owner"] == base_params["lease_owner"]

    def test_builds_outbox_array(self, base_params):
        result = build_commit_turn_rpc_payload(**base_params)

        outbox = result["p_outbox_events"]
        assert isinstance(outbox, list)
        assert len(outbox) == 2
        assert outbox[0]["event_type"] == "turn_completed"
        assert outbox[0]["payload"] == {"ref": "turn_1"}
        assert outbox[0]["idempotency_key"] == "turn_1_key"

    def test_handles_none_emotional_state(self, base_params):
        base_params["emotional_state"] = None
        result = build_commit_turn_rpc_payload(**base_params)
        assert result["p_emotional_state"] is None

    def test_preserves_empty_mapping_as_empty(self, base_params):
        # Empty mappings must be preserved as {} — never converted to NULL.
        base_params["emotional_state"] = {}
        base_params["relationship_state"] = {}
        base_params["replay_payload"] = {}
        result = build_commit_turn_rpc_payload(**base_params)
        assert result["p_emotional_state"] == {}
        assert result["p_relationship_state"] == {}
        assert result["p_replay_payload"] == {}

    def test_handles_none_relationship_state(self, base_params):
        base_params["relationship_state"] = None
        result = build_commit_turn_rpc_payload(**base_params)
        assert result["p_relationship_state"] is None

    def test_handles_none_lease_owner(self, base_params):
        base_params["lease_owner"] = None
        result = build_commit_turn_rpc_payload(**base_params)
        assert result["p_lease_owner"] is None

    def test_handles_empty_outbox_events(self, base_params):
        base_params["outbox_events"] = []
        result = build_commit_turn_rpc_payload(**base_params)
        assert result["p_outbox_events"] == []


# ═══════════════════════════════════════════════════════════════════════
# 5. Result parsing
# ═══════════════════════════════════════════════════════════════════════


def _success_result() -> dict:
    return {
        "user_id": "user_123",
        "request_id": VALID_REQUEST_ID,
        "committed_revision": 1,
        "user_message_id": VALID_REQUEST_ID,
        "assistant_message_id": VALID_MESSAGE_ID,
        "replay_payload": _valid_replay_payload(),
        "outbox_events": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "event_type": "turn_completed",
                "idempotency_key": "turn_1_key",
                "turn_request_id": VALID_REQUEST_ID,
                "contract_version": 1,
            }
        ],
        "created_at": "2024-01-01T00:00:00Z",
        "completed_at": "2024-01-01T00:00:00Z",
    }


class TestParseCommitTurnResult:
    def test_parse_success_result(self):
        result = parse_commit_turn_result(_success_result())

        assert isinstance(result, CommittedTurn)
        assert result.user_id == "user_123"
        assert result.request_id == VALID_REQUEST_ID
        assert result.committed_revision == 1
        # user_message_id derived from request_id; assistant from replay message_id
        assert result.user_message_id == VALID_REQUEST_ID
        assert result.assistant_message_id == VALID_MESSAGE_ID
        assert result.replay_payload["response"] == "Hi there!"
        assert len(result.outbox_events) == 1
        assert isinstance(result.outbox_events[0], CommittedOutboxRef)
        assert result.outbox_events[0].event_type == "turn_completed"
        assert result.outbox_events[0].contract_version == 1
        # Operational fields must never leak into the public contract.
        assert not hasattr(result.outbox_events[0], "status")
        assert not hasattr(result.outbox_events[0], "attempts")
        assert not hasattr(result.outbox_events[0], "lease_owner")

    def test_parse_replay_request_id_must_match_result_request_id(self):
        result = _success_result()
        result["replay_payload"]["request_id"] = "00000000-0000-0000-0000-000000000000"

        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)

        assert exc.value.code == "invalid_replay_payload"

    def test_parse_error_result_conflict(self):
        error_result = {
            "error": {
                "code": "revision_mismatch",
                "message": "Profile revision does not match expected value",
                "expected_revision": 5,
                "actual_revision": 6,
            }
        }
        with pytest.raises(ConflictError) as exc:
            parse_commit_turn_result(error_result)

        error = exc.value
        assert error.code == "revision_mismatch"
        assert "does not match" in error.message
        assert error.expected_revision == 5
        assert error.actual_revision == 6

    def test_parse_error_result_request_conflict(self):
        error_result = {
            "error": {
                "code": "request_payload_conflict",
                "message": "Request ID already exists with different payload",
                "request_id": VALID_REQUEST_ID,
            }
        }
        with pytest.raises(ConflictError) as exc:
            parse_commit_turn_result(error_result)

        error = exc.value
        assert error.code == "request_payload_conflict"
        assert error.request_id == VALID_REQUEST_ID

    def test_parse_error_result_request_in_progress(self):
        error_result = {
            "error": {
                "code": "request_in_progress",
                "message": "Request is already in progress by another worker",
                "request_id": VALID_REQUEST_ID,
            }
        }
        with pytest.raises(ConflictError) as exc:
            parse_commit_turn_result(error_result)
        assert exc.value.code == "request_in_progress"

    def test_parse_error_result_lease_conflict(self):
        error_result = {
            "error": {
                "code": "lease_conflict",
                "message": "Request state changed; reclaim failed",
                "request_id": VALID_REQUEST_ID,
            }
        }
        with pytest.raises(ConflictError) as exc:
            parse_commit_turn_result(error_result)
        assert exc.value.code == "lease_conflict"

    def test_parse_error_result_validation_failed(self):
        error_result = {
            "error": {
                "code": "validation_failed",
                "message": "replay_payload must contain response",
            }
        }
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(error_result)
        assert exc.value.code == "validation_failed"

    def test_parse_error_result_database_error_is_persistence(self):
        # database_error must NEVER be classified as ValidationError/ConflictError.
        error_result = {
            "error": {
                "code": "database_error",
                "message": "internal database error",
            }
        }
        with pytest.raises(PersistenceError) as exc:
            parse_commit_turn_result(error_result)
        assert exc.value.code == "database_error"

    def test_parse_error_result_unknown_code_fails_closed(self):
        error_result = {"error": {"code": "mystery_code", "message": "x"}}
        with pytest.raises(PersistenceError):
            parse_commit_turn_result(error_result)

    def test_parse_invalid_result_not_mapping(self):
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result("not a mapping")
        assert "invalid_rpc_result" in str(exc.value)

    def test_parse_malformed_error_envelope(self):
        with pytest.raises(ValidationError):
            parse_commit_turn_result({"error": "not a mapping"})
        with pytest.raises(ValidationError):
            parse_commit_turn_result({"error": {"message": "no code"}})

    def test_parse_missing_field(self):
        result = _success_result()
        del result["user_id"]
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)
        assert "missing field" in str(exc.value)

    def test_parse_extra_field_rejected(self):
        result = _success_result()
        result["user_message_chat_log_id"] = 100
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)
        assert "unexpected field" in str(exc.value)

    def test_parse_bool_revision_rejected(self):
        result = _success_result()
        result["committed_revision"] = True
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)
        assert "invalid_committed_revision" in str(exc.value)

    def test_parse_negative_revision_rejected(self):
        result = _success_result()
        result["committed_revision"] = -1
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)
        assert "invalid_committed_revision" in str(exc.value)

    def test_parse_user_message_id_must_equal_request_id(self):
        result = _success_result()
        result["user_message_id"] = "99999999-9999-9999-9999-999999999999"
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)
        assert "invalid_user_message_id" in str(exc.value)

    def test_parse_assistant_message_id_must_match_replay(self):
        result = _success_result()
        result["assistant_message_id"] = "99999999-9999-9999-9999-999999999999"
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)
        assert "invalid_assistant_message_id" in str(exc.value)

    def test_parse_empty_outbox_events(self):
        result = _success_result()
        result["outbox_events"] = []
        parsed = parse_commit_turn_result(result)
        assert parsed.outbox_events == ()

    def test_parse_outbox_ref_with_operational_fields_rejected(self):
        result = _success_result()
        result["outbox_events"][0]["status"] = "pending"
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(result)
        assert "unexpected outbox ref field" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════
# 6. Data classes
# ═══════════════════════════════════════════════════════════════════════


class TestCommittedTurn:
    @pytest.fixture
    def committed_turn(self):
        return CommittedTurn(
            user_id="user_123",
            request_id=VALID_REQUEST_ID,
            committed_revision=1,
            user_message_id=VALID_REQUEST_ID,
            assistant_message_id=VALID_MESSAGE_ID,
            replay_payload=_valid_replay_payload(),
            outbox_events=[
                CommittedOutboxRef(
                    id="11111111-1111-1111-1111-111111111111",
                    event_type="turn_completed",
                    idempotency_key="turn_1_key",
                    turn_request_id=VALID_REQUEST_ID,
                    contract_version=1,
                )
            ],
            created_at="2024-01-01T00:00:00Z",
            completed_at="2024-01-01T00:00:01Z",
        )

    def test_to_db_row(self, committed_turn):
        row = committed_turn.to_db_row()
        assert row["user_id"] == "user_123"
        assert row["request_id"] == VALID_REQUEST_ID
        assert row["committed_revision"] == 1
        assert row["user_message_id"] == VALID_REQUEST_ID
        assert isinstance(row["replay_payload"], dict)
        assert isinstance(row["outbox_events"], list)
        assert set(row["outbox_events"][0]) == {
            "id", "event_type", "idempotency_key", "turn_request_id", "contract_version",
        }

    def test_immutable(self, committed_turn):
        with pytest.raises(FrozenInstanceError):
            committed_turn.user_id = "modified"


class TestCommittedOutboxRef:
    def test_immutable(self):
        ref = CommittedOutboxRef(
            id="11111111-1111-1111-1111-111111111111",
            event_type="turn_completed",
            idempotency_key="k",
            turn_request_id=VALID_REQUEST_ID,
            contract_version=1,
        )
        with pytest.raises(FrozenInstanceError):
            ref.id = "x"


class TestConflictError:
    def test_str_representation(self):
        error = ConflictError(
            code="revision_mismatch",
            message="Revision mismatch",
            expected_revision=5,
            actual_revision=6,
            request_id=VALID_REQUEST_ID,
        )
        error_str = str(error)
        assert "revision_mismatch" in error_str
        assert "Revision mismatch" in error_str
        assert "expected_revision=5" in error_str
        assert "actual_revision=6" in error_str
        assert f"request_id={VALID_REQUEST_ID}" in error_str

    def test_is_exception(self):
        error = ConflictError(code="test", message="test", expected_revision=0)
        assert isinstance(error, Exception)
        assert isinstance(error, ConflictError)

    def test_slots(self):
        error = ConflictError(code="test", message="test", expected_revision=0)
        assert hasattr(type(error), "__slots__")
        assert error.code == "test"
        assert error.message == "test"
        assert error.expected_revision == 0


class TestValidationError:
    def test_str_representation(self):
        error = ValidationError(code="test_error", message="Test message")
        assert str(error) == "test_error: Test message"

    def test_is_exception(self):
        error = ValidationError(code="test", message="test")
        assert isinstance(error, Exception)
        assert isinstance(error, ValidationError)

    def test_slots(self):
        error = ValidationError(code="test", message="test")
        assert hasattr(type(error), "__slots__")
        assert error.code == "test"
        assert error.message == "test"


class TestPersistenceError:
    def test_str_representation(self):
        error = PersistenceError(code="database_error", message="persistence error")
        assert str(error) == "database_error: persistence error"

    def test_is_exception(self):
        error = PersistenceError(code="database_error", message="error")
        assert isinstance(error, Exception)
        assert isinstance(error, PersistenceError)
        # Must not be classified as validation or conflict.
        assert not isinstance(error, ValidationError)
        assert not isinstance(error, ConflictError)

    def test_slots(self):
        error = PersistenceError(code="database_error", message="error")
        assert hasattr(type(error), "__slots__")
        assert error.code == "database_error"
        assert error.message == "error"


# ═══════════════════════════════════════════════════════════════════════
# 7. No shared mutable state
# ═══════════════════════════════════════════════════════════════════════


class TestNoSharedMutableState:
    """Ensure the module has no module-level mutable state."""

    def test_no_module_mutable_state(self):
        import backend.atomic_turn_commit as module

        for name in dir(module):
            if not name.startswith("_"):
                obj = getattr(module, name)
                if isinstance(obj, (dict, list, set)):
                    assert isinstance(obj, (frozenset, tuple)), (
                        f"Module-level mutable state: {name} is {type(obj).__name__}"
                    )

    def test_validation_is_stateless(self):
        validate_atomic_commit_input(
            authenticated_user_id="user_1",
            request_id="11111111-1111-1111-1111-111111111111",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload=_valid_replay_payload("Hi"),
        )
        validate_atomic_commit_input(
            authenticated_user_id="user_2",
            request_id="22222222-2222-2222-2222-222222222222",
            expected_revision=1,
            user_message="Goodbye",
            assistant_message="Bye",
            emotional_state=None,
            relationship_state=None,
            public_response="Bye",
            outbox_events=[],
            replay_payload=_valid_replay_payload("Bye"),
        )

    def test_payload_building_is_stateless(self):
        result1 = build_commit_turn_rpc_payload(
            authenticated_user_id="user_1",
            request_id="11111111-1111-1111-1111-111111111111",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload=_valid_replay_payload("Hi"),
            payload_hash_sha256="a" * 64,
            lease_owner=None,
        )
        result2 = build_commit_turn_rpc_payload(
            authenticated_user_id="user_2",
            request_id="22222222-2222-2222-2222-222222222222",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload=_valid_replay_payload("Hi"),
            payload_hash_sha256="b" * 64,
            lease_owner=None,
        )
        assert result1["p_authenticated_user_id"] == "user_1"
        assert result2["p_authenticated_user_id"] == "user_2"


# ═══════════════════════════════════════════════════════════════════════
# 8. Edge cases and boundary conditions
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_max_length_idempotency_key(self):
        _validate_idempotency_key("a" * 128)

    def test_max_length_lease_owner(self):
        _validate_lease_owner("a" * 64)

    def test_max_length_error_code(self):
        _validate_error_code("a" * 64)

    def test_exactly_128_char_key(self):
        _validate_idempotency_key("a" * 128)
        with pytest.raises(ValidationError):
            _validate_idempotency_key("a" * 129)

    def test_exactly_64_char_owner(self):
        _validate_lease_owner("a" * 64)
        with pytest.raises(ValidationError):
            _validate_lease_owner("a" * 65)

    def test_all_valid_idempotency_chars(self):
        _validate_idempotency_key("ABCabc012._:-")

    def test_valid_lease_owner_chars(self):
        _validate_lease_owner("Worker-1.2:test")

    def test_replay_payload_with_all_allowed_keys(self):
        payload = {
            "response": "test",
            "emotion_state": {},
            "message_id": VALID_MESSAGE_ID,
            "request_id": VALID_REQUEST_ID,
            "duration_ms": 100,
        }
        _validate_replay_payload(payload)

    def test_validation_with_large_revision(self):
        validate_atomic_commit_input(
            authenticated_user_id="user_1",
            request_id="11111111-1111-1111-1111-111111111111",
            expected_revision=2**60,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload=_valid_replay_payload("Hi"),
        )

    def test_validation_with_zero_revision(self):
        validate_atomic_commit_input(
            authenticated_user_id="user_1",
            request_id="11111111-1111-1111-1111-111111111111",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload=_valid_replay_payload("Hi"),
        )


# ═══════════════════════════════════════════════════════════════════════
# 9. Async commit_turn entry point
# ═══════════════════════════════════════════════════════════════════════


class TestAsyncCommitTurn:
    """Direct tests of the async commit_turn() entry point."""

    @pytest.fixture
    def valid_inputs(self):
        return {
            "authenticated_user_id": "user_123",
            "request_id": VALID_REQUEST_ID,
            "expected_revision": 0,
            "user_message": "Hello",
            "assistant_message": "Hi there!",
            "emotional_state": _valid_emotional_state(),
            "relationship_state": _valid_relationship_state(),
            "public_response": "Hi there!",
            "outbox_events": [("turn_completed", {"ref": "turn_1"}, "turn_1_key")],
            "replay_payload": _valid_replay_payload(),
        }

    @pytest.fixture
    def recording_rpc_client(self):
        """An async rpc client that records calls and returns a success result."""

        class _Recording:
            def __init__(self):
                self.calls = []

            async def __call__(self, name, params):
                self.calls.append((name, dict(params)))
                return _success_result()

        return _Recording()

    @pytest.mark.asyncio
    async def test_success(self, valid_inputs, recording_rpc_client):
        result = await commit_turn(
            rpc_client=recording_rpc_client,
            **valid_inputs,
        )
        assert isinstance(result, CommittedTurn)
        assert result.user_id == "user_123"
        assert result.request_id == VALID_REQUEST_ID
        assert result.committed_revision == 1

    @pytest.mark.asyncio
    async def test_rpc_called_once_with_commit_turn(self, valid_inputs, recording_rpc_client):
        await commit_turn(rpc_client=recording_rpc_client, **valid_inputs)
        assert len(recording_rpc_client.calls) == 1
        name, _ = recording_rpc_client.calls[0]
        assert name == "commit_turn"

    @pytest.mark.asyncio
    async def test_payload_sent(self, valid_inputs, recording_rpc_client):
        await commit_turn(rpc_client=recording_rpc_client, **valid_inputs)
        _, params = recording_rpc_client.calls[0]
        assert params["p_authenticated_user_id"] == "user_123"
        assert params["p_request_id"] == VALID_REQUEST_ID
        assert params["p_expected_revision"] == 0
        assert params["p_user_message"] == "Hello"
        assert params["p_assistant_message"] == "Hi there!"
        assert params["p_public_response"] == "Hi there!"
        assert params["p_replay_payload"]["message_id"] == VALID_MESSAGE_ID
        assert params["p_outbox_events"][0]["event_type"] == "turn_completed"

    @pytest.mark.asyncio
    async def test_canonical_hash_sent(self, valid_inputs, recording_rpc_client):
        await commit_turn(rpc_client=recording_rpc_client, **valid_inputs)
        _, params = recording_rpc_client.calls[0]
        sent_hash = params["p_payload_hash_sha256"]
        assert isinstance(sent_hash, str) and len(sent_hash) == 64
        assert all(c in "0123456789abcdef" for c in sent_hash)
        # Recompute the canonical hash independently and compare.
        canonical = {
            "authenticated_user_id": "user_123",
            "request_id": VALID_REQUEST_ID,
            "expected_revision": 0,
            "user_message": "Hello",
            "assistant_message": "Hi there!",
            "emotional_state": _valid_emotional_state(),
            "relationship_state": _valid_relationship_state(),
            "public_response": "Hi there!",
            "replay_payload": _valid_replay_payload(),
            "outbox_events": [
                {"event_type": "turn_completed", "payload": {"ref": "turn_1"}, "idempotency_key": "turn_1_key"}
            ],
        }
        assert sent_hash == canonical_payload_hash(canonical)

    @pytest.mark.asyncio
    async def test_rpc_not_called_on_invalid_input(self, valid_inputs, recording_rpc_client):
        valid_inputs["authenticated_user_id"] = ""
        with pytest.raises(ValidationError):
            await commit_turn(rpc_client=recording_rpc_client, **valid_inputs)
        assert len(recording_rpc_client.calls) == 0

    @pytest.mark.asyncio
    async def test_validation_before_rpc(self, valid_inputs, recording_rpc_client):
        valid_inputs["replay_payload"] = {"response": "mismatch"}
        with pytest.raises(ValidationError):
            await commit_turn(rpc_client=recording_rpc_client, **valid_inputs)
        assert len(recording_rpc_client.calls) == 0

    @pytest.mark.asyncio
    async def test_invalid_lease_owner_rejected_before_rpc(self, valid_inputs, recording_rpc_client):
        with pytest.raises(ValidationError):
            await commit_turn(
                rpc_client=recording_rpc_client,
                lease_owner="worker space",
                **valid_inputs,
            )
        assert len(recording_rpc_client.calls) == 0

    @pytest.mark.asyncio
    async def test_conflict_maps_to_conflict_error(self, valid_inputs):
        async def _conflict(name, params):
            return {
                "error": {
                    "code": "revision_mismatch",
                    "message": "Profile revision does not match",
                    "expected_revision": 0,
                    "actual_revision": 3,
                }
            }

        with pytest.raises(ConflictError) as exc:
            await commit_turn(rpc_client=_conflict, **valid_inputs)
        assert exc.value.code == "revision_mismatch"
        assert exc.value.actual_revision == 3

    @pytest.mark.asyncio
    async def test_request_in_progress_maps_to_conflict_error(self, valid_inputs):
        async def _in_progress(name, params):
            return {
                "error": {
                    "code": "request_in_progress",
                    "message": "Request is already in progress by another worker",
                    "request_id": VALID_REQUEST_ID,
                }
            }

        with pytest.raises(ConflictError) as exc:
            await commit_turn(rpc_client=_in_progress, **valid_inputs)
        assert exc.value.code == "request_in_progress"

    @pytest.mark.asyncio
    async def test_persistence_error_maps_to_persistence_error(self, valid_inputs):
        async def _boom(name, params):
            raise RuntimeError("underlying postgres failure detail")

        with pytest.raises(PersistenceError) as exc:
            await commit_turn(rpc_client=_boom, **valid_inputs)
        assert exc.value.code == "database_error"
        assert "underlying postgres failure detail" not in str(exc.value)
        assert str(exc.value) == "database_error: persistence error"

    @pytest.mark.asyncio
    async def test_malformed_result_fails_closed(self, valid_inputs):
        async def _malformed(name, params):
            return {"weird": "payload"}

        with pytest.raises(ValidationError) as exc:
            await commit_turn(rpc_client=_malformed, **valid_inputs)
        assert "invalid_rpc_result" in str(exc.value)

    @pytest.mark.asyncio
    async def test_with_lease_owner(self, valid_inputs, recording_rpc_client):
        result = await commit_turn(
            rpc_client=recording_rpc_client,
            lease_owner="worker-1",
            **valid_inputs,
        )
        assert isinstance(result, CommittedTurn)
        _, params = recording_rpc_client.calls[0]
        assert params["p_lease_owner"] == "worker-1"
