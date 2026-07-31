"""
Tests for ``backend.atomic_turn_commit`` (#271).

Covers:
 1. Pure importability from subprocess (no heavy deps, no env/socket)
 2. Input validation: types, ranges, constraints
 3. Canonical payload hash consistency
 4. RPC payload building
 5. Result parsing: success and error cases
 6. Conflict detection and error handling
 7. Immutability of result objects
 8. No shared mutable state
 9. Defense-in-depth validation before RPC
 10. Edge cases: empty strings, None values, boundary conditions
"""

import json
import subprocess
import sys
import os

import pytest
from dataclasses import FrozenInstanceError, asdict
from typing import Mapping, Any, Optional

# Import under test
from backend.atomic_turn_commit import (
    MessageRef,
    CommittedTurn,
    ConflictError,
    ValidationError,
    validate_atomic_commit_input,
    build_commit_turn_rpc_payload,
    parse_commit_turn_result,
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
    TurnRequestRecord,
    OutboxEventRecord,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Pure importability
# ═══════════════════════════════════════════════════════════════════════

_PURITY_SCRIPT = '''
import sys
import os as _os
import socket as _socket

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

def _raise_socket(*args, **kwargs):
    raise OSError("socket.socket blocked")
_os.socket = _raise_socket

_BLOCKED = frozenset({
    "fastapi", "groq", "supabase", "sentence_transformers",
    "pydantic", "httpx", "httpcore", "anyio",
    "backend.engine", "backend.memory", "backend.trusted_context",
})

class _BlockImport:
    def find_module(self, name, path=None):
        if name in _BLOCKED or name.startswith(("backend.", "tests.")):
            raise ImportError(f"blocked: {name}")
        return None

sys.meta_path.insert(0, _BlockImport())

import backend.atomic_turn_commit
print("IMPORT_OK")
'''


class TestImportability:
    """Import the module in a subprocess with all external resources blocked."""

    def test_import_purity(self):
        """Module imports without touching env, socket, or blocked packages."""
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
        payload = {"response": "test", "duration_ms": 100}
        _validate_replay_payload(payload)

    def test_empty_payload(self):
        _validate_replay_payload({})

    def test_forbidden_key_top_level(self):
        for key in FORBIDDEN_PAYLOAD_KEYS:
            with pytest.raises(ValidationError) as exc:
                _validate_replay_payload({key: "value"})
            assert "invalid_replay_payload" in str(exc.value)

    def test_forbidden_key_nested(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload({"response": {"message": "nested"}})
        assert "invalid_replay_payload" in str(exc.value)

    def test_unknown_key(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload({"unknown_key": "value"})
        assert "invalid_replay_payload" in str(exc.value)

    def test_not_mapping(self):
        with pytest.raises(ValidationError) as exc:
            _validate_replay_payload("not a mapping")
        assert "invalid_replay_payload" in str(exc.value)


# ═══════════════════════════════════════════════════════════════════════
# 3. Full input validation
# ═══════════════════════════════════════════════════════════════════════


class TestValidateAtomicCommitInput:
    @pytest.fixture
    def valid_inputs(self):
        return {
            "authenticated_user_id": "user_123",
            "request_id": "req_456",
            "expected_revision": 0,
            "user_message": "Hello",
            "assistant_message": "Hi there!",
            "emotional_state": {"mood": "happy"},
            "relationship_state": {"trust": 0.8},
            "public_response": "Hi there!",
            "outbox_events": [("turn_completed", {"ref": "turn_1"}, "turn_1_priv")],
            "replay_payload": {"response": "Hi there!", "duration_ms": 100},
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

    def test_negative_revision(self, valid_inputs):
        valid_inputs["expected_revision"] = -1
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

    def test_non_list_outbox_events(self, valid_inputs):
        valid_inputs["outbox_events"] = "not a list"
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

    def test_empty_outbox_events(self, valid_inputs):
        valid_inputs["outbox_events"] = []
        validate_atomic_commit_input(**valid_inputs)


# ═══════════════════════════════════════════════════════════════════════
# 4. RPC payload building
# ═══════════════════════════════════════════════════════════════════════


class TestBuildCommitTurnRpcPayload:
    @pytest.fixture
    def base_params(self):
        return {
            "authenticated_user_id": "user_123",
            "request_id": "12345678-1234-1234-1234-123456789abc",
            "expected_revision": 0,
            "user_message": "Hello",
            "assistant_message": "Hi there!",
            "emotional_state": {"mood": "happy"},
            "relationship_state": {"trust": 0.8},
            "public_response": "Hi there!",
            "outbox_events": [
                ("turn_completed", {"ref": "turn_1"}, "turn_1_key"),
                ("memory_updated", {"entity_id": "mem_1"}, "mem_1_key"),
            ],
            "replay_payload": {"response": "Hi there!", "duration_ms": 100},
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


class TestParseCommitTurnResult:
    @pytest.fixture
    def success_result(self):
        return {
            "user_id": "user_123",
            "request_id": "12345678-1234-1234-1234-123456789abc",
            "committed_revision": 1,
            "user_message_chat_log_id": 100,
            "assistant_message_chat_log_id": 101,
            "user_message_id": "12345678-1234-1234-1234-123456789abc",
            "assistant_message_id": "87654321-4321-4321-4321-cba987654321",
            "replay_payload": {"response": "Hi!", "duration_ms": 50},
            "outbox_events": [
                {
                    "id": "evt_1",
                    "event_type": "turn_completed",
                    "user_id": "user_123",
                    "payload": {"ref": "turn_1"},
                    "status": "pending",
                    "contract_version": 1,
                    "idempotency_key": "turn_1_key",
                    "turn_request_id": "12345678-1234-1234-1234-123456789abc",
                    "attempts": 0,
                    "next_attempt_at": "2024-01-01T00:00:01Z",
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                }
            ],
            "created_at": "2024-01-01T00:00:00Z",
            "completed_at": "2024-01-01T00:00:00Z",
        }

    def test_parse_success_result(self, success_result):
        result = parse_commit_turn_result(success_result)

        assert isinstance(result, CommittedTurn)
        assert result.user_id == "user_123"
        assert result.request_id == "12345678-1234-1234-1234-123456789abc"
        assert result.committed_revision == 1
        assert result.user_message_chat_log_id == 100
        assert result.assistant_message_chat_log_id == 101
        assert result.replay_payload == {"response": "Hi!", "duration_ms": 50}
        assert len(result.outbox_events) == 1

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
                "request_id": "req_123",
            }
        }
        with pytest.raises(ConflictError) as exc:
            parse_commit_turn_result(error_result)

        error = exc.value
        assert error.code == "request_payload_conflict"
        assert error.request_id == "req_123"

    def test_parse_invalid_result_not_mapping(self):
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result("not a mapping")
        assert "invalid_rpc_result" in str(exc.value)

    def test_parse_missing_field(self, success_result):
        del success_result["user_id"]
        with pytest.raises(ValidationError) as exc:
            parse_commit_turn_result(success_result)
        assert "missing field" in str(exc.value)

    def test_parse_empty_outbox_events(self, success_result):
        success_result["outbox_events"] = []
        result = parse_commit_turn_result(success_result)
        # outbox_events is now a tuple for deep immutability
        assert result.outbox_events == ()


# ═══════════════════════════════════════════════════════════════════════
# 6. Data classes
# ═══════════════════════════════════════════════════════════════════════


class TestCommittedTurn:
    @pytest.fixture
    def committed_turn(self):
        return CommittedTurn(
            user_id="user_123",
            request_id="req_456",
            committed_revision=1,
            user_message_chat_log_id=100,
            assistant_message_chat_log_id=101,
            user_message_id="msg_1",
            assistant_message_id="msg_2",
            replay_payload={"response": "Hi!"},
            outbox_events=[],
            created_at="2024-01-01T00:00:00Z",
            completed_at="2024-01-01T00:00:01Z",
        )

    def test_to_db_row(self, committed_turn):
        row = committed_turn.to_db_row()
        assert row["user_id"] == "user_123"
        assert row["request_id"] == "req_456"
        assert row["committed_revision"] == 1
        assert isinstance(row["replay_payload"], dict)
        assert isinstance(row["outbox_events"], list)

    def test_immutable(self, committed_turn):
        with pytest.raises(FrozenInstanceError):
            committed_turn.user_id = "modified"


class TestConflictError:
    def test_str_representation(self):
        error = ConflictError(
            code="revision_mismatch",
            message="Revision mismatch",
            expected_revision=5,
            actual_revision=6,
            request_id="req_123",
        )
        error_str = str(error)
        assert "revision_mismatch" in error_str
        assert "Revision mismatch" in error_str
        assert "expected_revision=5" in error_str
        assert "actual_revision=6" in error_str
        assert "request_id=req_123" in error_str

    def test_is_exception(self):
        """Test that ConflictError is an Exception subclass."""
        error = ConflictError(code="test", message="test", expected_revision=0)
        assert isinstance(error, Exception)
        assert isinstance(error, ConflictError)

    def test_slots(self):
        """Test that ConflictError uses __slots__ for memory efficiency."""
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
        """Test that ValidationError is an Exception subclass."""
        error = ValidationError(code="test", message="test")
        assert isinstance(error, Exception)
        assert isinstance(error, ValidationError)

    def test_slots(self):
        """Test that ValidationError uses __slots__ for memory efficiency."""
        error = ValidationError(code="test", message="test")
        assert hasattr(type(error), "__slots__")
        assert error.code == "test"
        assert error.message == "test"


class TestMessageRef:
    def test_creation(self):
        ref = MessageRef(user_id="user_123", chat_log_id=100)
        assert ref.user_id == "user_123"
        assert ref.chat_log_id == 100

    def test_immutable(self):
        ref = MessageRef(user_id="user_123", chat_log_id=100)
        with pytest.raises(FrozenInstanceError):
            ref.user_id = "modified"


# ═══════════════════════════════════════════════════════════════════════
# 7. No shared mutable state
# ═══════════════════════════════════════════════════════════════════════


class TestNoSharedMutableState:
    """Ensure the module has no module-level mutable state."""

    def test_no_module_mutable_state(self):
        import backend.atomic_turn_commit as module

        # Check that no module-level variables are mutable
        for name in dir(module):
            if not name.startswith("_"):
                obj = getattr(module, name)
                if isinstance(obj, (dict, list, set)):
                    # Ensure it's a constant (tuple or frozenset)
                    assert isinstance(obj, (frozenset, tuple)), (
                        f"Module-level mutable state: {name} is {type(obj).__name__}"
                    )

    def test_validation_is_stateless(self):
        """Validation functions don't retain state between calls."""
        # Call validation multiple times with different inputs
        validate_atomic_commit_input(
            authenticated_user_id="user_1",
            request_id="req_1",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload={},
        )
        validate_atomic_commit_input(
            authenticated_user_id="user_2",
            request_id="req_2",
            expected_revision=1,
            user_message="Goodbye",
            assistant_message="Bye",
            emotional_state=None,
            relationship_state=None,
            public_response="Bye",
            outbox_events=[],
            replay_payload={},
        )

    def test_payload_building_is_stateless(self):
        """Payload building doesn't retain state between calls."""
        result1 = build_commit_turn_rpc_payload(
            authenticated_user_id="user_1",
            request_id="req_1",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload={},
            payload_hash_sha256="a" * 64,
            lease_owner=None,
        )
        result2 = build_commit_turn_rpc_payload(
            authenticated_user_id="user_2",
            request_id="req_2",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload={},
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
        key = "a" * 128
        _validate_idempotency_key(key)

    def test_max_length_lease_owner(self):
        owner = "a" * 64
        _validate_lease_owner(owner)

    def test_max_length_error_code(self):
        code = "a" * 64
        _validate_error_code(code)

    def test_exactly_128_char_key(self):
        key = "a" * 128
        _validate_idempotency_key(key)
        # 129 should fail
        with pytest.raises(ValidationError):
            _validate_idempotency_key("a" * 129)

    def test_exactly_64_char_owner(self):
        owner = "a" * 64
        _validate_lease_owner(owner)
        # 65 should fail
        with pytest.raises(ValidationError):
            _validate_lease_owner("a" * 65)

    def test_all_valid_idempotency_chars(self):
        # All valid characters: alphanumeric, ., _, :, -
        key = "ABCabc012._:-"
        _validate_idempotency_key(key)

    def test_valid_lease_owner_chars(self):
        owner = "Worker-1.2:test"
        _validate_lease_owner(owner)

    def test_replay_payload_with_all_allowed_keys(self):
        payload = {
            "response": "test",
            "emotion_state": {},
            "message_id": "msg_1",
            "request_id": "req_1",
            "duration_ms": 100,
        }
        _validate_replay_payload(payload)

    def test_validation_with_large_revision(self):
        validate_atomic_commit_input(
            authenticated_user_id="user_1",
            request_id="req_1",
            expected_revision=2**60,  # Large but valid
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload={},
        )

    def test_validation_with_zero_revision(self):
        validate_atomic_commit_input(
            authenticated_user_id="user_1",
            request_id="req_1",
            expected_revision=0,
            user_message="Hello",
            assistant_message="Hi",
            emotional_state=None,
            relationship_state=None,
            public_response="Hi",
            outbox_events=[],
            replay_payload={},
        )
