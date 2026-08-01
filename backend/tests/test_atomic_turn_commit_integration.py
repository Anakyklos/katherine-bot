"""Real Supabase integration tests for ``public.commit_turn`` (#271).

This file is executed ONLY by the database CI job against a freshly reset
local Supabase instance (no mocks: real PostgreSQL transactions, locks, FKs,
leases and rollbacks). It must never be collected by the ordinary backend
unit job.

Covers the 29 mandatory scenarios from issue #271:

 1.  happy path writes exactly two messages, snapshots, request and outbox
 2.  failure after the first message rolls back the whole transaction
 3.  failure after the snapshot update rolls back the whole transaction
 4.  exact retry returns the previous result without new writes
 5.  same request ID with a different payload raises a conflict
 6.  two concurrent transactions with the same revision: only one commits
 7.  different users progress in parallel (no global lock)
 8.  a missing profile is created exactly once under concurrency
 9.  duplicate outbox idempotency key is rejected (no partial writes)
10.  revision increments exactly once
11.  fake identity inside a snapshot never changes the persisted user
12.  FKs and cascades do not block valid deletions
13.  returned errors are sanitized (no SQLERRM / constraint names / payload)
14.  completed + same hash replays without any CAS
15.  pending + active lease of another worker is blocked
16.  pending + active lease of the same worker can continue
17.  pending + expired lease is reclaimed atomically
18.  expired + same hash can be reclaimed
19.  any status with a divergent hash raises a payload conflict
20.  two simultaneous reclaim attempts do not both win
21.  pruning messages does not break replay
22.  changing outbox status/attempts does not change replay
23.  outbox reference order is deterministic
24.  divergent p_public_response is rejected
25.  textual / boolean / incompatible schema_version is rejected
26.  nested user_id and bond_label are rejected
27.  anon and authenticated cannot execute the RPC
28.  service_role can execute the RPC
29.  an unexpected failure aborts the RPC with no partial writes

Failure injection uses disposable triggers created only in the local test
database (never public RPC failpoints).
"""

from __future__ import annotations

import asyncio
import json
import math
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

from backend.atomic_turn_commit import ConflictError, ValidationError, commit_turn
from backend.emotional_domain import EmotionalStateV1
from backend.relationship import RelationshipStateV1


_SUPABASE_CLI = ["supabase"] if shutil.which("supabase") else ["npx", "supabase"]


# ---------------------------------------------------------------------------
# Environment / clients
# ---------------------------------------------------------------------------


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for atomic turn commit integration tests"
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


@pytest.fixture(scope="module")
def service_client(supabase_url: str, service_role_key: str) -> Client:
    return create_client(supabase_url, service_role_key)


@pytest.fixture(scope="module")
def anon_client(supabase_url: str, anon_key: str) -> Client:
    return create_client(supabase_url, anon_key)


@pytest.fixture(scope="module")
def auth_client(supabase_url: str, anon_key: str, service_client: Client) -> tuple[Client, str]:
    """A real authenticated user (client, user_id)."""
    email = "atomic-turn-commit-auth@test.local"
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

    client.auth.sign_out()
    for user in service_client.auth.admin.list_users():
        if user.email == email:
            service_client.auth.admin.delete_user(user.id)


# ---------------------------------------------------------------------------
# SQL helpers (pinned local Supabase CLI, sanitized)
# ---------------------------------------------------------------------------


def _run_sql(sql: str) -> list[dict]:
    """Execute trusted test SQL through the pinned local Supabase CLI.

    Telemetry is disabled in the child environment because the CLI's PostHog
    analytics flush intermittently times out and converts a successful SQL
    command into a non-zero exit code, which would make these tests flaky.
    """
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
    assert result.returncode == 0, "sanitized atomic commit test SQL operation failed"
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
    return f"atc_{label}_{uuid.uuid4().hex[:12]}"


def _hash_a() -> str:
    return "a" * 64


def _hash_b() -> str:
    return "b" * 64


def _emotional_state() -> dict:
    return EmotionalStateV1.neutral(timestamp=1700000000.0).to_dict()


def _relationship_state() -> dict:
    return RelationshipStateV1.neutral(timestamp=1700000000.0).to_dict()


def _domain_state_payloads() -> tuple[dict, dict]:
    """Return non-neutral snapshots through the canonical domain serializers."""
    emotional = EmotionalStateV1.create(
        pleasure=0.25,
        arousal=-0.5,
        dominance=0.75,
        libido=0.1,
        aggression=0.2,
        connection=0.9,
        energy=0.8,
        tension=0.35,
        coping_mode="DEFENSIVE",
        timestamp=1700000000.25,
    ).to_dict()
    relationship = RelationshipStateV1.create(
        trust=0.8,
        affection=0.65,
        tension=0.3,
        triggers=["  abandonment  ", "conflict", "abandonment"],
        timestamp=1700000000.25,
    ).to_dict()
    return emotional, relationship


def _commit_params_with_states(user_id: str, request_id: str, *, payload_hash: str) -> dict:
    emotional, relationship = _domain_state_payloads()
    return _commit_params(
        user_id,
        request_id,
        payload_hash=payload_hash,
        emotional_state=emotional,
        relationship_state=relationship,
    )


def _replay_payload(response: str = "Hi there!") -> dict:
    return {"response": response, "message_id": str(uuid.uuid4()), "duration_ms": 42}


