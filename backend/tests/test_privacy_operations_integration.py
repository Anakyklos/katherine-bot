"""Real Supabase integration tests for the privacy data operations (#314).

This file is executed ONLY by the database CI job against a freshly reset
local Supabase instance (no mocks: real PostgreSQL transactions, locks, FKs
and rollbacks). It must never be collected by the ordinary backend unit job.

Covers the 16 mandatory scenarios from issue #314:

 1.  delete_history removes chat_logs, turn_requests, derived outbox_events
     and archival_extractions, preserving memories and snapshots
 2.  delete_memories removes memories/candidates, preserving chat and
     snapshots
 3.  reset_emotional_state produces a valid v1 neutral snapshot and preserves
     relationship/history/memories
 4.  reset_relationship_state produces a valid v1 neutral snapshot and
     preserves emotional/history/memories
 5.  each operation increments profiles.revision exactly once
 6.  retry of the same operation_id returns the stored replay without a new
     mutation or revision increment
 7.  divergent operation/payload on the same operation_id is a sanitized
     conflict
 8.  an injected mid-operation failure causes total rollback
 9.  two concurrent operations of the same user never interleave partial
     state (same per-user advisory lock as commit_turn)
10.  users A and B remain fully isolated (no global lock)
11.  admission_reservations survive delete_history (no quota bypass)
12.  PUBLIC/anon/authenticated cannot execute the RPCs
13.  service_role holds only the minimal necessary grants
14.  errors and results are sanitized (no user_id/content/prompt/memory/HMAC/
     raw SQL leakage)
15.  migration applies on a clean reset (this suite) and on a legacy upgrade
     (backend/tests/test_privacy_operations_legacy.py)
16.  pgTAP + database integration + existing backend/frontend stay green (CI)

Concurrency tests use deterministic barrier coordination, never sleeps.
Failure injection uses disposable triggers created only in the local test
database (never public RPC failpoints).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from postgrest.exceptions import APIError
from supabase import Client, create_client

from backend.atomic_turn_commit import ConflictError, PersistenceError, ValidationError
from backend.privacy_operations import (
    OPERATION_DELETE_HISTORY,
    OPERATION_DELETE_MEMORIES,
    neutral_emotional_snapshot,
    neutral_relationship_snapshot,
    new_operation_id,
    run_privacy_operation,
)

_SUPABASE_CLI = ["supabase"] if shutil.which("supabase") else ["npx", "supabase"]

# ---------------------------------------------------------------------------
# Environment / clients
# ---------------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for privacy operations integration tests"
    return value


@pytest.fixture(scope="module")
def supabase_url() -> str:
    return _required_env("SUPABASE_URL")


@pytest.fixture(scope="module")
def anon_key() -> str:
    return _required_env("SUPABASE_ANON_KEY")


@pytest.fixture(scope="module")
def service_role_key() -> str:
    return _required_env("SUPABASE_SERVICE_ROLE_KEY")


def _close_http_transports(client: Client) -> None:
    if client is None:
        return
    for attr, session_attr in (
        ("_postgrest", "session"),
        ("_storage", "session"),
        ("_functions", "_client"),
    ):
        transport = getattr(client, attr, None)
        if transport is None:
            continue
        session = getattr(transport, session_attr, None)
        if session is not None and hasattr(session, "close"):
            session.close()


def _close_client(client: Client) -> None:
    if client is None:
        return
    _close_http_transports(client)
    auth = getattr(client, "auth", None)
    if auth is not None and hasattr(auth, "close"):
        auth.close()


@pytest.fixture(scope="module")
def service_client(supabase_url: str, service_role_key: str) -> Client:
    client = create_client(supabase_url, service_role_key)
    yield client
    _close_client(client)


@pytest.fixture(scope="module")
def anon_client(supabase_url: str, anon_key: str) -> Client:
    client = create_client(supabase_url, anon_key)
    yield client
    _close_client(client)


@pytest.fixture(scope="module")
def auth_client(supabase_url: str, anon_key: str, service_client: Client) -> tuple[Client, str]:
    """A real authenticated user (client, user_id)."""
    email = "privacy-ops-auth@test.local"
    password = "password123"
    client = create_client(supabase_url, anon_key)

    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)

    client.auth.sign_up({"email": email, "password": password})
    response = client.auth.sign_in_with_password({"email": email, "password": password})
    assert response is not None and response.user is not None
    assert response.session is not None and response.session.access_token is not None
    yield client, response.user.id

    _close_http_transports(client)
    client.auth.sign_out()
    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)
    _close_client(client)


# ---------------------------------------------------------------------------
# SQL helpers (pinned local Supabase CLI, sanitized)
# ---------------------------------------------------------------------------


def _run_sql(sql: str) -> list[dict]:
    """Execute trusted test SQL through the pinned local Supabase CLI."""
    child_env = dict(os.environ)
    child_env["SUPABASE_TELEMETRY_DISABLED"] = "1"
    child_env["SUPABASE_ANALYTICS_ENABLED"] = "false"
    result = subprocess.run(
        [
            *_SUPABASE_CLI,
            "db",
            "query",
            "--agent=no",
            "--output",
            "json",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
    )
    assert result.returncode == 0, "sanitized privacy test SQL operation failed"
    output = result.stdout.strip()
    if not output or output[0] not in "[{":
        return []
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    return parsed


def _count(table: str, user_id: str) -> int:
    rows = _run_sql(
        f"SELECT count(*)::integer AS count FROM public.{table} "
        f"WHERE user_id = '{user_id}'"
    )
    return rows[0]["count"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uid(label: str) -> str:
    return f"prv_{label}_{uuid.uuid4().hex[:12]}"


def _emotional_state() -> dict:
    return {
        "schema_version": 1, "pleasure": 0.9, "arousal": 0.8, "dominance": 0.7,
        "libido": 0.1, "aggression": 0.1, "connection": 0.5, "energy": 0.8,
        "tension": 0.1, "coping_mode": "MANIC", "timestamp": 1700000000.0,
    }


def _relationship_state() -> dict:
    return {
        "schema_version": 1, "trust": 0.9, "affection": 0.8, "tension": 0.1,
        "triggers": [], "timestamp": 1700000000.0,
    }


def _seed_user(
    user_id: str,
    *,
    revision: int = 2,
    with_turn: bool = True,
    with_memory: bool = True,
    with_admission: bool = True,
) -> None:
    """Seed a realistic user with history, memory, snapshots and ledger rows."""
    _run_sql(
        "INSERT INTO public.profiles (user_id, persona_config, user_profile, "
        "relationship_state, emotional_state, revision) VALUES "
        f"('{user_id}', 'persona-config', '{{}}'::jsonb, "
        f"'{json.dumps(_relationship_state())}'::jsonb, "
        f"'{json.dumps(_emotional_state())}'::jsonb, {revision})"
    )
    _run_sql(
        "INSERT INTO public.chat_logs (user_id, role, content) VALUES "
        f"('{user_id}', 'user', 'hello'), ('{user_id}', 'assistant', 'hi there')"
    )
    if with_turn:
        turn_id = str(uuid.uuid4())
        _run_sql(
            "INSERT INTO public.turn_requests ("
            "id, user_id, request_id, payload_hash_sha256, status, expected_revision, "
            "committed_revision, replay_payload, created_at, updated_at, completed_at) "
            "VALUES "
            f"('{turn_id}', '{user_id}', '{uuid.uuid4()}', "
            f"'{'a' * 64}', 'completed', 0, {revision}, "
            f"'{{\"response\":\"hi there\",\"message_id\":\"{uuid.uuid4()}\"}}'::jsonb, "
            "now(), now(), now())"
        )
        _run_sql(
            "INSERT INTO public.outbox_events ("
            "event_type, contract_version, user_id, turn_request_id, payload, status, "
            "attempts, next_attempt_at, idempotency_key, created_at, updated_at) VALUES "
            f"('turn_completed', 1, '{user_id}', '{turn_id}', "
            f"'{{\"ref\":\"t1\"}}'::jsonb, 'pending', 0, now() + interval '1 second', "
            f"'{user_id}-k1', now(), now())"
        )
    if with_memory:
        _run_sql(
            "INSERT INTO public.memories (user_id, content, metadata) VALUES "
            f"('{user_id}', 'a durable memory', '{{\"tags\":[\"x\"]}}'::jsonb)"
        )
    _run_sql(
        "INSERT INTO public.archival_extractions ("
        "user_id, source_chat_log_id, extractor_version, schema_version, "
        "idempotency_key, facts) "
        "SELECT '{user_id}', id, 1, 1, '{user_id}-arch-1', '{{\"facts\":[]}}'::jsonb "
        "FROM public.chat_logs WHERE user_id = '{user_id}' AND role = 'user'".format(
            user_id=user_id
        )
    )
    if with_admission:
        _run_sql(
            "INSERT INTO public.admission_reservations ("
            "user_id, request_id, message_hmac_sha256, network_hmac_sha256, "
            "estimated_units) VALUES "
            f"('{user_id}', '{uuid.uuid4()}', repeat('a', 64), repeat('b', 64), 10)"
        )


def _cleanup_user(user_id: str) -> None:
    for table in (
        "privacy_operations",
        "admission_reservations",
        "archival_extractions",
        "memories",
        "chat_logs",
        "turn_requests",
        "outbox_events",
        "profiles",
    ):
        _run_sql(f"DELETE FROM public.{table} WHERE user_id = '{user_id}'")


def _call_rpc(client: Client, name: str, params: dict) -> dict:
    """Invoke a privacy RPC and return the parsed result object."""
    response = client.rpc(name, params).execute()
    data = response.data
    if isinstance(data, list):
        assert len(data) == 1
        data = data[0]
    assert isinstance(data, dict), f"unexpected RPC response shape: {type(data).__name__}"
    return data


def _rpc_params(user_id: str, operation_id: str, payload: dict | None = None) -> dict:
    return {
        "p_authenticated_user_id": user_id,
        "p_operation_id": operation_id,
        "p_operation_payload": payload if payload is not None else {},
    }


@dataclass(frozen=True)
class _ConcurrentOp:
    user_id: str
    rpc_name: str
    operation_id: str
    payload: dict


def _run_concurrent(
    *,
    url: str,
    key: str,
    barrier: threading.Barrier,
    calls: list[_ConcurrentOp],
    timeout: int = 30,
) -> list[dict]:
    """Run privacy RPCs concurrently and return each call's parsed result."""

    def _one(call: _ConcurrentOp) -> dict:
        client = create_client(url, key)
        try:
            barrier.wait(timeout=timeout)
            return _call_rpc(
                client,
                call.rpc_name,
                _rpc_params(call.user_id, call.operation_id, call.payload),
            )
        finally:
            _close_client(client)

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(_one, call) for call in calls]
        return [f.result(timeout=timeout) for f in futures]


