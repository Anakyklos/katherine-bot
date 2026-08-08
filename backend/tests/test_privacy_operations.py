"""
Tests for ``backend.privacy_operations`` (#314).

Covers:
 1. Pure importability from subprocess (no heavy deps, no env/socket/clock/
    randomness/filesystem at import)
 2. Neutral snapshot helpers produce valid v1 domain snapshots
 3. Input validation: operation, user id, operation id, payload shape,
    reset payloads requiring a valid v1 snapshot
 4. RPC payload building
 5. Result parsing: strict success contract, replay equality, error
    classification (conflict / validation / persistence)
 6. Async run_privacy_operation entry point (validation, single RPC call,
    success/conflict/malformed/persistence)
 7. Defense-in-depth validation before RPC (invalid input never calls RPC)
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap

import pytest

from backend.atomic_turn_commit import (
    ConflictError,
    PersistenceError,
    ValidationError,
)
from backend.privacy_operations import (
    OPERATION_DELETE_HISTORY,
    OPERATION_DELETE_MEMORIES,
    OPERATION_RESET_EMOTIONAL_STATE,
    OPERATION_RESET_RELATIONSHIP_STATE,
    PrivacyOperationResult,
    build_privacy_operation_rpc_payload,
    neutral_emotional_snapshot,
    neutral_relationship_snapshot,
    new_operation_id,
    parse_privacy_operation_result,
    run_privacy_operation,
    validate_privacy_operation_input,
)

VALID_OP_ID = "11111111-1111-1111-1111-111111111111"


def _payload(op_id: str = VALID_OP_ID) -> dict:
    return {"p_authenticated_user_id": "user-a", "p_operation_id": op_id, "p_operation_payload": {}}


def _success_result(
    op: str = OPERATION_DELETE_HISTORY,
    op_id: str = VALID_OP_ID,
    revision: int = 1,
    counts: dict | None = None,
) -> dict:
    return {
        "status": "applied",
        "operation": op,
        "operation_id": op_id,
        "user_id": "user-a",
        "revision": revision,
        "counts": counts
        if counts is not None
        else {
            "chat_logs": 2,
            "turn_requests": 1,
            "outbox_events": 1,
            "archival_extractions": 1,
            "memories": 0,
            "profiles": 0,
        },
    }


# ═══════════════════════════════════════════════════════════════════════
# 1. Pure importability (isolated subprocess, no side effects)
# ═══════════════════════════════════════════════════════════════════════

_PURITY_SCRIPT = textwrap.dedent(
    """
    import sys
    import threading

    import socket as _socket

    def _forbid(*args, **kwargs):
        raise AssertionError("network socket usage during import")

    _socket.socket.connect = _forbid
    _socket.socket.connect_ex = _forbid
    _socket.create_connection = _forbid

    import supabase as _supabase

    def _no_supabase_client(*args, **kwargs):
        raise AssertionError("real Supabase client constructed during import")

    _supabase.create_client = _no_supabase_client

    threads_before = len(threading.enumerate())

    import backend.privacy_operations

    threads_after = len(threading.enumerate())

    assert threads_after == threads_before, "import started a thread"
    print("PRIVACY_PURITY_OK")
    """
)


def test_privacy_operations_import_is_pure():
    result = subprocess.run(
        [sys.executable, "-c", _PURITY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "PRIVACY_PURITY_OK" in result.stdout


# ═══════════════════════════════════════════════════════════════════════
# 2. Neutral snapshot helpers
# ═══════════════════════════════════════════════════════════════════════


def test_neutral_emotional_snapshot_is_valid_v1():
    snapshot = neutral_emotional_snapshot(1700000000.0)
    assert snapshot["schema_version"] == 1
    assert snapshot["coping_mode"] == "HEALTHY"
    assert snapshot["pleasure"] == 0.0
    assert snapshot["connection"] == 0.5
    assert snapshot["timestamp"] == 1700000000.0
    assert set(snapshot) == {
        "schema_version", "pleasure", "arousal", "dominance", "libido",
        "aggression", "connection", "energy", "tension", "coping_mode",
        "timestamp",
    }


def test_neutral_relationship_snapshot_is_valid_v1():
    snapshot = neutral_relationship_snapshot(1700000000.0)
    assert snapshot["schema_version"] == 1
    assert snapshot["trust"] == 0.5
    assert snapshot["affection"] == 0.3
    assert snapshot["triggers"] == []
    assert set(snapshot) == {
        "schema_version", "trust", "affection", "tension", "triggers", "timestamp",
    }


def test_new_operation_id_is_canonical_uuid():
    import re

    op_id = new_operation_id()
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", op_id
    )
    assert new_operation_id() != op_id


# ═══════════════════════════════════════════════════════════════════════
# 3. Input validation
# ═══════════════════════════════════════════════════════════════════════


def test_validate_rejects_unknown_operation():
    with pytest.raises(ValidationError) as exc:
        validate_privacy_operation_input("drop_database", "user-a", VALID_OP_ID, {})
    assert exc.value.code == "invalid_operation"


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (OPERATION_DELETE_HISTORY, None),
        (OPERATION_DELETE_MEMORIES, "not-a-mapping"),
        (OPERATION_RESET_EMOTIONAL_STATE, None),
        (OPERATION_RESET_RELATIONSHIP_STATE, []),
    ],
)
def test_validate_rejects_bad_payload(operation, payload):
    with pytest.raises(ValidationError) as exc:
        validate_privacy_operation_input(operation, "user-a", VALID_OP_ID, payload)
    assert exc.value.code == "invalid_operation_payload"


@pytest.mark.parametrize(
    "bad_user_id",
    [None, "", 123],
)
def test_validate_rejects_bad_user_id(bad_user_id):
    with pytest.raises(ValidationError) as exc:
        validate_privacy_operation_input(
            OPERATION_DELETE_HISTORY, bad_user_id, VALID_OP_ID, {}
        )
    assert exc.value.code == "invalid_user_id"


@pytest.mark.parametrize(
    "bad_op_id",
    [None, "", "not-a-uuid", "11111111-1111-1111-1111-11111111111G", "ABCDEFAB-..."],
)
def test_validate_rejects_bad_operation_id(bad_op_id):
    with pytest.raises(ValidationError) as exc:
        validate_privacy_operation_input(
            OPERATION_DELETE_HISTORY, "user-a", bad_op_id, {}
        )
    assert exc.value.code == "invalid_operation_id"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot.update({"coping_mode": "INVALID"}),
        lambda snapshot: snapshot.pop("energy"),
        lambda snapshot: snapshot.update({"pleasure": 2.0}),
        lambda snapshot: snapshot.update({"user_id": "attacker"}),
        lambda snapshot: snapshot.update({"schema_version": 2}),
    ],
)
def test_validate_rejects_forged_emotional_reset_snapshot(mutate):
    snapshot = neutral_emotional_snapshot(1700000000.0)
    mutate(snapshot)
    with pytest.raises(ValidationError):
        validate_privacy_operation_input(
            OPERATION_RESET_EMOTIONAL_STATE, "user-a", VALID_OP_ID, snapshot
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot.update({"triggers": ["x" * 129]}),
        lambda snapshot: snapshot.update({"trust": -0.1}),
        lambda snapshot: snapshot.pop("affection"),
        lambda snapshot: snapshot.update({"bond_label": "hidden"}),
    ],
)
def test_validate_rejects_forged_relationship_reset_snapshot(mutate):
    snapshot = neutral_relationship_snapshot(1700000000.0)
    mutate(snapshot)
    with pytest.raises(ValidationError):
        validate_privacy_operation_input(
            OPERATION_RESET_RELATIONSHIP_STATE, "user-a", VALID_OP_ID, snapshot
        )


def test_validate_accepts_valid_inputs():
    validate_privacy_operation_input(
        OPERATION_DELETE_HISTORY, "user-a", VALID_OP_ID, {}
    )
    validate_privacy_operation_input(
        OPERATION_RESET_EMOTIONAL_STATE,
        "user-a",
        VALID_OP_ID,
        neutral_emotional_snapshot(1700000000.0),
    )
    validate_privacy_operation_input(
        OPERATION_RESET_RELATIONSHIP_STATE,
        "user-a",
        VALID_OP_ID,
        neutral_relationship_snapshot(1700000000.0),
    )


# ═══════════════════════════════════════════════════════════════════════
# 4. RPC payload building
# ═══════════════════════════════════════════════════════════════════════


def test_build_rpc_payload():
    payload = build_privacy_operation_rpc_payload(
        OPERATION_DELETE_HISTORY, "user-a", VALID_OP_ID, {"reason": "x"}
    )
    assert payload == {
        "p_authenticated_user_id": "user-a",
        "p_operation_id": VALID_OP_ID,
        "p_operation_payload": {"reason": "x"},
    }


# ═══════════════════════════════════════════════════════════════════════
# 5. Result parsing
# ═══════════════════════════════════════════════════════════════════════


def test_parse_success_result():
    parsed = parse_privacy_operation_result(
        _success_result(), OPERATION_DELETE_HISTORY, VALID_OP_ID
    )
    assert isinstance(parsed, PrivacyOperationResult)
    assert parsed.operation == OPERATION_DELETE_HISTORY
    assert parsed.operation_id == VALID_OP_ID
    assert parsed.user_id == "user-a"
    assert parsed.revision == 1
    assert parsed.counts["chat_logs"] == 2
    assert parsed.to_db_row()["status"] == "applied"


def test_parse_replay_result_is_identical():
    first = parse_privacy_operation_result(
        _success_result(), OPERATION_DELETE_HISTORY, VALID_OP_ID
    )
    second = parse_privacy_operation_result(
        _success_result(), OPERATION_DELETE_HISTORY, VALID_OP_ID
    )
    assert second.to_db_row() == first.to_db_row()


def test_parse_conflict_result():
    with pytest.raises(ConflictError) as exc:
        parse_privacy_operation_result(
            {
                "error": {
                    "code": "operation_conflict",
                    "message": "operation_id already used with a different operation or payload",
                }
            },
            OPERATION_DELETE_HISTORY,
            VALID_OP_ID,
        )
    assert exc.value.code == "operation_conflict"


def test_parse_validation_error_result():
    with pytest.raises(ValidationError) as exc:
        parse_privacy_operation_result(
            {
                "error": {
                    "code": "validation_failed",
                    "message": "authenticated_user_id is required",
                }
            },
            OPERATION_DELETE_HISTORY,
            VALID_OP_ID,
        )
    assert exc.value.code == "validation_failed"


def test_parse_unknown_error_is_sanitized_persistence():
    with pytest.raises(PersistenceError) as exc:
        parse_privacy_operation_result(
            {"error": {"code": "database_error", "message": "SECRET_LEAK"}},
            OPERATION_DELETE_HISTORY,
            VALID_OP_ID,
        )
    assert "SECRET_LEAK" not in str(exc.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda result: result.pop("status"),
        lambda result: result.update({"extra": 1}),
        lambda result: result.update({"operation": OPERATION_DELETE_MEMORIES}),
        lambda result: result.update({"operation_id": "22222222-2222-2222-2222-222222222222"}),
        lambda result: result.update({"revision": -1}),
        lambda result: result.update({"revision": True}),
        lambda result: result.update({"counts": {"memories": 1}}),
        lambda result: result.update({"counts": {"chat_logs": -1, "turn_requests": 0,
                                                  "outbox_events": 0, "archival_extractions": 0,
                                                  "memories": 0, "profiles": 0}}),
    ],
)
def test_parse_rejects_malformed_success(mutate):
    result = _success_result()
    mutate(result)
    with pytest.raises(ValidationError):
        parse_privacy_operation_result(result, OPERATION_DELETE_HISTORY, VALID_OP_ID)


# ═══════════════════════════════════════════════════════════════════════
# 6/7. Async entry point: validation before RPC, single call, classification
# ═══════════════════════════════════════════════════════════════════════


def test_run_validates_before_rpc():
    calls = []

    async def rpc_client(name: str, params: dict) -> dict:
        calls.append((name, params))
        return _success_result()

    with pytest.raises(ValidationError):
        asyncio.run(
            run_privacy_operation(
                rpc_client=rpc_client,
                operation="bogus",
                authenticated_user_id="user-a",
                operation_id=VALID_OP_ID,
                payload={},
            )
        )
    assert calls == [], "RPC must never be called for invalid input"


def test_run_success_single_call():
    calls = []

    async def rpc_client(name: str, params: dict) -> dict:
        calls.append((name, params))
        return _success_result()

    result = asyncio.run(
        run_privacy_operation(
            rpc_client=rpc_client,
            operation=OPERATION_DELETE_HISTORY,
            authenticated_user_id="user-a",
            operation_id=VALID_OP_ID,
            payload={},
        )
    )
    assert len(calls) == 1
    assert calls[0][0] == "delete_history"
    assert calls[0][1]["p_authenticated_user_id"] == "user-a"
    assert calls[0][1]["p_operation_id"] == VALID_OP_ID
    assert result.to_db_row() == _success_result()


def test_run_uses_correct_rpc_name_per_operation():
    expected = {
        OPERATION_DELETE_HISTORY: "delete_history",
        OPERATION_DELETE_MEMORIES: "delete_memories",
        OPERATION_RESET_EMOTIONAL_STATE: "reset_emotional_state",
        OPERATION_RESET_RELATIONSHIP_STATE: "reset_relationship_state",
    }
    for operation, rpc_name in expected.items():
        seen = {}
        payload = (
            neutral_emotional_snapshot(1700000000.0)
            if operation == OPERATION_RESET_EMOTIONAL_STATE
            else neutral_relationship_snapshot(1700000000.0)
            if operation == OPERATION_RESET_RELATIONSHIP_STATE
            else {}
        )

        async def rpc_client(name: str, params: dict) -> dict:
            seen["name"] = name
            return _success_result(op=operation, op_id=VALID_OP_ID)

        result = asyncio.run(
            run_privacy_operation(
                rpc_client=rpc_client,
                operation=operation,
                authenticated_user_id="user-a",
                operation_id=VALID_OP_ID,
                payload=payload,
            )
        )
        assert seen["name"] == rpc_name
        assert result.operation == operation


def test_run_conflict_propagates():
    async def rpc_client(name: str, params: dict) -> dict:
        return {
            "error": {
                "code": "operation_conflict",
                "message": "operation_id already used with a different operation or payload",
            }
        }

    with pytest.raises(ConflictError) as exc:
        asyncio.run(
            run_privacy_operation(
                rpc_client=rpc_client,
                operation=OPERATION_DELETE_HISTORY,
                authenticated_user_id="user-a",
                operation_id=VALID_OP_ID,
                payload={},
            )
        )
    assert exc.value.code == "operation_conflict"


def test_run_rpc_exception_is_sanitized_persistence():
    async def rpc_client(name: str, params: dict) -> dict:
        raise RuntimeError("SECRET_CONNECTION_LEAK")

    with pytest.raises(PersistenceError) as exc:
        asyncio.run(
            run_privacy_operation(
                rpc_client=rpc_client,
                operation=OPERATION_DELETE_HISTORY,
                authenticated_user_id="user-a",
                operation_id=VALID_OP_ID,
                payload={},
            )
        )
    assert "SECRET_CONNECTION_LEAK" not in str(exc.value)
    assert "persistence error" in str(exc.value)