def _commit_params(
    user_id: str,
    request_id: str,
    *,
    payload_hash: str,
    expected_revision: int = 0,
    lease_owner: str | None = None,
    response: str = "Hi there!",
    user_message: str = "Hello",
    assistant_message: str = "Hi there!",
    emotional_state: dict | None = None,
    relationship_state: dict | None = None,
    replay_payload: dict | None = None,
    outbox_events: list[dict] | None = None,
) -> dict:
    return {
        "p_authenticated_user_id": user_id,
        "p_request_id": request_id,
        "p_expected_revision": expected_revision,
        "p_user_message": user_message,
        "p_assistant_message": assistant_message,
        "p_payload_hash_sha256": payload_hash,
        "p_emotional_state": (
            emotional_state if emotional_state is not None else _emotional_state()
        ),
        "p_relationship_state": (
            relationship_state if relationship_state is not None else _relationship_state()
        ),
        "p_public_response": response,
        "p_replay_payload": replay_payload if replay_payload is not None else _replay_payload(response),
        "p_outbox_events": outbox_events if outbox_events is not None else [],
        "p_lease_owner": lease_owner,
    }


def _call_commit(client: Client, params: dict) -> dict:
    """Invoke the commit_turn RPC and return the parsed result object."""
    response = client.rpc("commit_turn", params).execute()
    data = response.data
    if isinstance(data, list):
        assert len(data) == 1
        data = data[0]
    assert isinstance(data, dict), f"unexpected RPC response shape: {type(data).__name__}"
    return data


def _normalized(result: dict) -> dict:
    """Public normalized representation used to compare fresh vs replay."""
    assert "error" not in result, f"expected success result, got {result}"
    return {
        "user_id": result["user_id"],
        "request_id": result["request_id"],
        "committed_revision": result["committed_revision"],
        "user_message_id": result["user_message_id"],
        "assistant_message_id": result["assistant_message_id"],
        "replay_payload": result["replay_payload"],
        "outbox_events": result["outbox_events"],
        "created_at": result["created_at"],
        "completed_at": result["completed_at"],
    }


def _cleanup_user(client: Client, user_id: str) -> None:
    try:
        client.table("chat_logs").delete().eq("user_id", user_id).execute()
    except Exception:
        pass
    try:
        client.table("profiles").delete().eq("user_id", user_id).execute()
    except Exception:
        pass


def _create_profile(client: Client, user_id: str) -> None:
    """Create a profile with the baseline revision (0) directly."""
    client.table("profiles").upsert({"user_id": user_id}).execute()


def _insert_pending_request(user_id: str, request_id: str, payload_hash: str, lease_owner: str, lease_expires_at: str) -> None:
    _run_sql(
        "INSERT INTO public.turn_requests "
        "(user_id, request_id, payload_hash_sha256, status, expected_revision, "
        " lease_owner, lease_expires_at) VALUES "
        f"('{user_id}', '{request_id}', '{payload_hash}', 'pending', 0, "
        f"'{lease_owner}', '{lease_expires_at}')"
    )


def _insert_expired_request(user_id: str, request_id: str, payload_hash: str, error_code: str = "timeout") -> None:
    _run_sql(
        "INSERT INTO public.turn_requests "
        "(user_id, request_id, payload_hash_sha256, status, expected_revision, error_code) "
        f"VALUES ('{user_id}', '{request_id}', '{payload_hash}', 'expired', 0, '{error_code}')"
    )


def _insert_completed_request(user_id: str, request_id: str, payload_hash: str) -> None:
    replay = _replay_payload()
    _run_sql(
        "INSERT INTO public.turn_requests "
        "(user_id, request_id, payload_hash_sha256, status, expected_revision, "
        " committed_revision, replay_payload, completed_at) VALUES "
        f"('{user_id}', '{request_id}', '{payload_hash}', 'completed', 0, 1, "
        f"'{json.dumps(replay)}'::jsonb, now())"
    )


@dataclass(frozen=True)
class _ConcurrentCommit:
    user_id: str
    request_id: str
    payload_hash: str
    lease_owner: str | None = None
    expected_revision: int = 0


def _run_concurrent(
    *,
    url: str,
    key: str,
    barrier: threading.Barrier,
    calls: list[_ConcurrentCommit],
    timeout: int = 30,
) -> list[dict]:
    """Run commit_turn concurrently and return each call's parsed result.

    Unexpected persistence failures surface as APIError; callers decide how to
    classify the outcome.
    """

    def _one(call: _ConcurrentCommit) -> dict:
        client = create_client(url, key)
        barrier.wait(timeout=timeout)
        return _call_commit(
            client,
            _commit_params(
                call.user_id,
                call.request_id,
                payload_hash=call.payload_hash,
                lease_owner=call.lease_owner,
                expected_revision=call.expected_revision,
            ),
        )

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(_one, call) for call in calls]
        return [f.result(timeout=timeout) for f in futures]


# ---------------------------------------------------------------------------
# Failpoint triggers (created only in the local test DB)
# ---------------------------------------------------------------------------


def _install_trigger(name: str, target: str, condition: str | None, message: str) -> None:
    """Create a disposable BEFORE/AFTER trigger that raises *message*."""
    trigger_fn = f"atc_{name}_fn"
    _run_sql(
        f"CREATE OR REPLACE FUNCTION public.{trigger_fn}() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        f"RAISE EXCEPTION '{message}'; END $$;"
    )
    cond_clause = f" WHEN ({condition})" if condition else ""
    _run_sql(
        f"CREATE TRIGGER atc_{name}_trg AFTER INSERT ON public.{target} "
        f"FOR EACH ROW{cond_clause} EXECUTE FUNCTION public.{trigger_fn}()"
    )


def _drop_trigger(name: str, target: str) -> None:
    _run_sql(f"DROP TRIGGER IF EXISTS atc_{name}_trg ON public.{target}")
    _run_sql(f"DROP FUNCTION IF EXISTS public.atc_{name}_fn()")