# ---------------------------------------------------------------------------
# Failpoint triggers (created only in the local test DB)
# ---------------------------------------------------------------------------


def _install_delete_trigger(name: str, target: str, message: str) -> None:
    trigger_fn = f"prv_{name}_fn"
    _run_sql(
        f"CREATE OR REPLACE FUNCTION public.{trigger_fn}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"RAISE EXCEPTION '{message}'; END $$;"
    )
    _run_sql(
        f"CREATE TRIGGER prv_{name}_trg AFTER DELETE ON public.{target} "
        "FOR EACH ROW EXECUTE FUNCTION public.{fn}()".format(fn=trigger_fn)
    )


def _install_update_trigger(name: str, target: str, message: str) -> None:
    trigger_fn = f"prv_{name}_fn"
    _run_sql(
        f"CREATE OR REPLACE FUNCTION public.{trigger_fn}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"RAISE EXCEPTION '{message}'; END $$;"
    )
    _run_sql(
        f"CREATE TRIGGER prv_{name}_trg AFTER UPDATE ON public.{target} "
        "FOR EACH ROW EXECUTE FUNCTION public.{fn}()".format(fn=trigger_fn)
    )


def _drop_trigger(name: str, target: str) -> None:
    _run_sql(f"DROP TRIGGER IF EXISTS prv_{name}_trg ON public.{target}")
    _run_sql(f"DROP FUNCTION IF EXISTS public.prv_{name}_fn()")


