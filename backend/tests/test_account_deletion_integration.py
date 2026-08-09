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
from supabase import Client, create_client

from backend.account_deletion import (
    SupabaseAccountDeletionRepository,
    compute_account_deletion_user_ref,
    compute_intent_fingerprint,
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
                with lock:
                    winners.append(job.job_id)
            except PersistenceError:
                # No eligible job: the expected outcome for the loser.
                pass
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
        # A third claim while the lease is active finds no eligible job.
        with pytest.raises(PersistenceError):
            _repo(service_client).acquire_lease("worker-2", 60, 100)
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
        # A second claim while the lease is active finds nothing.
        with pytest.raises(PersistenceError):
            _repo(service_client).acquire_lease("worker-b", 60, 100)
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
        with pytest.raises(PersistenceError):
            _repo(service_client).acquire_lease("worker-ex2", 60, 100)
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
        with pytest.raises(PersistenceError):
            _repo(service_client).acquire_lease("worker-new", 60, 100)
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
        with pytest.raises(PersistenceError):
            _repo(service_client).acquire_lease("worker-new2", 60, 100)

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
        with pytest.raises(PersistenceError):
            _repo(service_client).acquire_lease("worker-new3", 60, 100)
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