def _install_reclaim_miss_trigger() -> None:
    """Make the final reclaim UPDATE miss after message writes begin."""
    _run_sql(
        "CREATE OR REPLACE FUNCTION public.atc_reclaim_miss_fn() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "UPDATE public.turn_requests SET lease_owner='other-worker', "
        "lease_expires_at=now() + INTERVAL '1 hour' "
        "WHERE user_id=NEW.user_id AND status IN ('pending','expired'); "
        "RETURN NEW; END $$;"
    )
    _run_sql(
        "CREATE TRIGGER atc_reclaim_miss_trg AFTER INSERT ON public.chat_logs "
        "FOR EACH ROW WHEN (NEW.role = 'assistant') "
        "EXECUTE FUNCTION public.atc_reclaim_miss_fn()"
    )


def _drop_reclaim_miss_trigger() -> None:
    _run_sql("DROP TRIGGER IF EXISTS atc_reclaim_miss_trg ON public.chat_logs")
    _run_sql("DROP FUNCTION IF EXISTS public.atc_reclaim_miss_fn()")


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_writes_messages_snapshots_request_outbox(service_client: Client):
    user_id = _uid("happy")
    request_id = str(uuid.uuid4())
    params = _commit_params(
        user_id,
        request_id,
        payload_hash=_hash_a(),
        lease_owner="worker-1",
        outbox_events=[
            {"event_type": "turn_completed", "payload": {"ref": "t1"}, "idempotency_key": "k1"},
        ],
    )
    try:
        result = _call_commit(service_client, params)
        assert "error" not in result
        assert result["user_id"] == user_id
        assert result["request_id"] == request_id
        assert result["committed_revision"] == 1
        assert result["user_message_id"] == request_id
        assert result["assistant_message_id"] == result["replay_payload"]["message_id"]
        assert result["replay_payload"]["response"] == "Hi there!"
        assert len(result["outbox_events"]) == 1
        ref = result["outbox_events"][0]
        assert set(ref) == {"id", "event_type", "idempotency_key", "turn_request_id", "contract_version"}
        assert ref["event_type"] == "turn_completed"
        assert ref["idempotency_key"] == "k1"
        assert ref["contract_version"] == 1

        # exactly two messages, one request, one outbox, one profile
        assert _count("chat_logs", user_id) == 2
        assert _count("turn_requests", user_id) == 1
        assert _count("outbox_events", user_id) == 1
        assert _count("profiles", user_id) == 1

        # snapshots persisted with revision 1
        rows = _run_sql(
            f"SELECT revision, emotional_state, relationship_state FROM public.profiles "
            f"WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 1
        assert rows[0]["emotional_state"]["schema_version"] == 1
        assert rows[0]["relationship_state"]["schema_version"] == 1
    finally:
        _cleanup_user(service_client, user_id)


def test_domain_serializers_round_trip_through_real_commit(service_client: Client):
    """The real RPC must persist the exact v1 domain serializer output."""
    user_id = _uid("domain_serializers")
    request_id = str(uuid.uuid4())
    params = _commit_params_with_states(user_id, request_id, payload_hash=_hash_a())
    try:
        result = _call_commit(service_client, params)
        assert "error" not in result
        rows = _run_sql(
            "SELECT emotional_state, relationship_state FROM public.profiles "
            f"WHERE user_id = '{user_id}'"
        )
        assert rows == [{
            "emotional_state": params["p_emotional_state"],
            "relationship_state": params["p_relationship_state"],
        }]
        assert params["p_emotional_state"]["coping_mode"] == "DEFENSIVE"
        assert params["p_emotional_state"]["timestamp"] == 1700000000.25
        assert params["p_relationship_state"]["timestamp"] == 1700000000.25
        assert params["p_relationship_state"]["triggers"] == ["abandonment", "conflict"]
        assert "bond_label" not in params["p_relationship_state"]
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 2. Rollback after first message
# ---------------------------------------------------------------------------


def test_failure_after_first_message_rolls_back(service_client: Client):
    user_id = _uid("rb1")
    request_id = str(uuid.uuid4())
    params = _commit_params(user_id, request_id, payload_hash=_hash_a())
    try:
        # Fail on the second (assistant) message insert.
        _install_trigger("rb1", "chat_logs", "NEW.role = 'assistant'", "injected fail after first message")
        with pytest.raises(APIError) as exc:
            _call_commit(service_client, params)
        assert exc.value.code == "P0001"
        # Everything rolled back: no messages, no profile, no request, no outbox.
        assert _count("chat_logs", user_id) == 0
        assert _count("profiles", user_id) == 0
        assert _count("turn_requests", user_id) == 0
        assert _count("outbox_events", user_id) == 0
    finally:
        _drop_trigger("rb1", "chat_logs")
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 3. Rollback after snapshot update
# ---------------------------------------------------------------------------


def test_failure_after_snapshot_update_rolls_back(service_client: Client):
    user_id = _uid("rb2")
    request_id = str(uuid.uuid4())
    params = _commit_params(user_id, request_id, payload_hash=_hash_a())
    try:
        _install_trigger("rb2", "profiles", None, "injected fail after snapshot update")
        with pytest.raises(APIError) as exc:
            _call_commit(service_client, params)
        assert exc.value.code == "P0001"
        assert _count("chat_logs", user_id) == 0
        assert _count("profiles", user_id) == 0
        assert _count("turn_requests", user_id) == 0
        assert _count("outbox_events", user_id) == 0
    finally:
        _drop_trigger("rb2", "profiles")
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 4. Exact retry returns previous result without new writes
# ---------------------------------------------------------------------------


def test_exact_retry_replays_without_new_writes(service_client: Client):
    user_id = _uid("retry")
    request_id = str(uuid.uuid4())
    params = _commit_params(user_id, request_id, payload_hash=_hash_a())
    try:
        first = _call_commit(service_client, params)
        assert "error" not in first
        assert _count("chat_logs", user_id) == 2

        second = _call_commit(service_client, params)
        assert _normalized(second) == _normalized(first)
        # no new writes
        assert _count("chat_logs", user_id) == 2
        assert _count("turn_requests", user_id) == 1
        assert _count("outbox_events", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 5. Same request ID, different payload -> conflict
# ---------------------------------------------------------------------------


def test_same_request_different_payload_conflict(service_client: Client):
    user_id = _uid("conf")
    request_id = str(uuid.uuid4())
    try:
        first = _call_commit(service_client, _commit_params(user_id, request_id, payload_hash=_hash_a()))
        assert "error" not in first

        conflict = _call_commit(service_client, _commit_params(user_id, request_id, payload_hash=_hash_b()))
        assert conflict["error"]["code"] == "request_payload_conflict"
        # first commit untouched
        assert _count("chat_logs", user_id) == 2
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 6. Two concurrent transactions, same revision: only one commits
# ---------------------------------------------------------------------------


def test_two_transactions_same_revision_only_one_commits(supabase_url, service_role_key):
    user_id = _uid("race1")
    barrier = threading.Barrier(2)
    calls = [
        _ConcurrentCommit(user_id, str(uuid.uuid4()), _hash_a(), expected_revision=0),
        _ConcurrentCommit(user_id, str(uuid.uuid4()), _hash_b(), expected_revision=0),
    ]
    try:
        results = _run_concurrent(url=supabase_url, key=service_role_key, barrier=barrier, calls=calls)
        codes = [r["error"]["code"] for r in results if "error" in r]
        successes = [r for r in results if "error" not in r]
        assert len(successes) == 1
        assert codes == ["revision_mismatch"]
        # exactly one committed request and revision == 1
        assert _count("turn_requests", user_id) == 1
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 1
    finally:
        _cleanup_user(create_client(supabase_url, service_role_key), user_id)


# ---------------------------------------------------------------------------
# 7. Different users progress in parallel (no global lock)
# ---------------------------------------------------------------------------


def test_different_users_progress_in_parallel(supabase_url, service_role_key):
    barrier = threading.Barrier(2)
    calls = [
        _ConcurrentCommit(_uid("par_a"), str(uuid.uuid4()), _hash_a(), expected_revision=0),
        _ConcurrentCommit(_uid("par_b"), str(uuid.uuid4()), _hash_b(), expected_revision=0),
    ]
    client = create_client(supabase_url, service_role_key)
    try:
        results = _run_concurrent(url=supabase_url, key=service_role_key, barrier=barrier, calls=calls)
        assert all("error" not in r for r in results)
        assert [r["committed_revision"] for r in results] == [1, 1]
    finally:
        for call in calls:
            _cleanup_user(client, call.user_id)


# ---------------------------------------------------------------------------
# 8. Missing profile created exactly once under concurrency
# ---------------------------------------------------------------------------


def test_missing_profile_created_once_under_concurrency(supabase_url, service_role_key):
    user_id = _uid("profile_race")
    workers = 5
    barrier = threading.Barrier(workers)
    calls = [
        _ConcurrentCommit(user_id, str(uuid.uuid4()), _hash_a(), expected_revision=0)
        for _ in range(workers)
    ]
    client = create_client(supabase_url, service_role_key)
    try:
        results = _run_concurrent(url=supabase_url, key=service_role_key, barrier=barrier, calls=calls, timeout=40)
        successes = [r for r in results if "error" not in r]
        mismatches = [r["error"]["code"] for r in results if "error" in r]
        assert len(successes) == 1
        assert mismatches.count("revision_mismatch") == workers - 1
        # exactly one profile row, revision == 1
        assert _count("profiles", user_id) == 1
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 1
    finally:
        _cleanup_user(client, user_id)


# ---------------------------------------------------------------------------
# 9. Outbox duplicate idempotency key rejected (no partial writes)
# ---------------------------------------------------------------------------


def test_duplicate_outbox_idempotency_rejected(service_client: Client):
    user_id = _uid("outbox_dup")
    try:
        first = _call_commit(
            service_client,
            _commit_params(
                user_id,
                str(uuid.uuid4()),
                payload_hash=_hash_a(),
                outbox_events=[
                    {"event_type": "memory_indexed", "payload": {"ref": "m1"}, "idempotency_key": "dup-key"},
                ],
            ),
        )
        assert "error" not in first
        assert _count("outbox_events", user_id) == 1

        # Second commit (different request) with the SAME idempotency key.
        # expected_revision=1 so the revision CAS passes and the call reaches
        # the outbox idempotency unique constraint.
        with pytest.raises(APIError) as exc:
            _call_commit(
                service_client,
                _commit_params(
                    user_id,
                    str(uuid.uuid4()),
                    payload_hash=_hash_b(),
                    expected_revision=1,
                    outbox_events=[
                        {"event_type": "memory_indexed", "payload": {"ref": "m2"}, "idempotency_key": "dup-key"},
                    ],
                ),
            )
        # 23505 unique violation is converted to the sanitized persistence error.
        assert exc.value.code == "P0001"
        # The failed transaction left no partial state.
        assert _count("outbox_events", user_id) == 1
        assert _count("chat_logs", user_id) == 2
        assert _count("turn_requests", user_id) == 1
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 10. Revision increments exactly once
# ---------------------------------------------------------------------------


def test_revision_increments_exactly_once(service_client: Client):
    user_id = _uid("rev")
    try:
        r1 = _call_commit(service_client, _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_a()))
        assert r1["committed_revision"] == 1
        # replay does not re-increment
        r1b = _call_commit(service_client, _commit_params(user_id, r1["request_id"], payload_hash=_hash_a()))
        assert r1b["committed_revision"] == 1
        assert _count("turn_requests", user_id) == 1
        # second distinct request with expected_revision=1
        r2 = _call_commit(service_client, _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_b(), expected_revision=1))
        assert r2["committed_revision"] == 2
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 2
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 11. Fake identity inside a snapshot never changes the persisted user
# ---------------------------------------------------------------------------