# ---------------------------------------------------------------------------
# 1. delete_history semantics
# ---------------------------------------------------------------------------


def test_delete_history_removes_history_derivatives_preserves_rest(service_client: Client):
    user_id = _uid("dh")
    _seed_user(user_id, revision=2)
    try:
        result = _call_rpc(
            service_client, "delete_history", _rpc_params(user_id, new_operation_id())
        )
        assert "error" not in result
        assert result["status"] == "applied"
        assert result["operation"] == "delete_history"
        assert result["revision"] == 3
        assert result["counts"]["chat_logs"] == 2
        assert result["counts"]["turn_requests"] == 1
        assert result["counts"]["outbox_events"] == 1
        assert result["counts"]["archival_extractions"] == 1
        assert result["counts"]["memories"] == 0

        assert _count("chat_logs", user_id) == 0
        assert _count("turn_requests", user_id) == 0
        assert _count("outbox_events", user_id) == 0
        assert _count("archival_extractions", user_id) == 0
        # preserved
        assert _count("memories", user_id) == 1
        assert _count("profiles", user_id) == 1
        rows = _run_sql(
            f"SELECT revision, persona_config, emotional_state, relationship_state "
            f"FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 3
        assert rows[0]["persona_config"] == "persona-config"
        assert rows[0]["emotional_state"]["coping_mode"] == "MANIC"
        assert rows[0]["relationship_state"]["trust"] == 0.9
    finally:
        _cleanup_user(user_id)


def test_delete_history_preserves_admission_reservations(service_client: Client):
    user_id = _uid("dh_adm")
    _seed_user(user_id, revision=2, with_admission=True)
    try:
        assert _count("admission_reservations", user_id) == 1
        result = _call_rpc(
            service_client, "delete_history", _rpc_params(user_id, new_operation_id())
        )
        assert "error" not in result
        assert _count("admission_reservations", user_id) == 1
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# 2. delete_memories semantics
# ---------------------------------------------------------------------------


def test_delete_memories_removes_memories_preserves_chat_and_snapshots(service_client: Client):
    user_id = _uid("dm")
    _seed_user(user_id, revision=4)
    try:
        result = _call_rpc(
            service_client, "delete_memories", _rpc_params(user_id, new_operation_id())
        )
        assert "error" not in result
        assert result["operation"] == "delete_memories"
        assert result["counts"]["memories"] == 1
        assert result["counts"]["archival_extractions"] == 1
        assert result["revision"] == 5

        assert _count("memories", user_id) == 0
        assert _count("archival_extractions", user_id) == 0
        # preserved
        assert _count("chat_logs", user_id) == 2
        rows = _run_sql(
            f"SELECT revision, emotional_state, relationship_state "
            f"FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 5
        assert rows[0]["emotional_state"]["coping_mode"] == "MANIC"
        assert rows[0]["relationship_state"]["trust"] == 0.9
    finally:
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# 3/4. Reset semantics
# ---------------------------------------------------------------------------


def test_reset_emotional_state_produces_valid_neutral_v1(service_client: Client):
    user_id = _uid("re")
    _seed_user(user_id, revision=6)
    neutral = neutral_emotional_snapshot(1700000000.0)
    try:
        result = _call_rpc(
            service_client,
            "reset_emotional_state",
            _rpc_params(user_id, new_operation_id(), neutral),
        )
        assert "error" not in result
        assert result["operation"] == "reset_emotional_state"
        assert result["revision"] == 7
        assert result["counts"]["profiles"] == 1

        rows = _run_sql(
            f"SELECT revision, emotional_state, relationship_state FROM public.profiles "
            f"WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 7
        assert rows[0]["emotional_state"] == neutral
        assert rows[0]["emotional_state"]["schema_version"] == 1
        assert rows[0]["emotional_state"]["coping_mode"] == "HEALTHY"
        assert rows[0]["relationship_state"]["trust"] == 0.9
        # history and memories untouched
        assert _count("chat_logs", user_id) == 2
        assert _count("memories", user_id) == 1
    finally:
        _cleanup_user(user_id)


def test_reset_relationship_state_produces_valid_neutral_v1(service_client: Client):
    user_id = _uid("rr")
    _seed_user(user_id, revision=8)
    neutral = neutral_relationship_snapshot(1700000000.0)
    try:
        result = _call_rpc(
            service_client,
            "reset_relationship_state",
            _rpc_params(user_id, new_operation_id(), neutral),
        )
        assert "error" not in result
        assert result["operation"] == "reset_relationship_state"
        assert result["revision"] == 9

        rows = _run_sql(
            f"SELECT revision, emotional_state, relationship_state FROM public.profiles "
            f"WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 9
        assert rows[0]["relationship_state"] == neutral
        assert rows[0]["relationship_state"]["schema_version"] == 1
        assert rows[0]["emotional_state"]["coping_mode"] == "MANIC"
        assert _count("chat_logs", user_id) == 2
        assert _count("memories", user_id) == 1
    finally:
        _cleanup_user(user_id)


def test_reset_rejects_malformed_snapshot_atomically(service_client: Client):
    user_id = _uid("re_bad")
    _seed_user(user_id, revision=3)
    bad = neutral_emotional_snapshot(1700000000.0)
    bad["coping_mode"] = "BOGUS"
    try:
        result = _call_rpc(
            service_client,
            "reset_emotional_state",
            _rpc_params(user_id, new_operation_id(), bad),
        )
        assert result["error"]["code"] == "validation_failed"
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 3
        assert _count("privacy_operations", user_id) == 0
    finally:
        _cleanup_user(user_id)


def test_reset_rejects_valid_but_non_neutral_emotional_snapshot(service_client: Client):
    """A structurally valid v1 emotional snapshot with non-neutral values must
    be rejected by reset_emotional_state without touching snapshot, revision
    or the ledger (issue #314 review)."""
    user_id = _uid("re_non_neutral")
    _seed_user(user_id, revision=3)
    non_neutral = neutral_emotional_snapshot(1700000000.0)
    non_neutral["pleasure"] = 0.9
    non_neutral["arousal"] = 0.8
    non_neutral["dominance"] = 0.7
    non_neutral["libido"] = 0.1
    non_neutral["aggression"] = 0.1
    non_neutral["tension"] = 0.1
    non_neutral["coping_mode"] = "MANIC"
    try:
        result = _call_rpc(
            service_client,
            "reset_emotional_state",
            _rpc_params(user_id, new_operation_id(), non_neutral),
        )
        assert result["error"]["code"] == "validation_failed"
        rows = _run_sql(
            f"SELECT revision, emotional_state FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 3
        assert rows[0]["emotional_state"]["coping_mode"] == "MANIC"
        assert _count("privacy_operations", user_id) == 0
    finally:
        _cleanup_user(user_id)


def test_reset_rejects_valid_but_non_neutral_relationship_snapshot(service_client: Client):
    user_id = _uid("rr_non_neutral")
    _seed_user(user_id, revision=4)
    non_neutral = neutral_relationship_snapshot(1700000000.0)
    non_neutral["trust"] = 0.9
    non_neutral["affection"] = 0.8
    non_neutral["tension"] = 0.1
    try:
        result = _call_rpc(
            service_client,
            "reset_relationship_state",
            _rpc_params(user_id, new_operation_id(), non_neutral),
        )
        assert result["error"]["code"] == "validation_failed"
        rows = _run_sql(
            f"SELECT revision, relationship_state FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 4
        assert rows[0]["relationship_state"]["trust"] == 0.9
        assert _count("privacy_operations", user_id) == 0
    finally:
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# 5/6. Revision exactly once + durable idempotent replay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rpc_name", "payload_factory", "revision_before", "revision_after"),
    [
        ("delete_history", lambda: {}, 2, 3),
        ("delete_memories", lambda: {}, 4, 5),
        ("reset_emotional_state", lambda: neutral_emotional_snapshot(1700000000.0), 6, 7),
        ("reset_relationship_state", lambda: neutral_relationship_snapshot(1700000000.0), 8, 9),
    ],
)
def test_revision_increments_exactly_once_and_replay_is_durable(
    service_client: Client,
    rpc_name: str,
    payload_factory,
    revision_before: int,
    revision_after: int,
):
    user_id = _uid(f"rev_{rpc_name}")
    _seed_user(user_id, revision=revision_before)
    operation_id = new_operation_id()
    payload = payload_factory()
    try:
        first = _call_rpc(
            service_client, rpc_name, _rpc_params(user_id, operation_id, payload)
        )
        assert "error" not in first
        assert first["revision"] == revision_after
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == revision_after

        # Exact retry: identical stored result, no mutation, no revision bump
        replay = _call_rpc(
            service_client, rpc_name, _rpc_params(user_id, operation_id, payload)
        )
        assert replay == first
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == revision_after
        # Durable ledger: exactly one row, replay survives "restart" (new RPC call)
        ledger = _run_sql(
            f"SELECT operation, operation_payload_sha256, result "
            f"FROM public.privacy_operations "
            f"WHERE user_id = '{user_id}' AND operation_id = '{operation_id}'"
        )
        assert len(ledger) == 1
        assert ledger[0]["operation"] == rpc_name
        assert ledger[0]["result"] == first
    finally:
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# 7. Divergent operation/payload on the same operation_id -> conflict
# ---------------------------------------------------------------------------


def test_same_operation_id_divergent_payload_conflicts(service_client: Client):
    user_id = _uid("conf_payload")
    _seed_user(user_id, revision=2)
    operation_id = new_operation_id()
    try:
        first = _call_rpc(
            service_client, "delete_history", _rpc_params(user_id, operation_id, {})
        )
        assert "error" not in first

        conflict = _call_rpc(
            service_client,
            "delete_history",
            _rpc_params(user_id, operation_id, {"reason": "different"}),
        )
        assert conflict["error"]["code"] == "operation_conflict"
        assert conflict["error"]["message"] == (
            "operation_id already used with a different operation or payload"
        )
        # nothing changed
        assert _count("chat_logs", user_id) == 0
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 3
    finally:
        _cleanup_user(user_id)


def test_same_operation_id_divergent_operation_conflicts(service_client: Client):
    user_id = _uid("conf_op")
    _seed_user(user_id, revision=2)
    operation_id = new_operation_id()
    try:
        first = _call_rpc(
            service_client, "delete_history", _rpc_params(user_id, operation_id, {})
        )
        assert "error" not in first

        conflict = _call_rpc(
            service_client, "delete_memories", _rpc_params(user_id, operation_id, {})
        )
        assert conflict["error"]["code"] == "operation_conflict"
        assert _count("memories", user_id) == 1
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 3
    finally:
        _cleanup_user(user_id)


def test_same_operation_id_divergent_reset_snapshot_conflicts(service_client: Client):
    user_id = _uid("conf_reset")
    _seed_user(user_id, revision=4)
    operation_id = new_operation_id()
    neutral_a = neutral_emotional_snapshot(1700000000.0)
    neutral_b = neutral_emotional_snapshot(1700000000.5)
    try:
        first = _call_rpc(
            service_client,
            "reset_emotional_state",
            _rpc_params(user_id, operation_id, neutral_a),
        )
        assert "error" not in first

        conflict = _call_rpc(
            service_client,
            "reset_emotional_state",
            _rpc_params(user_id, operation_id, neutral_b),
        )
        assert conflict["error"]["code"] == "operation_conflict"
    finally:
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# 8. Injected mid-operation failure -> total rollback
# ---------------------------------------------------------------------------


def test_delete_history_failure_rolls_back_everything(service_client: Client):
    user_id = _uid("rb_dh")
    _seed_user(user_id, revision=2)
    try:
        # Fail on the chat_logs DELETE: by then outbox/turn_requests/archival
        # deletes already ran inside the transaction; everything must roll back.
        _install_delete_trigger("rb_dh", "chat_logs", "injected fail during history delete")
        with pytest.raises(APIError) as exc:
            _call_rpc(service_client, "delete_history", _rpc_params(user_id, new_operation_id()))
        assert exc.value.code == "P0001"

        assert _count("chat_logs", user_id) == 2
        assert _count("turn_requests", user_id) == 1
        assert _count("outbox_events", user_id) == 1
        assert _count("archival_extractions", user_id) == 1
        assert _count("memories", user_id) == 1
        assert _count("privacy_operations", user_id) == 0
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 2
    finally:
        _drop_trigger("rb_dh", "chat_logs")
        _cleanup_user(user_id)


def test_delete_memories_failure_rolls_back_everything(service_client: Client):
    user_id = _uid("rb_dm")
    _seed_user(user_id, revision=2)
    try:
        _install_delete_trigger("rb_dm", "memories", "injected fail during memory delete")
        with pytest.raises(APIError) as exc:
            _call_rpc(service_client, "delete_memories", _rpc_params(user_id, new_operation_id()))
        assert exc.value.code == "P0001"

        assert _count("memories", user_id) == 1
        assert _count("archival_extractions", user_id) == 1
        assert _count("chat_logs", user_id) == 2
        assert _count("privacy_operations", user_id) == 0
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 2
    finally:
        _drop_trigger("rb_dm", "memories")
        _cleanup_user(user_id)


def test_reset_failure_rolls_back_snapshot_and_revision(service_client: Client):
    user_id = _uid("rb_re")
    _seed_user(user_id, revision=3)
    neutral = neutral_emotional_snapshot(1700000000.0)
    try:
        # Fail on the profiles UPDATE: the snapshot replacement and the
        # revision bump must both roll back.
        _install_update_trigger("rb_re", "profiles", "injected fail during reset")
        with pytest.raises(APIError) as exc:
            _call_rpc(
                service_client,
                "reset_emotional_state",
                _rpc_params(user_id, new_operation_id(), neutral),
            )
        assert exc.value.code == "P0001"

        rows = _run_sql(
            f"SELECT revision, emotional_state FROM public.profiles "
            f"WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 3
        assert rows[0]["emotional_state"]["coping_mode"] == "MANIC"
        assert _count("privacy_operations", user_id) == 0
    finally:
        _drop_trigger("rb_re", "profiles")
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# 9. Concurrent operations of the same user never interleave partial state
# ---------------------------------------------------------------------------


def test_concurrent_same_user_operations_are_serialized(supabase_url, service_role_key):
    user_id = _uid("conc_same")
    _seed_user(user_id, revision=2)
    neutral = neutral_emotional_snapshot(1700000000.0)
    barrier = threading.Barrier(2)
    calls = [
        _ConcurrentOp(user_id, "delete_history", new_operation_id(), {}),
        _ConcurrentOp(user_id, "reset_emotional_state", new_operation_id(), neutral),
    ]
    try:
        results = _run_concurrent(url=supabase_url, key=service_role_key, barrier=barrier, calls=calls)
        assert all("error" not in r for r in results)
        # Serialized: both applied, revision bumped exactly twice, and the
        # final state is fully consistent (no partial interleaving).
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 4
        assert _count("chat_logs", user_id) == 0
        assert _count("memories", user_id) == 1
        assert _count("admission_reservations", user_id) == 1
        state = _run_sql(
            f"SELECT emotional_state->>'coping_mode' AS coping FROM public.profiles "
            f"WHERE user_id = '{user_id}'"
        )
        assert state[0]["coping"] == "HEALTHY"
        assert _count("privacy_operations", user_id) == 2
    finally:
        _cleanup_user(user_id)


def test_concurrent_same_operation_id_only_one_applies(supabase_url, service_role_key):
    """Two simultaneous identical operations: one applies, the other replays."""
    user_id = _uid("conc_same_id")
    _seed_user(user_id, revision=2)
    operation_id = new_operation_id()
    barrier = threading.Barrier(2)
    calls = [
        _ConcurrentOp(user_id, "delete_history", operation_id, {}),
        _ConcurrentOp(user_id, "delete_history", operation_id, {}),
    ]
    try:
        results = _run_concurrent(url=supabase_url, key=service_role_key, barrier=barrier, calls=calls)
        assert all("error" not in r for r in results)
        assert results[0] == results[1]
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 3
        assert _count("privacy_operations", user_id) == 1
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# 10. Users A/B remain fully isolated (no global lock)
# ---------------------------------------------------------------------------


def test_different_users_progress_in_parallel(supabase_url, service_role_key):
    user_a = _uid("iso_a")
    user_b = _uid("iso_b")
    _seed_user(user_a, revision=2)
    _seed_user(user_b, revision=4)
    barrier = threading.Barrier(2)
    calls = [
        _ConcurrentOp(user_a, "delete_history", new_operation_id(), {}),
        _ConcurrentOp(user_b, "delete_memories", new_operation_id(), {}),
    ]
    try:
        results = _run_concurrent(url=supabase_url, key=service_role_key, barrier=barrier, calls=calls)
        assert all("error" not in r for r in results)
        assert results[0]["revision"] == 3
        assert results[1]["revision"] == 5

        # A: history gone, memories preserved
        assert _count("chat_logs", user_a) == 0
        assert _count("memories", user_a) == 1
        # B: memories gone, history preserved
        assert _count("memories", user_b) == 0
        assert _count("chat_logs", user_b) == 2
    finally:
        _cleanup_user(user_a)
        _cleanup_user(user_b)


# ---------------------------------------------------------------------------
# 12/13. Authorization: only service_role executes; grants are minimal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rpc_name",
    ["delete_history", "delete_memories", "reset_emotional_state", "reset_relationship_state"],
)
def test_anon_cannot_execute_any_privacy_rpc(anon_client: Client, rpc_name: str):
    params = _rpc_params("anon-target", new_operation_id())
    with pytest.raises(APIError) as exc:
        anon_client.rpc(rpc_name, params).execute()
    assert getattr(exc.value, "code", None) == "42501"


@pytest.mark.parametrize(
    "rpc_name",
    ["delete_history", "delete_memories", "reset_emotional_state", "reset_relationship_state"],
)
def test_authenticated_cannot_execute_any_privacy_rpc(
    auth_client: tuple[Client, str], rpc_name: str
):
    client, user_id = auth_client
    params = _rpc_params(user_id, new_operation_id())
    with pytest.raises(APIError) as exc:
        client.rpc(rpc_name, params).execute()
    assert getattr(exc.value, "code", None) == "42501"


def test_service_role_can_execute_all_privacy_rpcs(service_client: Client):
    user_id = _uid("svc")
    _seed_user(user_id, revision=2)
    try:
        results = [
            _call_rpc(
                service_client,
                rpc_name,
                _rpc_params(user_id, new_operation_id(), payload),
            )
            for rpc_name, payload in (
                ("delete_history", {}),
                ("delete_memories", {}),
                ("reset_emotional_state", neutral_emotional_snapshot(1700000000.0)),
                ("reset_relationship_state", neutral_relationship_snapshot(1700000000.0)),
            )
        ]
        assert all("error" not in r for r in results)
    finally:
        _cleanup_user(user_id)


def test_grants_are_minimal():
    # service_role has EXECUTE ONLY on the four public RPCs.
    for rpc_name in (
        "delete_history", "delete_memories",
        "reset_emotional_state", "reset_relationship_state",
    ):
        assert _run_sql(
            f"SELECT has_function_privilege('service_role', "
            f"'public.{rpc_name}(text, uuid, jsonb)', 'EXECUTE') AS result"
        )[0]["result"] is True
    # ... and NO table privileges on the ledger (RPCs are the only path).
    assert _run_sql(
        "SELECT has_table_privilege('service_role', 'public.privacy_operations', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS result"
    )[0]["result"] is False
    # anon / authenticated / PUBLIC: no EXECUTE on the RPCs, no table access.
    for role in ("anon", "authenticated", "public"):
        for rpc_name in (
            "delete_history", "delete_memories",
            "reset_emotional_state", "reset_relationship_state",
        ):
            assert _run_sql(
                f"SELECT has_function_privilege('{role}', "
                f"'public.{rpc_name}(text, uuid, jsonb)', 'EXECUTE') AS result"
            )[0]["result"] is False


# ---------------------------------------------------------------------------
# 14. Sanitized errors and results
# ---------------------------------------------------------------------------


def test_failure_errors_are_sanitized(service_client: Client):
    user_id = _uid("sanitize")
    _seed_user(user_id, revision=2)
    try:
        _install_delete_trigger(
            "sanitize", "chat_logs", "SECRET_CONSTRAINT_LEAK user_content=LEAK HMAC=LEAK"
        )
        with pytest.raises(APIError) as exc:
            _call_rpc(service_client, "delete_history", _rpc_params(user_id, new_operation_id()))
        message = str(exc.value)
        assert "SECRET_CONSTRAINT_LEAK" not in message
        assert "user_content=LEAK" not in message
        assert "HMAC=LEAK" not in message
        assert "P0001" in message or "persistence error" in message
        assert "injected" not in message
    finally:
        _drop_trigger("sanitize", "chat_logs")
        _cleanup_user(user_id)


def test_results_never_contain_sensitive_content(service_client: Client):
    user_id = _uid("no_leak")
    _seed_user(user_id, revision=2)
    try:
        for rpc_name, payload in (
            ("delete_history", {}),
            ("delete_memories", {}),
            ("reset_emotional_state", neutral_emotional_snapshot(1700000000.0)),
            ("reset_relationship_state", neutral_relationship_snapshot(1700000000.0)),
        ):
            result = _call_rpc(
                service_client, rpc_name, _rpc_params(user_id, new_operation_id(), payload)
            )
            assert "error" not in result
            serialized = json.dumps(result)
            for marker in ("hello", "hi there", "a durable memory", "persona-config"):
                assert marker not in serialized
    finally:
        _cleanup_user(user_id)


def test_validation_errors_do_not_echo_payload(service_client: Client):
    user_id = _uid("no_echo")
    _seed_user(user_id, revision=2)
    bad = neutral_emotional_snapshot(1700000000.0)
    bad["user_id"] = "attacker-hidden-user"
    try:
        result = _call_rpc(
            service_client,
            "reset_emotional_state",
            _rpc_params(user_id, new_operation_id(), bad),
        )
        assert result["error"]["code"] == "validation_failed"
        serialized = json.dumps(result)
        assert "attacker-hidden-user" not in serialized
        assert _count("profiles", "attacker-hidden-user") == 0
    finally:
        _cleanup_user(user_id)


def test_sql_validation_rejects_invalid_user_ids_without_ledger_rows(service_client: Client):
    """Whitespace-only and oversized identities fail with a predictable
    validation envelope through the SQL boundary and never create ledger rows
    (issue #314 review)."""
    user_id = _uid("uid_bounds")
    _seed_user(user_id, revision=2)
    try:
        ws_result = _call_rpc(
            service_client, "delete_history", _rpc_params("   ", new_operation_id())
        )
        assert ws_result["error"]["code"] == "validation_failed"

        long_result = _call_rpc(
            service_client, "delete_history", _rpc_params("x" * 129, new_operation_id())
        )
        assert long_result["error"]["code"] == "validation_failed"

        # No ledger rows for the invalid identities.
        assert _run_sql(
            "SELECT count(*)::integer AS count FROM public.privacy_operations "
            "WHERE user_id = '   ' OR user_id = '" + "x" * 129 + "'"
        )[0]["count"] == 0

        # Boundary: exactly 128 characters is accepted (no-op for missing user).
        ok_user = "y" * 128
        ok_result = _call_rpc(
            service_client, "delete_history", _rpc_params(ok_user, new_operation_id())
        )
        assert "error" not in ok_result
        assert ok_result["status"] == "applied"
        assert ok_result["user_id"] == ok_user
    finally:
        _cleanup_user(user_id)


def test_backend_adapter_rejects_divergent_result_user(service_client: Client):
    """The adapter fails closed when the RPC result belongs to another user
    and never exposes the divergent identity (issue #314 review)."""
    user_id = _uid("adapter_div")
    _seed_user(user_id, revision=2)
    divergent = "SECRET-DIVERGENT-USER-MARKER"

    async def rpc_client(name: str, params: dict) -> dict:
        result = _call_rpc(service_client, name, params)
        result["user_id"] = divergent
        return result

    try:
        with pytest.raises(ValidationError) as exc:
            asyncio.run(
                run_privacy_operation(
                    rpc_client=rpc_client,
                    operation=OPERATION_DELETE_HISTORY,
                    authenticated_user_id=user_id,
                    operation_id=new_operation_id(),
                    payload={},
                )
            )
        assert exc.value.code == "invalid_rpc_result"
        assert divergent not in str(exc.value)
    finally:
        _cleanup_user(user_id)


# ---------------------------------------------------------------------------
# Backend adapter against the real database
# ---------------------------------------------------------------------------


def test_backend_adapter_real_client_full_flow(service_client: Client):
    user_id = _uid("adapter")
    _seed_user(user_id, revision=2)

    async def rpc_client(name: str, params: dict) -> dict:
        return _call_rpc(service_client, name, params)

    try:
        result = asyncio.run(
            run_privacy_operation(
                rpc_client=rpc_client,
                operation=OPERATION_DELETE_HISTORY,
                authenticated_user_id=user_id,
                operation_id=new_operation_id(),
                payload={},
            )
        )
        assert result.to_db_row()["revision"] == 3
        assert result.counts["chat_logs"] == 2

        # Replay via the adapter: identical, no extra increment
        replay = asyncio.run(
            run_privacy_operation(
                rpc_client=rpc_client,
                operation=OPERATION_DELETE_HISTORY,
                authenticated_user_id=user_id,
                operation_id=result.operation_id,
                payload={},
            )
        )
        assert replay.to_db_row() == result.to_db_row()

        # Divergent payload via the adapter: sanitized ConflictError
        with pytest.raises(ConflictError) as exc:
            asyncio.run(
                run_privacy_operation(
                    rpc_client=rpc_client,
                    operation=OPERATION_DELETE_HISTORY,
                    authenticated_user_id=user_id,
                    operation_id=result.operation_id,
                    payload={"reason": "different"},
                )
            )
        assert exc.value.code == "operation_conflict"
    finally:
        _cleanup_user(user_id)


def test_backend_adapter_rpc_failure_is_sanitized_persistence(service_client: Client):
    user_id = _uid("adapter_fail")
    _seed_user(user_id, revision=2)

    async def rpc_client(name: str, params: dict) -> dict:
        return _call_rpc(service_client, name, params)

    try:
        _install_delete_trigger("adapter_fail", "chat_logs", "boom")
        with pytest.raises(PersistenceError):
            asyncio.run(
                run_privacy_operation(
                    rpc_client=rpc_client,
                    operation=OPERATION_DELETE_HISTORY,
                    authenticated_user_id=user_id,
                    operation_id=new_operation_id(),
                    payload={},
                )
            )
    finally:
        _drop_trigger("adapter_fail", "chat_logs")
        _cleanup_user(user_id)
