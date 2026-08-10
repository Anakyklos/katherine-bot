"""Real Supabase integration tests for the #324 account deletion ledger.

This file is executed ONLY by the database CI job against a freshly reset
local Supabase instance (no mocks: real PostgreSQL transactions, RLS,
grants, advisory locks and the ledger RPCs). It must never be collected by
the ordinary backend unit job (see the ignore list in
``.github/workflows/ci.yml``).

Covers the mandatory scenarios of #324:

 1.  The tombstone is created BEFORE any purge (request -> pending, data
     untouched).
 2.  Replay of the same (ref, operation_id, fingerprint) is idempotent.
 3.  A divergent intent fingerprint produces a sanitized conflict.
 4.  The same operation_id for users A and B never interferes.
 5.  Two concurrent claims on independent connections: exactly one wins.
 6.  An expired lease makes the job eligible again.
 7.  An old worker cannot finalize after losing the lease.
 8.  Purge of A fully preserves B.
 9.  Every table of user A is removed.
10.  The real FK graph does not break the purge.
11.  An artificial failure mid-purge rolls back EVERY delete.
12.  db_purged_at does not appear after the rollback.
13.  A repeated purge on an empty database is a safe replay.
14.  Finalize clears the raw user_id.
15.  The user_ref HMAC persists after finalize.
16.  The tombstone stays queryable by the defined contract.
17.  Active/failed jobs are never purged by age.
18.  Concurrent purge vs same-user writers is serialized by the per-user
     advisory lock.
19.  A different user is not globally blocked.

Uses independent connections, barriers and direct SQL state manipulation
(no long sleeps).

Since #325 this file also exercises the durable worker end to end against
the real database with a FAKE Auth Admin boundary (scenarios 20-30 below):
DB-first ordering, empty-queue nominal behavior, retry/attempts gating by
``next_attempt_at``, lease-loss recovery, crash-after-purge re-executability,
two real workers on independent clients, ``completed`` + user_id
minimization, and preservation of account B.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from supabase import Client, create_client

from backend.account_deletion import (
    SupabaseAccountDeletionRepository,
    compute_account_deletion_user_ref,
    compute_intent_fingerprint,
)
from backend.account_deletion_worker import (
    ERROR_AUTH_UNAVAILABLE,
    OUTCOME_ALREADY_ABSENT,
    OUTCOME_AUTH_ERROR,
    OUTCOME_DELETED,
    AccountDeletionWorker,
    AccountDeletionWorkerConfig,
    AuthDeleteResult,
)
from backend.atomic_turn_commit import PersistenceError
from backend.privacy_operations import (
    OPERATION_DELETE_HISTORY,
    run_privacy_operation,
)

_SUPABASE_CLI = ["supabase"] if shutil.which("supabase") else ["npx", "supabase"]

SECRET = b"ci-test-secret-0123456789abcdef0123456789abcdef"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for account deletion integration tests"
    return value


def _close_client(client: Client) -> None:
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
    auth = getattr(client, "auth", None)
    if auth is not None and hasattr(auth, "close"):
        auth.close()


@pytest.fixture(scope="module")
def supabase_url() -> str:
    return _required_env("SUPABASE_URL")


@pytest.fixture(scope="module")
def service_role_key() -> str:
    return _required_env("SUPABASE_SERVICE_ROLE_KEY")


@pytest.fixture(scope="module")
def service_client(supabase_url: str, service_role_key: str) -> Client:
    client = create_client(supabase_url, service_role_key)
    yield client
    _close_client(client)


# ─── SQL helpers (pinned local Supabase CLI, sanitized) ─────────────────────


def _run_sql(sql: str) -> list[dict]:
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
    assert result.returncode == 0, "sanitized account deletion test SQL operation failed"
    output = result.stdout.strip()
    if not output or output[0] not in "[{":
        return []
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    return parsed


def _count(table: str, predicate: str) -> int:
    rows = _run_sql(f"SELECT count(*)::integer AS count FROM public.{table} WHERE {predicate}")
    return rows[0]["count"]


def _uid(label: str) -> str:
    return f"adl_{label}_{uuid.uuid4().hex[:12]}"


def _ref(user_id: str) -> str:
    return compute_account_deletion_user_ref(SECRET, user_id)


def _fingerprint() -> str:
    return compute_intent_fingerprint({"op": "delete_account", "scope": ["db", "auth"]})


def _new_op_id() -> str:
    return str(uuid.uuid4())


def _repo(client: Client) -> SupabaseAccountDeletionRepository:
    return SupabaseAccountDeletionRepository(client)


def _privacy_delete_history(client: Client, user_id: str) -> None:
    """Run delete_history (same per-user advisory lock as the purge)."""

    async def _rpc_client(name: str, params: dict) -> dict:
        response = client.rpc(name, params).execute()
        data = response.data
        if isinstance(data, list):
            data = data[0]
        return data

    async def _run():
        return await run_privacy_operation(
            rpc_client=_rpc_client,
            operation=OPERATION_DELETE_HISTORY,
            authenticated_user_id=user_id,
            operation_id=str(uuid.uuid4()),
            payload={},
        )

    asyncio.run(_run())


# ─── Seeding ────────────────────────────────────────────────────────────────


def _seed_profile(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.profiles (user_id, persona_config, user_profile, "
        "relationship_state, emotional_state, revision) VALUES "
        f"('{user_id}', 'persona', '{{}}'::jsonb, "
        f"'{{\"schema_version\":1,\"trust\":0.5,\"affection\":0.3,\"tension\":0.0,"
        f"\"triggers\":[],\"timestamp\":1700000000.0}}'::jsonb, "
        f"'{{\"schema_version\":1,\"pleasure\":0.0,\"arousal\":0.0,\"dominance\":0.0,"
        f"\"libido\":0.0,\"aggression\":0.0,\"connection\":0.5,\"energy\":0.8,"
        f"\"tension\":0.0,\"coping_mode\":\"HEALTHY\",\"timestamp\":1700000000.0}}'::jsonb, "
        "1)"
    )


def _seed_chat(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.chat_logs (user_id, role, content) VALUES "
        f"('{user_id}', 'user', 'hello'), ('{user_id}', 'assistant', 'hi there')"
    )


def _seed_memories(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.memories (user_id, content, metadata) VALUES "
        f"('{user_id}', 'a durable memory', '{{\"tags\":[\"x\"]}}'::jsonb)"
    )


def _seed_archival(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.archival_extractions "
        "(user_id, source_chat_log_id, extractor_version, schema_version, "
        "idempotency_key, facts) "
        "SELECT user_id, id, 1, 1, "
        f"'{user_id}-arch-1', '{{\"facts\":[]}}'::jsonb "
        f"FROM public.chat_logs WHERE user_id = '{user_id}' AND role = 'user'"
    )


def _seed_turn_request(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.turn_requests ("
        "id, user_id, request_id, payload_hash_sha256, status, expected_revision, "
        "committed_revision, replay_payload, created_at, updated_at, completed_at) "
        "VALUES ("
        f"'{uuid.uuid4()}', '{user_id}', '{uuid.uuid4()}', "
        f"'{'a' * 64}', 'completed', 0, 1, '{{\"response\":\"hi\"}}'::jsonb, "
        "now(), now(), now())"
    )


def _seed_outbox(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.outbox_events ("
        "event_type, contract_version, user_id, turn_request_id, payload, status, "
        "attempts, next_attempt_at, idempotency_key, created_at, updated_at) "
        "VALUES ("
        f"'turn_completed', 1, '{user_id}', NULL, '{{\"ref\":\"t1\"}}'::jsonb, "
        f"'pending', 0, now() + interval '1 second', '{user_id}-k1', now(), now())"
    )


def _seed_admission(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.admission_reservations ("
        "user_id, request_id, message_hmac_sha256, network_hmac_sha256, estimated_units) "
        "VALUES ("
        f"'{user_id}', '{uuid.uuid4()}', repeat('a', 64), repeat('b', 64), 10)"
    )


def _seed_privacy_ledger(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.privacy_operations ("
        "user_id, operation_id, operation, operation_payload_sha256, status, result) "
        "VALUES ("
        f"'{user_id}', '{uuid.uuid4()}', 'delete_history', repeat('c', 64), "
        "'applied', '{}'::jsonb)"
    )


def _seed_full_user(user_id: str) -> None:
    """Seed every table the purge removes (the real FK graph)."""
    _seed_profile(user_id)
    _seed_chat(user_id)
    _seed_memories(user_id)
    _seed_archival(user_id)
    _seed_turn_request(user_id)
    _seed_outbox(user_id)
    _seed_admission(user_id)
    _seed_privacy_ledger(user_id)


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
    _run_sql(f"DELETE FROM public.account_deletion_jobs WHERE user_ref_hmac_sha256 = '{_ref(user_id)}'")


# ─── 1/2/3/4. Request semantics ─────────────────────────────────────────────


def test_tombstone_created_before_any_purge(service_client: Client):
    user_id = _uid("tomb")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        result = _repo(service_client).request(user_id, ref, op_id, fp)
        assert result.status == "created"
        assert result.job_status == "pending"
        # The tombstone exists BEFORE any purge; the data is untouched.
        tombstone = _repo(service_client).has_tombstone(ref)
        assert tombstone.exists is True
        assert tombstone.status == "pending"
        assert _count("profiles", f"user_id = '{user_id}'") == 1
        assert _count("chat_logs", f"user_id = '{user_id}'") == 2
    finally:
        _cleanup_user(user_id)


def test_replay_is_idempotent(service_client: Client):
    user_id = _uid("replay")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        first = _repo(service_client).request(user_id, ref, op_id, fp)
        second = _repo(service_client).request(user_id, ref, op_id, fp)
        assert first.status == "created"
        assert second.status == "replay"
        assert second.job_id == first.job_id
        assert _count("account_deletion_jobs", f"user_ref_hmac_sha256 = '{ref}'") == 1
    finally:
        _cleanup_user(user_id)


def test_divergent_fingerprint_conflicts(service_client: Client):
    user_id = _uid("conflict")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    other_fp = compute_intent_fingerprint({"op": "delete_account", "scope": ["db"]})
    try:
        _repo(service_client).request(user_id, ref, op_id, fp)
        with pytest.raises(Exception) as exc:
            _repo(service_client).request(user_id, ref, op_id, other_fp)
        assert "conflict" in str(exc.value).lower()
        assert _count("account_deletion_jobs", f"user_ref_hmac_sha256 = '{ref}'") == 1
    finally:
        _cleanup_user(user_id)


def test_same_operation_id_across_users_no_interference(service_client: Client):
    user_a = _uid("crossa")
    user_b = _uid("crossb")
    op_id = _new_op_id()  # Same operation_id for both users.
    fp = _fingerprint()
    try:
        result_a = _repo(service_client).request(user_a, _ref(user_a), op_id, fp)
        result_b = _repo(service_client).request(user_b, _ref(user_b), op_id, fp)
        assert result_a.status == "created"
        assert result_b.status == "created"
        assert result_a.job_id != result_b.job_id
        # Each user's tombstone is independent.
        assert _repo(service_client).has_tombstone(_ref(user_a)).exists is True
        assert _repo(service_client).has_tombstone(_ref(user_b)).exists is True
    finally:
        _cleanup_user(user_a)
        _cleanup_user(user_b)


# ─── 5/6/7. Leases ──────────────────────────────────────────────────────────


def test_concurrent_claims_only_one_wins(
    service_client: Client, supabase_url: str, service_role_key: str
):
    user_id = _uid("claim")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    winners: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    try:
        client = create_client(supabase_url, service_role_key)
        _repo(client).request(user_id, ref, op_id, fp)
        _close_client(client)

        def _claim(worker: str):
            own_client = create_client(supabase_url, service_role_key)
            try:
                barrier.wait(timeout=30)
                job = _repo(own_client).acquire_lease(worker, 60, 100)
                if job is not None:
                    with lock:
                        winners.append(job.job_id)
                # The loser receives the nominal empty-queue None (#325).
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                _close_client(own_client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_claim, f"worker-{i}") for i in range(2)]
            for future in futures:
                future.result(timeout=60)

        assert errors == [], f"concurrent claims failed: {errors}"
        assert len(winners) == 1, "exactly one worker must win the claim"
        # A third claim while the lease is active finds no eligible job:
        # the empty-queue result is nominal None (#325), not an error.
        assert _repo(service_client).acquire_lease("worker-2", 60, 100) is None
    finally:
        _cleanup_user(user_id)


def test_expired_lease_requeues_job(service_client: Client):
    user_id = _uid("lease")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        job = _repo(service_client).acquire_lease("worker-a", 60, 100)
        assert job.job_id == request.job_id
        # A second claim while the lease is active finds nothing: the
        # empty-queue result is nominal None (#325), not an error.
        assert _repo(service_client).acquire_lease("worker-b", 60, 100) is None
        # Expire the lease directly (deterministic, no sleeps).
        _run_sql(
            "UPDATE public.account_deletion_jobs "
            "SET lease_expires_at = now() - interval '1 second' "
            f"WHERE job_id = '{job.job_id}'"
        )
        # Now the job is eligible again: a new worker can claim it.
        reclaimed = _repo(service_client).acquire_lease("worker-b", 60, 100)
        assert reclaimed.job_id == job.job_id
        assert reclaimed.attempts == job.attempts + 1
    finally:
        _cleanup_user(user_id)


def test_old_worker_cannot_finalize_after_losing_lease(service_client: Client):
    user_id = _uid("stale")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        job = _repo(service_client).acquire_lease("worker-old", 60, 100)
        # Expire the lease; worker-new reclaims the job.
        _run_sql(
            "UPDATE public.account_deletion_jobs "
            "SET lease_expires_at = now() - interval '1 second' "
            f"WHERE job_id = '{job.job_id}'"
        )
        _repo(service_client).acquire_lease("worker-new", 60, 100)
        # The old worker can no longer purge or finalize: fail closed.
        with pytest.raises(PersistenceError):
            _repo(service_client).purge(job.job_id, "worker-old", fp)
        with pytest.raises(PersistenceError):
            _repo(service_client).finalize(job.job_id, "worker-old")
        assert request.job_id == job.job_id
    finally:
        _cleanup_user(user_id)


# ─── 8/9/10. Purge semantics ────────────────────────────────────────────────


def test_purge_removes_all_a_tables_and_preserves_b(service_client: Client):
    user_a = _uid("pa")
    user_b = _uid("pb")
    _seed_full_user(user_a)
    _seed_full_user(user_b)
    ref_a = _ref(user_a)
    op_a = _new_op_id()
    fp_a = _fingerprint()
    try:
        request = _repo(service_client).request(user_a, ref_a, op_a, fp_a)
        job = _repo(service_client).acquire_lease("worker-a", 60, 100)
        assert job.user_id == user_a
        result = _repo(service_client).purge(job.job_id, "worker-a", fp_a)
        assert result.status == "purged"
        # Every table of A is gone (the real FK graph did not break).
        for table in (
            "outbox_events",
            "turn_requests",
            "archival_extractions",
            "memories",
            "chat_logs",
            "admission_reservations",
            "privacy_operations",
            "profiles",
        ):
            assert _count(table, f"user_id = '{user_a}'") == 0, f"{table} not purged"
        # B is completely preserved.
        expected_b = {
            "outbox_events": 1,
            "turn_requests": 1,
            "archival_extractions": 1,
            "memories": 1,
            "chat_logs": 2,
            "admission_reservations": 1,
            "privacy_operations": 1,
            "profiles": 1,
        }
        for table, expected in expected_b.items():
            assert _count(table, f"user_id = '{user_b}'") == expected, (
                f"{table} of B lost"
            )
        # The tombstone survived the purge.
        assert _count("account_deletion_jobs", f"user_ref_hmac_sha256 = '{ref_a}'") == 1
    finally:
        _cleanup_user(user_a)
        _cleanup_user(user_b)


def test_artificial_failure_mid_purge_rolls_back(service_client: Client):
    user_id = _uid("rollback")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        job = _repo(service_client).acquire_lease("worker-rb", 60, 100)
        # Install a BEFORE DELETE trigger on chat_logs for this user that
        # fails: the purge deletes outbox/turn_requests/archival/memories
        # first, then fails on chat_logs -> full rollback.
        _run_sql(
            "CREATE OR REPLACE FUNCTION public.adl_test_fail_trigger() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN "
            "RAISE EXCEPTION 'adl-test artificial failure'; END $$;"
        )
        _run_sql(
            "CREATE TRIGGER adl_test_fail BEFORE DELETE ON public.chat_logs "
            f"FOR EACH ROW WHEN (OLD.user_id = '{user_id}') "
            "EXECUTE FUNCTION public.adl_test_fail_trigger()"
        )
        try:
            with pytest.raises(PersistenceError):
                _repo(service_client).purge(job.job_id, "worker-rb", fp)
        finally:
            _run_sql("DROP TRIGGER IF EXISTS adl_test_fail ON public.chat_logs")
            _run_sql("DROP FUNCTION IF EXISTS public.adl_test_fail_trigger()")
        # Rollback was integral: every table of A still has its rows.
        for table, expected in (
            ("outbox_events", 1),
            ("turn_requests", 1),
            ("archival_extractions", 1),
            ("memories", 1),
            ("chat_logs", 2),
            ("admission_reservations", 1),
            ("privacy_operations", 1),
            ("profiles", 1),
        ):
            assert _count(table, f"user_id = '{user_id}'") == expected, (
                f"{table} not rolled back"
            )
        # db_purged_at did NOT appear; the job is still processing and the
        # tombstone remains.
        row = _run_sql(
            "SELECT db_purged_at IS NULL AS purged_null, status "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{job.job_id}'"
        )[0]
        assert row["purged_null"] is True
        assert row["status"] == "processing"
    finally:
        _cleanup_user(user_id)


def test_repeated_purge_on_empty_db_is_safe(service_client: Client):
    user_id = _uid("repeat")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        job = _repo(service_client).acquire_lease("worker-r1", 60, 100)
        first = _repo(service_client).purge(job.job_id, "worker-r1", fp)
        assert first.status == "purged"
        # Simulate the crash-after-DB-commit scenario: expire the lease,
        # a new worker reclaims and purges again on the empty database.
        _run_sql(
            "UPDATE public.account_deletion_jobs "
            "SET lease_expires_at = now() - interval '1 second' "
            f"WHERE job_id = '{job.job_id}'"
        )
        reclaimed = _repo(service_client).acquire_lease("worker-r2", 60, 100)
        assert reclaimed.job_id == job.job_id
        second = _repo(service_client).purge(reclaimed.job_id, "worker-r2", fp)
        assert second.status == "already_purged"
        assert second.db_purged_at == first.db_purged_at
        assert request.job_id == job.job_id
    finally:
        _cleanup_user(user_id)


# ─── 14. Failure/retry AFTER the DB purge preserve db_purged_at ─────────────


def test_failure_after_purge_preserves_marker_and_retries_safely(service_client: Client):
    """The future flow purge DB -> Auth fails -> retry must be representable.

    After a successful purge, record_failure and record_retry transition the
    job to failed/pending WITHOUT clearing db_purged_at; a reacquired job's
    purge returns already_purged without repeating deletes; finalization
    remains possible and minimizes user_id.
    """
    user_id = _uid("postpurge")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        # Purge succeeds first.
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        job = _repo(service_client).acquire_lease("worker-pp", 60, 100)
        purged = _repo(service_client).purge(job.job_id, "worker-pp", fp)
        assert purged.status == "purged"
        assert purged.db_purged_at is not None

        # Auth deletion fails (simulated): record_failure keeps the marker
        # and the tombstone stays blocking.
        failure = _repo(service_client).record_failure(
            job.job_id, "worker-pp", "auth_unavailable"
        )
        assert failure.status == "failed"
        row = _run_sql(
            "SELECT status, db_purged_at IS NOT NULL AS purged "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{job.job_id}'"
        )[0]
        assert row["status"] == "failed"
        assert row["purged"] is True, "db_purged_at must survive record_failure"
        assert _repo(service_client).has_tombstone(ref).exists is True

        # Reacquire after the retry window: the purge replays safely.
        _run_sql(
            "UPDATE public.account_deletion_jobs SET next_attempt_at = "
            "now() - interval '1 second' "
            f"WHERE job_id = '{job.job_id}'"
        )
        reclaimed = _repo(service_client).acquire_lease("worker-pp2", 60, 100)
        assert reclaimed.job_id == job.job_id
        assert reclaimed.db_purged_at == purged.db_purged_at
        replay = _repo(service_client).purge(reclaimed.job_id, "worker-pp2", fp)
        assert replay.status == "already_purged"
        # No delete was repeated: the user tables were already empty and
        # stay empty; counts are zero.
        assert replay.counts["profiles"] == 0
        assert _count("profiles", f"user_id = '{user_id}'") == 0

        # Purge -> record_retry -> reacquire is also safe.
        _repo(service_client).record_retry(reclaimed.job_id, "worker-pp2")
        row = _run_sql(
            "SELECT status, db_purged_at IS NOT NULL AS purged "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{job.job_id}'"
        )[0]
        assert row["status"] == "pending"
        assert row["purged"] is True, "db_purged_at must survive record_retry"
        _run_sql(
            "UPDATE public.account_deletion_jobs SET next_attempt_at = "
            "now() - interval '1 second' "
            f"WHERE job_id = '{job.job_id}'"
        )
        reclaimed2 = _repo(service_client).acquire_lease("worker-pp3", 60, 100)
        assert reclaimed2.job_id == job.job_id
        replay2 = _repo(service_client).purge(reclaimed2.job_id, "worker-pp3", fp)
        assert replay2.status == "already_purged"

        # Finalization after all retries is possible and minimizes user_id.
        finalized = _repo(service_client).finalize(reclaimed2.job_id, "worker-pp3")
        assert finalized.status == "completed"
        row = _run_sql(
            "SELECT user_id, user_ref_hmac_sha256, status "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{job.job_id}'"
        )[0]
        assert row["user_id"] is None
        assert row["user_ref_hmac_sha256"] == ref
        assert row["status"] == "completed"
        assert request.job_id == job.job_id
    finally:
        _cleanup_user(user_id)


def test_attempts_exhaustion_is_terminal_and_tombstone_preserved(service_client: Client):
    """The attempts ceiling produces a deterministic terminal state.

    The last allowed claim reaches the ceiling; record_failure at the
    ceiling makes the job terminal (next_attempt_at NULL); no new automatic
    claim happens; the tombstone stays blocking and preserved.
    """
    user_id = _uid("exhaust")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        # Fast-forward to the boundary: 99 attempts already consumed.
        _run_sql(
            "UPDATE public.account_deletion_jobs SET attempts = 99 "
            f"WHERE job_id = '{request.job_id}'"
        )
        # Last allowed claim: 99 -> 100.
        job = _repo(service_client).acquire_lease("worker-ex", 60, 100)
        assert job.attempts == 100
        # record_failure at the ceiling -> TERMINAL (next_attempt_at NULL).
        failure = _repo(service_client).record_failure(
            job.job_id, "worker-ex", "auth_unavailable"
        )
        assert failure.status == "failed"
        row = _run_sql(
            "SELECT next_attempt_at IS NULL AS terminal "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{job.job_id}'"
        )[0]
        assert row["terminal"] is True
        # No new automatic claim: the exhausted job is never selected.
        assert _repo(service_client).acquire_lease("worker-ex2", 60, 100) is None
        # The tombstone stays blocking.
        assert _repo(service_client).has_tombstone(ref).exists is True
        assert _count("account_deletion_jobs", f"user_ref_hmac_sha256 = '{ref}'") == 1
    finally:
        _cleanup_user(user_id)


def test_expired_ceiling_lease_is_terminalized_not_reclaimed(service_client: Client):
    """A processing job whose lease expired AT the attempts ceiling is
    terminalized in place and NEVER delivered to another worker.

    This closes the exhaustion loophole: without this, the last attempt
    dying before record_failure/record_retry could be reclaimed an
    unlimited number of times. The terminal state preserves the tombstone
    and db_purged_at, and other eligible jobs still progress.
    """
    user_id = _uid("ceil")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        # Fast-forward to the boundary and simulate a worker that died
        # with the lease held at the ceiling (attempt 100), without ever
        # calling record_failure/record_retry.
        _run_sql(
            "UPDATE public.account_deletion_jobs "
            "SET attempts = 100, status = 'processing', "
            "lease_owner = 'worker-dead', "
            "lease_expires_at = now() - interval '1 second' "
            f"WHERE job_id = '{request.job_id}'"
        )
        # The next poll must NOT hand the job to another worker: it is
        # terminalized and the poll reports no eligible job.
        assert _repo(service_client).acquire_lease("worker-new", 60, 100) is None
        row = _run_sql(
            "SELECT status, error_code, lease_owner IS NULL AS lease_cleared, "
            "next_attempt_at IS NULL AS terminal "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["status"] == "failed"
        assert row["error_code"] == "attempts_exhausted"
        assert row["lease_cleared"] is True
        assert row["terminal"] is True
        # The tombstone stays blocking and is never re-acquired.
        assert _repo(service_client).has_tombstone(ref).exists is True
        assert _repo(service_client).acquire_lease("worker-new2", 60, 100) is None

        # db_purged_at is preserved when it was already set.
        _run_sql(
            "UPDATE public.account_deletion_jobs SET db_purged_at = "
            "now() - interval '1 hour' "
            f"WHERE job_id = '{request.job_id}'"
        )
        _run_sql(
            "UPDATE public.account_deletion_jobs SET status = 'processing', "
            "lease_owner = 'worker-dead2', "
            "lease_expires_at = now() - interval '1 second' "
            f"WHERE job_id = '{request.job_id}'"
        )
        assert _repo(service_client).acquire_lease("worker-new3", 60, 100) is None
        row = _run_sql(
            "SELECT status, db_purged_at IS NOT NULL AS purged "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["status"] == "failed"
        assert row["purged"] is True, "terminalization must preserve db_purged_at"
    finally:
        _cleanup_user(user_id)


# ─── 14/15/16. Finalize and minimization ────────────────────────────────────

def test_finalize_clears_user_id_keeps_ref_and_tombstone(service_client: Client):
    user_id = _uid("final")
    _seed_profile(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        job = _repo(service_client).acquire_lease("worker-f", 60, 100)
        _repo(service_client).purge(job.job_id, "worker-f", fp)
        result = _repo(service_client).finalize(job.job_id, "worker-f")
        assert result.status == "completed"
        # Raw user_id minimized to NULL; the HMAC reference persists.
        row = _run_sql(
            "SELECT user_id, user_ref_hmac_sha256, status "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{job.job_id}'"
        )[0]
        assert row["user_id"] is None
        assert row["user_ref_hmac_sha256"] == ref
        assert row["status"] == "completed"
        # The tombstone stays queryable by the defined contract.
        tombstone = _repo(service_client).has_tombstone(ref)
        assert tombstone.exists is True
        assert tombstone.status == "completed"
        assert request.job_id == job.job_id
    finally:
        _cleanup_user(user_id)


# ─── 17. Retention ──────────────────────────────────────────────────────────


def test_active_and_failed_jobs_never_aged_out(service_client: Client):
    user_a = _uid("ret-a")
    user_b = _uid("ret-b")
    user_c = _uid("ret-c")
    ref_a = _ref(user_a)
    ref_b = _ref(user_b)
    ref_c = _ref(user_c)
    fp = _fingerprint()
    try:
        # A: completed (purged + finalized).
        req_a = _repo(service_client).request(user_a, ref_a, _new_op_id(), fp)
        job_a = _repo(service_client).acquire_lease("worker-rt", 60, 100)
        assert job_a.job_id == req_a.job_id
        _repo(service_client).purge(job_a.job_id, "worker-rt", fp)
        _repo(service_client).finalize(job_a.job_id, "worker-rt")
        # Age A's completed tombstone beyond the 30-day horizon.
        _run_sql(
            "UPDATE public.account_deletion_jobs SET completed_at = "
            "now() - interval '40 days', db_purged_at = now() - interval '40 days' "
            f"WHERE job_id = '{job_a.job_id}'"
        )
        # B: failed with an old next_attempt_at (never aged out).
        req_b = _repo(service_client).request(user_b, ref_b, _new_op_id(), fp)
        job_b = _repo(service_client).acquire_lease("worker-rt", 60, 100)
        assert job_b.job_id == req_b.job_id
        _repo(service_client).record_failure(job_b.job_id, "worker-rt", "auth_unavailable")
        _run_sql(
            "UPDATE public.account_deletion_jobs SET next_attempt_at = "
            "now() - interval '40 days', requested_at = now() - interval '40 days' "
            f"WHERE job_id = '{job_b.job_id}'"
        )
        # C: pending, never aged out.
        _repo(service_client).request(user_c, ref_c, _new_op_id(), fp)
        _run_sql(
            "UPDATE public.account_deletion_jobs SET next_attempt_at = "
            "now() - interval '40 days', requested_at = now() - interval '40 days' "
            f"WHERE user_ref_hmac_sha256 = '{ref_c}'"
        )

        # A future cutoff is clamped by the DB: only the aged completed
        # tombstone of A is removed; active/failed jobs survive.
        assert _repo(service_client).purge_completed(
            "2099-01-01T00:00:00+00:00", 100
        ) == 1
        assert _count("account_deletion_jobs", f"user_ref_hmac_sha256 = '{ref_b}'") == 1
        assert _count("account_deletion_jobs", f"user_ref_hmac_sha256 = '{ref_c}'") == 1
        # A second pass removes nothing.
        assert _repo(service_client).purge_completed(
            "2020-01-01T00:00:00+00:00", 100
        ) == 0
    finally:
        _cleanup_user(user_a)
        _cleanup_user(user_b)
        _cleanup_user(user_c)


# ─── 18/19. Concurrency against the per-user lock ───────────────────────────


def test_purge_serialized_with_same_user_writers(
    supabase_url: str, service_role_key: str
):
    """The purge holds the same per-user advisory lock as commit_turn and
    the privacy operations: a same-user writer (delete_history) that starts
    concurrently is serialized and both complete without corruption."""
    user_id = _uid("ser")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    try:
        client = create_client(supabase_url, service_role_key)
        request = _repo(client).request(user_id, ref, op_id, fp)
        _close_client(client)

        def _purge_worker():
            own_client = create_client(supabase_url, service_role_key)
            try:
                barrier.wait(timeout=30)
                job = _repo(own_client).acquire_lease("worker-ser", 60, 100)
                result = _repo(own_client).purge(job.job_id, "worker-ser", fp)
                assert result.status == "purged"
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                _close_client(own_client)

        def _writer_worker():
            own_client = create_client(supabase_url, service_role_key)
            try:
                barrier.wait(timeout=30)
                # delete_history acquires the SAME per-user advisory lock.
                _privacy_delete_history(own_client, user_id)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                _close_client(own_client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_purge_worker), pool.submit(_writer_worker)]
            for future in futures:
                future.result(timeout=120)

        assert errors == [], f"serialized purge/writer failed: {errors}"
        # Final state: user fully purged, tombstone intact.
        assert _count("profiles", f"user_id = '{user_id}'") == 0
        assert _count("account_deletion_jobs", f"user_ref_hmac_sha256 = '{ref}'") == 1
        assert request.job_id is not None
    finally:
        _cleanup_user(user_id)


def test_different_users_not_globally_blocked(
    supabase_url: str, service_role_key: str
):
    """A purge of A never blocks a same-operation writer of B: the per-user
    advisory lock is scoped per user, never global."""
    user_a = _uid("noblocka")
    user_b = _uid("noblockb")
    _seed_full_user(user_a)
    _seed_full_user(user_b)
    ref_a = _ref(user_a)
    op_a = _new_op_id()
    fp = _fingerprint()
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    try:
        client = create_client(supabase_url, service_role_key)
        _repo(client).request(user_a, ref_a, op_a, fp)
        _close_client(client)

        def _purge_a():
            own_client = create_client(supabase_url, service_role_key)
            try:
                barrier.wait(timeout=30)
                job = _repo(own_client).acquire_lease("worker-na", 60, 100)
                result = _repo(own_client).purge(job.job_id, "worker-na", fp)
                assert result.status == "purged"
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                _close_client(own_client)

        def _writer_b():
            own_client = create_client(supabase_url, service_role_key)
            try:
                barrier.wait(timeout=30)
                _privacy_delete_history(own_client, user_b)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                _close_client(own_client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_purge_a), pool.submit(_writer_b)]
            for future in futures:
                future.result(timeout=120)

        assert errors == [], f"cross-user concurrency failed: {errors}"
        # A fully purged; B's writer ran to completion and B's profile
        # still exists (delete_history preserves profiles).
        assert _count("profiles", f"user_id = '{user_a}'") == 0
        assert _count("profiles", f"user_id = '{user_b}'") == 1
    finally:
        _cleanup_user(user_a)
        _cleanup_user(user_b)


# ─── 20-30. Durable worker end to end (#325, real DB + FAKE Auth Admin) ─────


class _FakeAuthAdmin:
    """Fake Auth Admin boundary for integration tests (never touches Auth)."""

    def __init__(self, result: AuthDeleteResult) -> None:
        self.result = result
        self.calls: list[str] = []
        self.exc: BaseException | None = None

    def hard_delete(self, user_id: str) -> AuthDeleteResult:
        self.calls.append(user_id)
        if self.exc is not None:
            raise self.exc
        return self.result


def _it_worker(
    client: Client,
    auth: _FakeAuthAdmin,
    worker_id: str,
    *,
    lease_seconds: int = 60,
    max_batch: int = 5,
) -> AccountDeletionWorker:
    return AccountDeletionWorker(
        repository=SupabaseAccountDeletionRepository(client),
        auth_admin=auth,
        config=AccountDeletionWorkerConfig(
            worker_id=worker_id, lease_seconds=lease_seconds, max_batch=max_batch
        ),
    )


def test_worker_empty_queue_is_nominal_no_work(service_client: Client):
    """Real DB, no jobs: run_once returns no_work and never touches Auth."""
    auth = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_DELETED))
    result = _it_worker(service_client, auth, "it-worker-empty").run_once()
    assert result.no_work is True
    assert result.completed == 0
    assert auth.calls == []


def test_worker_db_first_end_to_end_preserves_b_and_minimizes(
    service_client: Client,
):
    """#325 scenario 21/22/23: worker processes A (DB purge BEFORE Auth),
    completes the job with user_id minimized, and B stays fully intact."""
    user_a = _uid("wrk-a")
    user_b = _uid("wrk-b")
    _seed_full_user(user_a)
    _seed_full_user(user_b)
    ref_a = _ref(user_a)
    ref_b = _ref(user_b)
    op_a = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_a, ref_a, op_a, fp)
        auth = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_DELETED))
        result = _it_worker(service_client, auth, "it-worker-e2e").run_once()
        assert result.completed == 1
        assert auth.calls == [user_a], "Auth must be called exactly once for A"
        # Every table of A is gone.
        for table in (
            "outbox_events",
            "turn_requests",
            "archival_extractions",
            "memories",
            "chat_logs",
            "admission_reservations",
            "privacy_operations",
            "profiles",
        ):
            assert _count(table, f"user_id = '{user_a}'") == 0, f"{table} not purged"
        # B is completely preserved.
        for table, expected in (
            ("outbox_events", 1),
            ("turn_requests", 1),
            ("archival_extractions", 1),
            ("memories", 1),
            ("chat_logs", 2),
            ("admission_reservations", 1),
            ("privacy_operations", 1),
            ("profiles", 1),
        ):
            assert _count(table, f"user_id = '{user_b}'") == expected, (
                f"{table} of B lost"
            )
        # Job completed, user_id minimized, HMAC ref and tombstone preserved.
        row = _run_sql(
            "SELECT status, user_id, user_ref_hmac_sha256 "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["status"] == "completed"
        assert row["user_id"] is None
        assert row["user_ref_hmac_sha256"] == ref_a
        assert _repo(service_client).has_tombstone(ref_a).exists is True
        assert _repo(service_client).has_tombstone(ref_b).exists is False
    finally:
        _cleanup_user(user_a)
        _cleanup_user(user_b)


def test_worker_auth_already_absent_is_idempotent_success(service_client: Client):
    user_id = _uid("wrk-absent")
    _seed_profile(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        auth = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_ALREADY_ABSENT))
        result = _it_worker(service_client, auth, "it-worker-absent").run_once()
        assert result.completed == 1
        row = _run_sql(
            "SELECT status, user_id FROM public.account_deletion_jobs "
            f"WHERE job_id = '{request.job_id}'"
        )[0]
        assert row["status"] == "completed"
        assert row["user_id"] is None
    finally:
        _cleanup_user(user_id)


def test_worker_next_attempt_at_gates_early_processing(service_client: Client):
    """#325 scenario 11: after a sanitized Auth failure the DB schedules
    next_attempt_at; an immediate poll finds no eligible job (no sleeps)."""
    user_id = _uid("wrk-gate")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        auth = _FakeAuthAdmin(
            AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE)
        )
        first = _it_worker(service_client, auth, "it-worker-gate1").run_once()
        assert first.retry_scheduled == 1
        # next_attempt_at is in the future: the job is not eligible yet.
        assert _repo(service_client).acquire_lease("it-worker-gate2", 60, 5) is None
        row = _run_sql(
            "SELECT status, db_purged_at IS NOT NULL AS purged "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["status"] == "pending"
        assert row["purged"] is True, "DB-first purge committed before the Auth retry"
        # Open the retry window deterministically, then the job is eligible.
        _run_sql(
            "UPDATE public.account_deletion_jobs SET next_attempt_at = "
            "now() - interval '1 second' "
            f"WHERE job_id = '{request.job_id}'"
        )
        auth2 = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_DELETED))
        second = _it_worker(service_client, auth2, "it-worker-gate3").run_once()
        assert second.completed == 1
        assert _repo(service_client).has_tombstone(ref).exists is True
    finally:
        _cleanup_user(user_id)


def test_worker_attempts_exhausted_not_reprocessed(service_client: Client):
    """#325 scenario 12: at the ceiling a failure is terminal and the worker
    never sees the job again (tombstone preserved, no automatic retry)."""
    user_id = _uid("wrk-exh")
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        _run_sql(
            "UPDATE public.account_deletion_jobs SET attempts = 99 "
            f"WHERE job_id = '{request.job_id}'"
        )
        auth = _FakeAuthAdmin(
            AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE)
        )
        result = _it_worker(service_client, auth, "it-worker-exh").run_once()
        assert result.failed == 1
        assert result.retry_scheduled == 0
        row = _run_sql(
            "SELECT status, error_code, next_attempt_at IS NULL AS terminal "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["status"] == "failed"
        assert row["error_code"] == "attempts_exhausted"
        assert row["terminal"] is True
        # Never claimed again automatically.
        assert _repo(service_client).acquire_lease("it-worker-exh2", 60, 5) is None
        assert _repo(service_client).has_tombstone(ref).exists is True
    finally:
        _cleanup_user(user_id)


def test_two_real_workers_never_process_same_job(
    supabase_url: str, service_role_key: str
):
    """#325 scenario 13: two workers on independent clients race; exactly one
    claims and completes the job, the other observes an empty queue."""
    user_id = _uid("wrk-race")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, bool]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    try:
        client = create_client(supabase_url, service_role_key)
        request = _repo(client).request(user_id, ref, op_id, fp)
        _close_client(client)

        def _run(worker_id: str, auth_result: AuthDeleteResult):
            own_client = create_client(supabase_url, service_role_key)
            try:
                barrier.wait(timeout=30)
                auth = _FakeAuthAdmin(auth_result)
                result = _it_worker(own_client, auth, worker_id).run_once()
                with lock:
                    outcomes.append((result.completed, result.no_work))
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                _close_client(own_client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_run, "it-worker-race-a", AuthDeleteResult(outcome=OUTCOME_DELETED)),
                pool.submit(_run, "it-worker-race-b", AuthDeleteResult(outcome=OUTCOME_DELETED)),
            ]
            for future in futures:
                future.result(timeout=120)

        assert errors == [], f"concurrent worker run failed: {errors}"
        assert sum(o[0] for o in outcomes) == 1, "exactly one worker completes the job"
        # The job is completed and the user data is gone.
        assert _count("profiles", f"user_id = '{user_id}'") == 0
        row = _run_sql(
            "SELECT status FROM public.account_deletion_jobs "
            f"WHERE user_ref_hmac_sha256 = '{ref}'"
        )[0]
        assert row["status"] == "completed"
        assert request.job_id is not None
    finally:
        _cleanup_user(user_id)


def test_worker_lease_lost_blocks_finalize_and_other_worker_recovers(
    service_client: Client, supabase_url: str, service_role_key: str
):
    """#325 scenarios 14/15: worker A loses its lease during external work and
    CANNOT finalize; worker B recovers, skips the destructive purge
    (db_purged_at authoritative) and completes safely."""
    user_id = _uid("wrk-lease")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        # Worker A: acquires and purges, then its lease expires while the
        # external Auth call would run. finalize must fail closed.
        client_a = create_client(supabase_url, service_role_key)
        job = _repo(client_a).acquire_lease("it-worker-a", 60, 5)
        _repo(client_a).purge(job.job_id, "it-worker-a", fp)
        _run_sql(
            "UPDATE public.account_deletion_jobs "
            "SET lease_expires_at = now() - interval '1 second' "
            f"WHERE job_id = '{job.job_id}'"
        )
        with pytest.raises(PersistenceError):
            _repo(client_a).finalize(job.job_id, "it-worker-a")
        _close_client(client_a)

        # Worker B on an independent client reclaims the job.
        client_b = create_client(supabase_url, service_role_key)
        auth_b = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_DELETED))
        result = _it_worker(client_b, auth_b, "it-worker-b").run_once()
        assert result.completed == 1
        assert auth_b.calls == [user_id]
        row = _run_sql(
            "SELECT status, user_id, db_purged_at IS NOT NULL AS purged "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["status"] == "completed"
        assert row["user_id"] is None
        assert row["purged"] is True
        assert _count("profiles", f"user_id = '{user_id}'") == 0
        _close_client(client_b)
    finally:
        _cleanup_user(user_id)


def test_worker_crash_after_purge_before_auth_is_reexecutable(
    service_client: Client, supabase_url: str, service_role_key: str
):
    """#325 scenario 16: worker A dies (KeyboardInterrupt) after the DB purge
    committed and before Auth; worker B recovers via db_purged_at without
    repeating the destructive purge."""
    user_id = _uid("wrk-crash")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        client_a = create_client(supabase_url, service_role_key)
        auth_a = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_DELETED))
        auth_a.exc = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            _it_worker(client_a, auth_a, "it-worker-crash-a").run_once()
        _close_client(client_a)
        # The purge committed: all tables are gone, the marker is durable.
        assert _count("profiles", f"user_id = '{user_id}'") == 0
        row = _run_sql(
            "SELECT db_purged_at IS NOT NULL AS purged, status "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["purged"] is True
        assert row["status"] == "processing"
        # The dead worker's lease expires; worker B can reclaim the job.
        _run_sql(
            "UPDATE public.account_deletion_jobs "
            "SET lease_expires_at = now() - interval '1 second' "
            f"WHERE job_id = '{request.job_id}'"
        )

        # Worker B recovers: purge is a safe replay (already_purged), Auth
        # delete runs, finalize completes.
        client_b = create_client(supabase_url, service_role_key)
        auth_b = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_DELETED))
        result = _it_worker(client_b, auth_b, "it-worker-crash-b").run_once()
        assert result.completed == 1
        assert auth_b.calls == [user_id]
        final_row = _run_sql(
            "SELECT status, user_id FROM public.account_deletion_jobs "
            f"WHERE job_id = '{request.job_id}'"
        )[0]
        assert final_row["status"] == "completed"
        assert final_row["user_id"] is None
        _close_client(client_b)
    finally:
        _cleanup_user(user_id)


def test_worker_auth_failure_preserves_db_purged_at_then_completes(
    service_client: Client,
):
    """#325 scenario 10: an Auth failure after the DB purge must preserve
    db_purged_at; the retry skips the purge and finalizes."""
    user_id = _uid("wrk-post")
    _seed_full_user(user_id)
    ref = _ref(user_id)
    op_id = _new_op_id()
    fp = _fingerprint()
    try:
        request = _repo(service_client).request(user_id, ref, op_id, fp)
        auth = _FakeAuthAdmin(
            AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE)
        )
        first = _it_worker(service_client, auth, "it-worker-post1").run_once()
        assert first.retry_scheduled == 1
        row = _run_sql(
            "SELECT status, db_purged_at IS NOT NULL AS purged "
            "FROM public.account_deletion_jobs WHERE job_id = "
            f"'{request.job_id}'"
        )[0]
        assert row["status"] == "pending"
        assert row["purged"] is True, "db_purged_at must survive an Auth retry"
        # Open the retry window; the next worker skips the destructive purge.
        _run_sql(
            "UPDATE public.account_deletion_jobs SET next_attempt_at = "
            "now() - interval '1 second' "
            f"WHERE job_id = '{request.job_id}'"
        )
        auth2 = _FakeAuthAdmin(AuthDeleteResult(outcome=OUTCOME_DELETED))
        second = _it_worker(service_client, auth2, "it-worker-post2").run_once()
        assert second.completed == 1
        assert _count("profiles", f"user_id = '{user_id}'") == 0
        assert _repo(service_client).has_tombstone(ref).exists is True
    finally:
        _cleanup_user(user_id)


# ─── #326 HTTP API: real job creation and tombstone gate ────────────────────
#
# End-to-end against the local Supabase instance: the authenticated HTTP
# endpoint creates a REAL job through the #324 contract, the tombstone gate
# blocks /history and /chat, the replay stays idempotent, and account B is
# not affected. The Auth Admin surface is never touched here (the worker is
# not part of these scenarios).


@pytest.fixture(scope="module")
def anon_key() -> str:
    return _required_env("SUPABASE_ANON_KEY")


@pytest.fixture(scope="module")
def app_client(
    supabase_url: str,
    service_role_key: str,
) -> tuple[TestClient, Client]:
    """The real FastAPI application wired to the local Supabase instance."""
    from backend.account_deletion_service import AccountDeletionService
    from backend.admission import AdmissionRuntimeConfig
    from backend.dependencies import ApplicationDependencies
    from backend.health import HealthRegistry
    from backend.main import create_app
    from backend.settings import AppEnvironment, Settings
    from backend.turn_execution import TurnExecutionConfig

    client = create_client(supabase_url, service_role_key)
    admission_config = AdmissionRuntimeConfig.from_values(SECRET.decode("utf-8"))
    account_deletion_service = AccountDeletionService(
        repository=SupabaseAccountDeletionRepository(client),
        turn_config=TurnExecutionConfig.defaults(),
        admission_config=admission_config,
    )
    settings = Settings(
        app_env=AppEnvironment.local,
        groq_api_key="ci-groq-key",
        admission_hmac_secret=SECRET.decode("utf-8"),
        cors_allowed_origins=("http://localhost:3000",),
    )
    engine = SimpleNamespace(
        memory_manager=SimpleNamespace(supabase=client),
        groq_manager=SimpleNamespace(is_configured=lambda: True),
    )
    deps = ApplicationDependencies(
        conversation_engine=engine,
        auth_client=client,
        admission_config=admission_config,
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
        clock=time.time,
        persistence_client=client,
        account_deletion_service=account_deletion_service,
    )
    app = create_app(settings=settings, dependencies=deps)
    yield TestClient(app), client
    _close_client(client)


@pytest.fixture(scope="module")
def users(
    supabase_url: str,
    anon_key: str,
    service_client: Client,
) -> dict[str, dict]:
    """Two real GoTrue users (``a`` and ``b``) with valid sessions."""
    created: list[dict] = []
    for label in ("a", "b"):
        email = f"adl-api-{label}-{uuid.uuid4().hex[:8]}@test.local"
        password = "password123"
        anon = create_client(supabase_url, anon_key)
        try:
            anon.auth.sign_up({"email": email, "password": password})
            response = anon.auth.sign_in_with_password(
                {"email": email, "password": password}
            )
            assert response is not None and response.user is not None
            assert (
                response.session is not None
                and response.session.access_token is not None
            )
            created.append(
                {
                    "label": label,
                    "id": response.user.id,
                    "token": response.session.access_token,
                    "email": email,
                }
            )
        finally:
            _close_client(anon)
    yield {entry["label"]: entry for entry in created}
    for entry in created:
        for user in service_client.auth.admin.list_users():
            if user.email == entry["email"]:
                service_client.auth.admin.delete_user(user.id)


def test_http_delete_account_creates_real_job_and_gate_blocks(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    """#326 scenario 29 (part 1): POST /privacy/delete-account creates a REAL
    #324 job from current_user.id alone; the tombstone then blocks /history
    and /chat; the replay is idempotent."""
    client, _ = app_client
    user = users["a"]
    user_b = users["b"]
    headers_a = {"Authorization": f"Bearer {user['token']}"}
    headers_b = {"Authorization": f"Bearer {user_b['token']}"}
    operation_id = _new_op_id()
    try:
        # Active user B can use /history before any tombstone exists.
        history_b = client.get("/history", headers=headers_b)
        assert history_b.status_code == 200

        # First request: honest accepted state, nothing internal.
        response = client.post(
            "/privacy/delete-account",
            json={"operation_id": operation_id},
            headers=headers_a,
        )
        assert response.status_code == 200
        assert response.json() == {"status": "accepted"}

        # A real job row exists, bound to the server-derived reference.
        rows = _run_sql(
            "SELECT job_id::text, user_ref_hmac_sha256, user_id, status "
            "FROM public.account_deletion_jobs "
            f"WHERE operation_id = '{operation_id}'"
        )
        assert len(rows) == 1
        assert rows[0]["user_ref_hmac_sha256"] == _ref(user["id"])
        assert rows[0]["user_id"] == user["id"]
        assert rows[0]["status"] == "pending"
        job_id = rows[0]["job_id"]

        # The tombstone gate blocks /history BEFORE any chat_logs read.
        history_a = client.get("/history", headers=headers_a)
        assert history_a.status_code == 423
        assert history_a.json() == {
            "detail": {
                "code": "account_deletion_pending",
                "message": "Account deletion is pending.",
            }
        }

        # The tombstone gate blocks /chat BEFORE admission.
        chat_a = client.post(
            "/chat",
            json={
                "request_id": str(uuid.uuid4()),
                "message": "hello",
            },
            headers=headers_a,
        )
        assert chat_a.status_code == 423
        assert chat_a.json()["detail"]["code"] == "account_deletion_pending"

        # Replay: same operation_id -> same honest state, no second job.
        replay = client.post(
            "/privacy/delete-account",
            json={"operation_id": operation_id},
            headers=headers_a,
        )
        assert replay.status_code == 200
        assert replay.json() == {"status": "accepted"}
        assert _count(
            "account_deletion_jobs", f"operation_id = '{operation_id}'"
        ) == 1

        # Account B is not affected: /history still works.
        history_b_after = client.get("/history", headers=headers_b)
        assert history_b_after.status_code == 200

        # The public response and the 423 payload never leak the job id.
        assert job_id not in replay.text
        assert job_id not in history_a.text
    finally:
        _cleanup_user(user["id"])
        _cleanup_user(user_b["id"])


def test_http_delete_account_rejects_spoofed_identity_before_rpc(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    """#326 scenario 29 (part 2): a body carrying user_id/user_ref/job_id is
    rejected with 422 and NO job is created (identity comes exclusively from
    the authenticated user)."""
    client, _ = app_client
    user = users["a"]
    headers = {"Authorization": f"Bearer {user['token']}"}
    for extra in (
        {"operation_id": _new_op_id(), "user_id": "attacker"},
        {"operation_id": _new_op_id(), "user_ref": "a" * 64},
        {"operation_id": _new_op_id(), "job_id": str(uuid.uuid4())},
    ):
        operation_id = extra["operation_id"]
        response = client.post(
            "/privacy/delete-account", json=extra, headers=headers
        )
        assert response.status_code == 422
        assert _count(
            "account_deletion_jobs", f"operation_id = '{operation_id}'"
        ) == 0, "no job may be created from a spoofed body"