def test_fake_identity_in_snapshot_rejected(service_client: Client):
    user_id = _uid("identity")
    request_id = str(uuid.uuid4())
    params = _commit_params(
        user_id,
        request_id,
        payload_hash=_hash_a(),
        emotional_state={
            "schema_version": 1,
            "pleasure": 0.1,
            "arousal": 0.2,
            "dominance": 0.3,
            "nested": {"user_id": "attacker-user-id"},
        },
    )
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
        # nothing persisted for the attacker identity
        assert _count("profiles", "attacker-user-id") == 0
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 12. FKs and cascades do not block valid deletion
# ---------------------------------------------------------------------------


def test_fks_and_cascades_do_not_block_valid_deletion(service_client: Client):
    user_id = _uid("cascade")
    try:
        result = _call_commit(service_client, _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_a()))
        assert "error" not in result
        # delete chat_logs rows (pruning): allowed, refs are nulled
        service_client.table("chat_logs").delete().eq("user_id", user_id).execute()
        assert _count("chat_logs", user_id) == 0
        # delete profile: cascades turn_requests + outbox
        service_client.table("profiles").delete().eq("user_id", user_id).execute()
        assert _count("turn_requests", user_id) == 0
        assert _count("outbox_events", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 13. Errors returned are sanitized
# ---------------------------------------------------------------------------


def test_unexpected_errors_are_sanitized(service_client: Client):
    user_id = _uid("sanitize")
    params = _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_a())
    try:
        _install_trigger("sanitize", "chat_logs", "NEW.role = 'assistant'", "SECRET_CONSTRAINT_LEAK user_message=LEAK")
        with pytest.raises(APIError) as exc:
            _call_commit(service_client, params)
        message = str(exc.value)
        assert "SECRET_CONSTRAINT_LEAK" not in message
        assert "user_message=LEAK" not in message
        assert "P0001" in message or "persistence error" in message
        assert "injected" not in message
    finally:
        _drop_trigger("sanitize", "chat_logs")
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 14. Completed + same hash replays without any CAS
# ---------------------------------------------------------------------------


def test_completed_same_hash_replays_without_cas(service_client: Client):
    user_id = _uid("replay_cas")
    request_id = str(uuid.uuid4())
    try:
        first = _call_commit(service_client, _commit_params(user_id, request_id, payload_hash=_hash_a()))
        assert "error" not in first
        # same hash but a bogus expected_revision: replay path must not do CAS
        replay = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), expected_revision=999),
        )
        assert _normalized(replay) == _normalized(first)
        assert _count("chat_logs", user_id) == 2
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 15. Pending + active lease of another worker is blocked
# ---------------------------------------------------------------------------


def test_pending_active_lease_other_worker_blocked(service_client: Client):
    user_id = _uid("lease_other")
    request_id = str(uuid.uuid4())
    try:
        _create_profile(service_client, user_id)
        _insert_pending_request(
            user_id, request_id, _hash_a(), "worker-a", "2099-01-01T00:00:00Z"
        )
        result = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), lease_owner="worker-b"),
        )
        assert result["error"]["code"] in {"request_in_progress", "lease_conflict"}
        # no writes happened
        assert _count("chat_logs", user_id) == 0
        rows = _run_sql(f"SELECT status FROM public.turn_requests WHERE user_id = '{user_id}'")
        assert rows[0]["status"] == "pending"
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 16. Pending + active lease of the same worker can continue
# ---------------------------------------------------------------------------


def test_pending_active_lease_same_worker_continues(service_client: Client):
    user_id = _uid("lease_same")
    request_id = str(uuid.uuid4())
    try:
        _create_profile(service_client, user_id)
        _insert_pending_request(
            user_id, request_id, _hash_a(), "worker-a", "2099-01-01T00:00:00Z"
        )
        result = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), lease_owner="worker-a"),
        )
        assert "error" not in result
        assert result["committed_revision"] == 1
        rows = _run_sql(
            f"SELECT status, lease_owner, lease_expires_at FROM public.turn_requests "
            f"WHERE user_id = '{user_id}'"
        )
        # transition to completed cleared the lease
        assert rows[0]["status"] == "completed"
        assert rows[0]["lease_owner"] is None
        assert rows[0]["lease_expires_at"] is None
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 17. Pending + expired lease is reclaimed atomically
# ---------------------------------------------------------------------------


def test_pending_expired_lease_reclaimed(service_client: Client):
    user_id = _uid("lease_expired")
    request_id = str(uuid.uuid4())
    try:
        _create_profile(service_client, user_id)
        _insert_pending_request(
            user_id, request_id, _hash_a(), "worker-dead", "2000-01-01T00:00:00Z"
        )
        result = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), lease_owner="worker-new"),
        )
        assert "error" not in result
        assert result["committed_revision"] == 1
        rows = _run_sql(f"SELECT status, lease_owner FROM public.turn_requests WHERE user_id = '{user_id}'")
        assert rows[0]["status"] == "completed"
        assert rows[0]["lease_owner"] is None
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 18. Expired + same hash can be reclaimed
# ---------------------------------------------------------------------------


def test_expired_same_hash_reclaimed(service_client: Client):
    user_id = _uid("expired_reclaim")
    request_id = str(uuid.uuid4())
    try:
        _create_profile(service_client, user_id)
        _insert_expired_request(user_id, request_id, _hash_a())
        result = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), lease_owner="worker-new"),
        )
        assert "error" not in result
        assert result["committed_revision"] == 1
        rows = _run_sql(f"SELECT status, error_code FROM public.turn_requests WHERE user_id = '{user_id}'")
        assert rows[0]["status"] == "completed"
        assert rows[0]["error_code"] is None
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 18b. Reclaim requires a non-empty, validated lease owner
# ---------------------------------------------------------------------------


def test_reclaim_requires_non_empty_lease_owner(service_client: Client):
    user_id = _uid("lease_required")
    request_id = str(uuid.uuid4())
    try:
        _create_profile(service_client, user_id)
        _insert_expired_request(user_id, request_id, _hash_a())
        # Empty lease owner fails validation and must not reclaim anything.
        result = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), lease_owner=""),
        )
        assert result["error"]["code"] in {"lease_conflict", "validation_failed"}
        rows = _run_sql(
            f"SELECT status FROM public.turn_requests WHERE user_id = '{user_id}'"
        )
        assert rows[0]["status"] == "expired"
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 19. Any status with divergent hash -> payload conflict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "expired", "completed"])
def test_divergent_hash_conflicts_for_any_status(service_client: Client, status: str):
    user_id = _uid(f"div_{status}")
    request_id = str(uuid.uuid4())
    try:
        _create_profile(service_client, user_id)
        if status == "pending":
            _insert_pending_request(user_id, request_id, _hash_a(), "worker-a", "2099-01-01T00:00:00Z")
        elif status == "expired":
            _insert_expired_request(user_id, request_id, _hash_a())
        else:
            _insert_completed_request(user_id, request_id, _hash_a())

        result = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_b(), lease_owner="worker-x"),
        )
        assert result["error"]["code"] == "request_payload_conflict"
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 20. Two simultaneous reclaim attempts do not both win
# ---------------------------------------------------------------------------


def test_simultaneous_reclaims_do_not_both_win(supabase_url, service_role_key):
    user_id = _uid("reclaim_race")
    request_id = str(uuid.uuid4())
    client = create_client(supabase_url, service_role_key)
    try:
        _create_profile(client, user_id)
        _insert_pending_request(
            user_id, request_id, _hash_a(), "worker-dead", "2000-01-01T00:00:00Z"
        )

        barrier = threading.Barrier(2)
        calls = [
            _ConcurrentCommit(user_id, request_id, _hash_a(), lease_owner="worker-1", expected_revision=0),
            _ConcurrentCommit(user_id, request_id, _hash_a(), lease_owner="worker-2", expected_revision=0),
        ]
        results = _run_concurrent(url=supabase_url, key=service_role_key, barrier=barrier, calls=calls)

        # Both calls may succeed (one fresh reclaim + one replay), but only one
        # set of writes exists: exactly 2 messages, revision incremented once.
        assert all("error" not in r for r in results)
        assert _count("chat_logs", user_id) == 2
        assert _count("turn_requests", user_id) == 1
        rows = _run_sql(f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'")
        assert rows[0]["revision"] == 1
    finally:
        _cleanup_user(client, user_id)


def test_reclaim_miss_rolls_back_profile_and_messages(service_client: Client):
    user_id = _uid("reclaim_rollback")
    request_id = str(uuid.uuid4())
    try:
        _create_profile(service_client, user_id)
        _insert_pending_request(
            user_id, request_id, _hash_a(), "worker-dead", "2000-01-01T00:00:00Z"
        )
        _install_reclaim_miss_trigger()
        result = _call_commit(
            service_client,
            _commit_params(
                user_id, request_id, payload_hash=_hash_a(), lease_owner="worker-1"
            ),
        )
        assert result["error"]["code"] == "lease_conflict"
        assert _count("chat_logs", user_id) == 0
        rows = _run_sql(
            f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'"
        )
        assert rows[0]["revision"] == 0
        request_rows = _run_sql(
            "SELECT status, lease_owner FROM public.turn_requests "
            f"WHERE user_id = '{user_id}' AND request_id = '{request_id}'"
        )
        assert request_rows == [{"status": "pending", "lease_owner": "worker-dead"}]
    finally:
        _drop_reclaim_miss_trigger()
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 21. Pruning messages does not break replay
# ---------------------------------------------------------------------------


def test_pruning_messages_does_not_break_replay(service_client: Client):
    user_id = _uid("prune")
    request_id = str(uuid.uuid4())
    try:
        first = _call_commit(service_client, _commit_params(user_id, request_id, payload_hash=_hash_a()))
        assert "error" not in first
        # prune all messages (turn_requests refs are nulled by the trigger)
        service_client.table("chat_logs").delete().eq("user_id", user_id).execute()
        assert _count("chat_logs", user_id) == 0
        # replay must still return the identical public result
        replay = _call_commit(service_client, _commit_params(user_id, request_id, payload_hash=_hash_a()))
        assert _normalized(replay) == _normalized(first)
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 22. Outbox status/attempts change does not change replay
# ---------------------------------------------------------------------------


def test_outbox_status_change_does_not_change_replay(service_client: Client):
    user_id = _uid("outbox_status")
    request_id = str(uuid.uuid4())
    try:
        first = _call_commit(
            service_client,
            _commit_params(
                user_id,
                request_id,
                payload_hash=_hash_a(),
                outbox_events=[
                    {"event_type": "memory_indexed", "payload": {"ref": "m1"}, "idempotency_key": "k1"},
                ],
            ),
        )
        assert "error" not in first
        # advance the outbox event to a coherent 'completed' state
        _run_sql(
            "UPDATE public.outbox_events SET status='completed', attempts=1, "
            "next_attempt_at=NULL, processed_at=now(), retention_until=now() + INTERVAL '30 days' "
            f"WHERE user_id = '{user_id}'"
        )
        replay = _call_commit(
            service_client,
            _commit_params(
                user_id,
                request_id,
                payload_hash=_hash_a(),
                outbox_events=[
                    {"event_type": "memory_indexed", "payload": {"ref": "m1"}, "idempotency_key": "k1"},
                ],
            ),
        )
        assert _normalized(replay) == _normalized(first)
        assert replay["outbox_events"] == first["outbox_events"]
    finally:
        _cleanup_user(service_client, user_id)


def test_outbox_deletion_and_stable_field_mutation_do_not_change_replay(service_client: Client):
    user_id = _uid("outbox_immutable")
    request_id = str(uuid.uuid4())
    events = [
        {"event_type": "memory_indexed", "payload": {"ref": "m1"}, "idempotency_key": "k1"},
    ]
    try:
        first = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), outbox_events=events),
        )
        ref = first["outbox_events"][0]
        _run_sql(
            "UPDATE public.outbox_events SET event_type='turn_completed', "
            f"idempotency_key='mutated-key' WHERE id = '{ref['id']}'"
        )
        _run_sql(f"DELETE FROM public.outbox_events WHERE id = '{ref['id']}'")
        replay = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), outbox_events=events),
        )
        assert replay["outbox_events"] == first["outbox_events"]
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 23. Outbox reference order is deterministic
# ---------------------------------------------------------------------------


def test_outbox_reference_order_is_deterministic(service_client: Client):
    user_id = _uid("order")
    request_id = str(uuid.uuid4())
    events = [
        {"event_type": "memory_indexed", "payload": {"ref": "m1"}, "idempotency_key": "k1"},
        {"event_type": "turn_completed", "payload": {"ref": "t1"}, "idempotency_key": "k2"},
        {"event_type": "memory_indexed", "payload": {"ref": "m3"}, "idempotency_key": "k3"},
    ]
    try:
        first = _call_commit(
            service_client,
            _commit_params(user_id, request_id, payload_hash=_hash_a(), outbox_events=events),
        )
        assert "error" not in first
        refs1 = [e["id"] for e in first["outbox_events"]]
        assert len(refs1) == 3

        for _ in range(2):
            replay = _call_commit(
                service_client,
                _commit_params(user_id, request_id, payload_hash=_hash_a(), outbox_events=events),
            )
            refs2 = [e["id"] for e in replay["outbox_events"]]
            assert refs2 == refs1
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 24. Divergent p_public_response rejected
# ---------------------------------------------------------------------------


def test_divergent_public_response_rejected(service_client: Client):
    user_id = _uid("pubresp")
    replay = _replay_payload("Authoritative response")
    params = _commit_params(
        user_id,
        str(uuid.uuid4()),
        payload_hash=_hash_a(),
        response="Different response",
        replay_payload=replay,
    )
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


@pytest.mark.parametrize("bad_response", [123, True, {"nested": "object"}])
def test_replay_response_must_be_string(service_client: Client, bad_response):
    user_id = _uid("replay_response_type")
    try:
        replay = _replay_payload()
        replay["response"] = bad_response
        result = _call_commit(
            service_client,
            _commit_params(
                user_id,
                str(uuid.uuid4()),
                payload_hash=_hash_a(),
                response=str(bad_response),
                replay_payload=replay,
            ),
        )
        assert result["error"]["code"] == "validation_failed"
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


def test_replay_request_id_must_match_outer_request(service_client: Client):
    user_id = _uid("replay_request_id")
    request_id = str(uuid.uuid4())
    replay = _replay_payload()
    replay["request_id"] = str(uuid.uuid4())
    try:
        result = _call_commit(
            service_client,
            _commit_params(
                user_id,
                request_id,
                payload_hash=_hash_a(),
                replay_payload=replay,
            ),
        )
        assert result["error"]["code"] == "validation_failed"
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


def test_replay_request_id_must_be_lowercase_canonical(service_client: Client):
    user_id = _uid("replay_request_id_case")
    request_id = str(uuid.uuid4())
    replay = _replay_payload()
    replay["request_id"] = request_id.upper()
    try:
        result = _call_commit(
            service_client,
            _commit_params(
                user_id,
                request_id,
                payload_hash=_hash_a(),
                replay_payload=replay,
            ),
        )
        assert result["error"]["code"] == "validation_failed"
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 25. schema_version textual/boolean/incompatible rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_schema_version",
    ["1", True, 2, 1.5],
)
def test_bad_schema_version_rejected(service_client: Client, bad_schema_version):
    user_id = _uid("schema_ver")
    emotional = _emotional_state()
    emotional["schema_version"] = bad_schema_version
    params = _commit_params(
        user_id,
        str(uuid.uuid4()),
        payload_hash=_hash_a(),
        emotional_state=emotional,
    )
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
        assert _count("chat_logs", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: state.pop("energy"),
        lambda state: state.update({"unexpected": 1}),
        lambda state: state.update({"pleasure": 2.0}),
    ],
)
def test_invalid_snapshot_shape_or_number_rejected(service_client: Client, mutate):
    user_id = _uid("snapshot_shape")
    emotional = _emotional_state()
    mutate(emotional)
    try:
        result = _call_commit(
            service_client,
            _commit_params(
                user_id,
                str(uuid.uuid4()),
                payload_hash=_hash_a(),
                emotional_state=emotional,
            ),
        )
        assert result["error"]["code"] == "validation_failed"
        assert _count("profiles", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


def test_new_profile_rejects_null_snapshots(service_client: Client):
    user_id = _uid("null_snapshot")
    params = _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_a())
    params["p_emotional_state"] = None
    params["p_relationship_state"] = None
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
        assert _count("profiles", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("p_payload_hash_sha256", "a" * 63),
        ("p_payload_hash_sha256", "a" * 65),
        ("p_payload_hash_sha256", "A" * 64),
        ("p_lease_owner", "w" * 65),
    ],
)
def test_sql_scalar_regex_boundaries_rejected(service_client: Client, field: str, value: str):
    user_id = _uid("sql_scalar_boundary")
    params = _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_a())
    params[field] = value
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
        assert _count("profiles", user_id) == 0
        assert _count("turn_requests", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


@pytest.mark.parametrize(
    "event",
    [
        {"event_type": "a" * 65, "payload": {}, "idempotency_key": "k"},
        {"event_type": "turn_completed", "payload": {"ref": "x"}, "idempotency_key": "k" * 129},
        {"event_type": "turn_completed", "payload": {"ref": "x" * 129}, "idempotency_key": "k"},
    ],
)
def test_sql_outbox_boundaries_rejected(service_client: Client, event: dict):
    user_id = _uid("sql_outbox_boundary")
    params = _commit_params(
        user_id, str(uuid.uuid4()), payload_hash=_hash_a(), outbox_events=[event]
    )
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
        assert _count("profiles", user_id) == 0
        assert _count("turn_requests", user_id) == 0
    finally:
        _cleanup_user(service_client, user_id)


def test_backend_adapter_rejects_nonfinite_snapshot_before_rpc(service_client: Client):
    """JSON has no non-finite number, so reject it before the real client serializes it."""
    user_id = _uid("snapshot_nonfinite")
    request_id = str(uuid.uuid4())
    emotional = _emotional_state()
    emotional["pleasure"] = math.nan
    replay = _replay_payload()
    replay["request_id"] = request_id

    async def rpc_client(name: str, params: dict) -> dict:
        return _call_commit(service_client, params)

    with pytest.raises(ValidationError) as exc:
        asyncio.run(
            commit_turn(
                rpc_client=rpc_client,
                authenticated_user_id=user_id,
                request_id=request_id,
                expected_revision=0,
                user_message="Hello",
                assistant_message="Hi there!",
                emotional_state=emotional,
                relationship_state=_relationship_state(),
                public_response="Hi there!",
                outbox_events=[],
                replay_payload=replay,
            )
        )
    assert exc.value.code == "invalid_emotional_state"
    assert _count("profiles", user_id) == 0


def test_backend_commit_turn_real_client_replays_and_conflicts(service_client: Client):
    user_id = _uid("backend_adapter")
    request_id = str(uuid.uuid4())
    replay = _replay_payload("Adapter response")
    replay["request_id"] = request_id
    outbox_events = [("turn_completed", {"ref": "adapter"}, "adapter-key")]

    async def rpc_client(name: str, params: dict) -> dict:
        return _call_commit(service_client, params)

    async def invoke(assistant_message: str):
        return await commit_turn(
            rpc_client=rpc_client,
            authenticated_user_id=user_id,
            request_id=request_id,
            expected_revision=0,
            user_message="Hello",
            assistant_message=assistant_message,
            emotional_state=_emotional_state(),
            relationship_state=_relationship_state(),
            public_response="Adapter response",
            outbox_events=outbox_events,
            replay_payload=replay,
        )

    try:
        first = asyncio.run(invoke("Adapter response"))
        replayed = asyncio.run(invoke("Adapter response"))
        assert replayed.to_db_row() == first.to_db_row()

        with pytest.raises(ConflictError) as exc:
            asyncio.run(invoke("Changed hashed assistant message"))
        assert exc.value.code == "request_payload_conflict"
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 26. Nested user_id and bond_label rejected
# ---------------------------------------------------------------------------


def test_nested_user_id_rejected(service_client: Client):
    user_id = _uid("nested_uid")
    emotional = _emotional_state()
    emotional["nested"] = {"deep": {"user_id": "attacker"}}
    params = _commit_params(
        user_id,
        str(uuid.uuid4()),
        payload_hash=_hash_a(),
        emotional_state=emotional,
    )
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
    finally:
        _cleanup_user(service_client, user_id)


def test_nested_bond_label_rejected(service_client: Client):
    user_id = _uid("nested_bl")
    relationship = _relationship_state()
    relationship["triggers"] = [{"bond_label": "hidden"}]
    params = _commit_params(
        user_id,
        str(uuid.uuid4()),
        payload_hash=_hash_a(),
        relationship_state=relationship,
    )
    try:
        result = _call_commit(service_client, params)
        assert result["error"]["code"] == "validation_failed"
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 27/28. Authorization
# ---------------------------------------------------------------------------


def _authz_params() -> dict:
    return _commit_params("anon-target", str(uuid.uuid4()), payload_hash=_hash_a())


def test_anon_cannot_execute_rpc(anon_client: Client):
    with pytest.raises(APIError) as exc:
        anon_client.rpc("commit_turn", _authz_params()).execute()
    assert getattr(exc.value, "code", None) == "42501"


def test_authenticated_cannot_execute_rpc(auth_client: tuple[Client, str]):
    client, _ = auth_client
    with pytest.raises(APIError) as exc:
        client.rpc("commit_turn", _authz_params()).execute()
    assert getattr(exc.value, "code", None) == "42501"


def test_service_role_can_execute_rpc(service_client: Client):
    user_id = _uid("svc")
    try:
        result = _call_commit(service_client, _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_a()))
        assert "error" not in result
        assert result["committed_revision"] == 1
    finally:
        _cleanup_user(service_client, user_id)


# ---------------------------------------------------------------------------
# 29. Unexpected failure aborts the RPC with no partial writes
# ---------------------------------------------------------------------------


def test_unexpected_failure_aborts_rpc_no_partial_writes(service_client: Client):
    user_id = _uid("abort")
    params = _commit_params(user_id, str(uuid.uuid4()), payload_hash=_hash_a())
    try:
        # Fail on the SECOND message insert: by then profile + first message
        # exist inside the transaction, but everything must roll back.
        _install_trigger("abort", "chat_logs", "NEW.role = 'assistant'", "boom")
        with pytest.raises(APIError) as exc:
            _call_commit(service_client, params)
        assert exc.value.code == "P0001"
        assert _count("chat_logs", user_id) == 0
        assert _count("profiles", user_id) == 0
        assert _count("turn_requests", user_id) == 0
        assert _count("outbox_events", user_id) == 0
    finally:
        _drop_trigger("abort", "chat_logs")
        _cleanup_user(service_client, user_id)
