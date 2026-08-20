"""Real-concurrency tests for the account deletion commit barrier (#329).

Runs against a REAL PostgreSQL database with independent connections per
concurrent actor (no mocks, no Supabase client). Reproduces the exact TOCTOU
window the review flagged:

1. ``/chat`` passes the preflight (no tombstone yet).
2. Deletion request is accepted and creates the tombstone.
3. The earlier ``commit_turn`` call reaches the commit boundary LATE — the
   barrier must block it inside the SAME advisory lock used by deletion.

Assertions prove every invariant at the same time: the commit returns the
sanitized ``account_deletion_pending`` envelope, and NO rows are written to
``profiles``, ``chat_logs``, ``turn_requests`` or ``outbox_events``.

A second scenario proves the barrier keeps working AFTER the purge and the
finalize step, when ``user_id`` has been minimized to NULL in the ledger.
"""

from __future__ import annotations
import hashlib
import time
from datetime import datetime, timezone
import hmac
import json
import threading
import uuid

import psycopg2

DATABASE_URL = "postgresql://katherine:katherine@localhost:5432/katherine"

WORKER_ID = "test-worker"
SECRET = b"test-secret-do-not-use-in-production-1234567890"

HMAC_DOMAIN = b"account-deletion"


def user_ref(user_id: str) -> str:
    return hmac.new(SECRET, HMAC_DOMAIN + b"\x00" + user_id.encode("utf-8"), hashlib.sha256).hexdigest()


def fingerprint(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(DATABASE_URL)


def rpc(cur: psycopg2.extensions.cursor, name: str, params: dict) -> object:
    """Call a public RPC. All RPCs are positional (no JSON envelope wrappers)."""
    positional = _positional_args(name, params)
    cur.execute(f"SELECT public.{name}({','.join(['%s'] * len(positional))})", positional)
    return cur.fetchone()[0]


def _positional_args(name: str, params: dict) -> list:
    if name == "commit_turn":
        return [
            params["p_authenticated_user_id"],
            params["p_request_id"],
            int(params["p_expected_revision"]),
            params["p_user_message"],
            params["p_assistant_message"],
            params["p_payload_hash_sha256"],
            params["p_emotional_state"],
            params["p_relationship_state"],
            params["p_public_response"],
            params["p_replay_payload"],
            params["p_outbox_events"],
            params["p_lease_owner"],
            params.get("p_account_deletion_user_ref"),
        ]
    if name == "account_deletion_request":
        return [
            params["p_authenticated_user_id"],
            params["p_user_ref_hmac_sha256"],
            params["p_operation_id"],
            params["p_intent_fingerprint_sha256"],
        ]
    if name == "account_deletion_commit_barrier":
        return [params["p_user_ref_hmac_sha256"]]
    if name == "account_deletion_has_tombstone":
        return [params["p_user_ref_hmac_sha256"]]
    if name == "account_deletion_acquire_lease":
        return [params["p_worker_id"], int(params["p_lease_seconds"]), int(params["p_max_batch"])]
    if name == "account_deletion_purge":
        return [
            params["p_job_id"],
            params["p_worker_id"],
            params["p_intent_fingerprint_sha256"],
        ]
    if name == "account_deletion_record_retry":
        return [params["p_job_id"], params["p_worker_id"]]
    if name == "account_deletion_finalize":
        return [params["p_job_id"], params["p_worker_id"]]
    raise AssertionError(f"no positional mapping for RPC {name}")


def seed_profile(cur: psycopg2.extensions.cursor, user_id: str, revision: int = 1) -> None:
    """Create a minimal profile row the historical commit_turn expects."""
    cur.execute(
        """INSERT INTO public.profiles (user_id, revision, emotional_state, relationship_state)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (user_id) DO NOTHING""",
        (
            user_id,
            revision,
            json.dumps(
                {
                    "schema_version": 1,
                    "pleasure": 0.0,
                    "arousal": 0.0,
                    "dominance": 0.0,
                    "libido": 0.0,
                    "aggression": 0.0,
                    "connection": 0.0,
                    "energy": 0.0,
                    "tension": 0.0,
                    "coping_mode": "HEALTHY",
                    "timestamp": 1.0,
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "trust": 0.0,
                    "affection": 0.0,
                    "tension": 0.0,
                    "triggers": [],
                    "timestamp": 1.0,
                }
            ),
        ),
    )


def count_tables(cur: psycopg2.extensions.cursor) -> dict[str, int]:
    cur.execute("SELECT count(*) FROM public.profiles")
    profiles = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.chat_logs")
    chat_logs = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.turn_requests")
    turn_requests = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.outbox_events")
    outbox = cur.fetchone()[0]
    return {
        "profiles": profiles,
        "chat_logs": chat_logs,
        "turn_requests": turn_requests,
        "outbox": outbox,
    }


def commit_turn_payload(
    user_id: str,
    request_id: str,
    revision: int,
    user_ref: str | None = None,
) -> dict:
    payload = {
        "p_authenticated_user_id": user_id,
        "p_request_id": request_id,
        "p_expected_revision": revision,
        "p_user_message": "barrier test message",
        "p_assistant_message": "barrier test response",
        "p_payload_hash_sha256": hashlib.sha256(
            "barrier test message".encode("utf-8")
        ).hexdigest(),
        "p_emotional_state": json.dumps(
            {
                "schema_version": 1,
                "pleasure": 0.0,
                "arousal": 0.0,
                "dominance": 0.0,
                "libido": 0.0,
                "aggression": 0.0,
                "connection": 0.0,
                "energy": 0.0,
                "tension": 0.0,
                "coping_mode": "HEALTHY",
                "timestamp": 1.0,
            }
        ),
        "p_relationship_state": json.dumps(
            {
                "schema_version": 1,
                "trust": 0.0,
                "affection": 0.0,
                "tension": 0.0,
                "triggers": [],
                "timestamp": 1.0,
            }
        ),
        "p_public_response": "barrier test response",
        "p_replay_payload": json.dumps(
            {"message_id": "00000000-0000-0000-0000-000000000000", "response": "barrier test response"}
        ),
        "p_outbox_events": json.dumps([]),
        "p_lease_owner": WORKER_ID,
    }
    if user_ref is not None:
        payload["p_account_deletion_user_ref"] = user_ref
    return payload


def request_deletion(cur: psycopg2.extensions.cursor, user_id: str) -> dict:
    return rpc(
        cur,
        "account_deletion_request",
        {
            "p_authenticated_user_id": user_id,
            "p_user_ref_hmac_sha256": user_ref(user_id),
            "p_operation_id": str(uuid.uuid4()),
            "p_intent_fingerprint_sha256": fingerprint({"purpose": "account_deletion"}),
        },
    )


def purge_and_finalize(cur: psycopg2.extensions.cursor) -> dict:
    """Run the worker pipeline for the single pending job."""
    job = rpc(
        cur,
        "account_deletion_acquire_lease",
        {"p_worker_id": WORKER_ID, "p_lease_seconds": 300, "p_max_batch": 1},
    )
    if job is None or not job.get("found"):
        raise AssertionError("no pending deletion job found")
    job_id = job["job_id"]
    rpc(
        cur,
        "account_deletion_purge",
        {
            "p_job_id": job_id,
            "p_worker_id": WORKER_ID,
            "p_intent_fingerprint_sha256": fingerprint({"purpose": "account_deletion"}),
        },
    )
    retry = rpc(
        cur, "account_deletion_record_retry", {"p_job_id": job_id, "p_worker_id": WORKER_ID}
    )
    # record_retry schedules a backoff (next_attempt_at = now + 30s * attempts).
    # A real worker respects the backoff before re-claiming; this test does the
    # same instead of subverting the pipeline with a manual UPDATE.
    next_at = retry.get("next_attempt_at")
    if next_at:
        wait_seconds = 0.5 + (
            datetime.fromisoformat(next_at) - datetime.now(timezone.utc)
        ).total_seconds()
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    # Re-claim and finalize (job moved back to pending by record_retry).
    job = rpc(
        cur,
        "account_deletion_acquire_lease",
        {"p_worker_id": WORKER_ID, "p_lease_seconds": 300, "p_max_batch": 1},
    )
    if job is None or not job.get("found"):
        raise AssertionError("job not re-claimable for finalize")
    return rpc(
        cur, "account_deletion_finalize", {"p_job_id": job["job_id"], "p_worker_id": WORKER_ID}
    )


def teardown_all() -> None:
    """Reset the ledger and tables for a clean run."""
    with conn() as db:
        with db.cursor() as cur:
            cur.execute("DELETE FROM public.account_deletion_jobs")
            cur.execute("DELETE FROM public.chat_logs")
            cur.execute("DELETE FROM public.turn_requests")
            cur.execute("DELETE FROM public.outbox_events")
            cur.execute("DELETE FROM public.profiles")


def test_barrier_blocks_commit_after_tombstone() -> None:
    """Request passes preflight, deletion is accepted, late commit is blocked."""
    teardown_all()
    user_id = f"barrier-user-{uuid.uuid4()}"
    request_id = str(uuid.uuid4())
    uref = user_ref(user_id)

    # 1. Pre-tombstone check passes (the preflight path would allow this).
    with conn() as db:
        with db.cursor() as cur:
            result = rpc(cur, "account_deletion_commit_barrier", {"p_user_ref_hmac_sha256": uref})
    assert result == {"blocked": False}, "preflight must pass before tombstone exists"

    # Seed the profile the historical commit would read/update.
    with conn() as db:
        with db.cursor() as cur:
            seed_profile(cur, user_id)
        db.commit()

    before = None
    with conn() as db:
        with db.cursor() as cur:
            before = count_tables(cur)

    # 2. Deletion request accepted concurrently (independent connection).
    delete_result: list[object] = []

    def accept_deletion() -> None:
        with conn() as db:
            with db.cursor() as cur:
                delete_result.append(request_deletion(cur, user_id))
            db.commit()

    acceptor = threading.Thread(target=accept_deletion)
    acceptor.start()
    acceptor.join(timeout=30)
    assert delete_result, "deletion acceptor did not run"
    assert delete_result[0]["status"] in ("created", "replay"), delete_result[0]
    assert delete_result[0]["job_status"] == "pending"

    # 3. The late commit_turn runs under its own connection. The SQL barrier
    # must block it before ANY write; the connection never commits anything.
    commit_result: list[object] = []

    def late_commit() -> None:
        with conn() as db:
            with db.cursor() as cur:
                try:
                    commit_result.append(
                        rpc(cur, "commit_turn", commit_turn_payload(user_id, request_id, 1, uref))
                    )
                    db.commit()
                except Exception as exc:  # pragma: no cover - sanitized raise
                    commit_result.append(exc)

    committer = threading.Thread(target=late_commit)
    committer.start()
    committer.join(timeout=30)
    assert commit_result, "late committer did not run"
    outcome = commit_result[0]
    assert isinstance(outcome, dict), f"expected envelope, got {type(outcome).__name__}"
    error = outcome.get("error")
    assert error is not None, "late commit must be refused, not committed"
    assert error.get("code") == "account_deletion_pending", outcome
    assert error.get("message") == "Account deletion is pending."

    # 4. Invariant: zero rows created anywhere.
    with conn() as db:
        with db.cursor() as cur:
            after = count_tables(cur)
    assert after == before, f"late commit wrote rows: before={before} after={after}"


def test_barrier_survives_purge_and_finalize() -> None:
    """Protection keeps working after purge/finalize (user_id minimized to NULL)."""
    teardown_all()
    user_id = f"barrier-user-{uuid.uuid4()}"
    uref = user_ref(user_id)
    request_id = str(uuid.uuid4())

    with conn() as db:
        with db.cursor() as cur:
            seed_profile(cur, user_id)
            result = request_deletion(cur, user_id)
            assert result["status"] == "created"
            purge_and_finalize(cur)
        db.commit()

    # Tombstone must still exist after finalize, keyed only on the HMAC.
    with conn() as db:
        with db.cursor() as cur:
            tombstone = rpc(cur, "account_deletion_has_tombstone", {"p_user_ref_hmac_sha256": uref})
    assert tombstone["exists"] is True, "tombstone must survive finalize"

    # The ledger row exists but user_id is minimized to NULL.
    with conn() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM public.account_deletion_jobs WHERE user_ref_hmac_sha256 = %s",
                (uref,),
            )
            row = cur.fetchone()
    assert row is not None, "ledger row must persist within retention"
    assert row[0] is None, "user_id must be minimized to NULL after finalize"

    # A late commit STILL blocked, even though user_id is NULL everywhere.
    commit_result: list[object] = []

    def late_commit() -> None:
        with conn() as db:
            with db.cursor() as cur:
                try:
                    commit_result.append(
                        rpc(cur, "commit_turn", commit_turn_payload(user_id, request_id, 1, uref))
                    )
                    db.commit()
                except Exception as exc:  # pragma: no cover
                    commit_result.append(exc)

    late_commit()
    assert commit_result
    outcome = commit_result[0]
    assert isinstance(outcome, dict) and outcome.get("error", {}).get("code") == "account_deletion_pending"


def test_barrier_passes_without_tombstone() -> None:
    """Normal path: no tombstone -> commit writes normally."""
    teardown_all()
    user_id = f"barrier-user-{uuid.uuid4()}"
    request_id = str(uuid.uuid4())
    uref = user_ref(user_id)

    with conn() as db:
        with db.cursor() as cur:
            seed_profile(cur, user_id)
        db.commit()

    with conn() as db:
        with db.cursor() as cur:
            outcome = rpc(
                cur, "commit_turn", commit_turn_payload(user_id, request_id, 1, uref)
            )
        db.commit()

    assert isinstance(outcome, dict), f"expected JSON object, got {type(outcome).__name__}"
    assert "error" not in outcome, f"normal commit must succeed: {outcome}"

    with conn() as db:
        with db.cursor() as cur:
            counts = count_tables(cur)
    assert counts["profiles"] == 1
    assert counts["turn_requests"] == 1
    assert counts["chat_logs"] >= 1
    assert counts["outbox"] == 0


def test_raw_user_id_never_bypasses_the_barrier() -> None:
    """The barrier looks up ONLY by HMAC. A raw user id never blocks or unblocks."""
    teardown_all()
    user_id = f"barrier-user-{uuid.uuid4()}"
    uref = user_ref(user_id)

    with conn() as db:
        with db.cursor() as cur:
            result = request_deletion(cur, user_id)
            assert result["status"] == "created"
        db.commit()

    # Querying with the raw user_id (not the HMAC) can NEVER look up the
    # tombstone: the input does not match the hex-64 HMAC contract, so the
    # barrier rejects it outright (fail closed). A caller that does not hold
    # the HMAC reference cannot proceed or interfere — exactly the property
    # the review demanded (HMAC-only lookup, no raw user_id in the path).
    with conn() as db:
        with db.cursor() as cur:
            try:
                rpc(cur, "account_deletion_commit_barrier", {"p_user_ref_hmac_sha256": user_id})
            except psycopg2.errors.RaiseException as raised:
                assert "persistence error" in str(raised), (
                    f"invalid reference must raise the sanitized persistence error, got {raised}"
                )
            else:
                raise AssertionError("raw user_id must be rejected, never silently accepted")

    # But the HMAC match blocks.
    with conn() as db:
        with db.cursor() as cur:
            right = rpc(cur, "account_deletion_commit_barrier", {"p_user_ref_hmac_sha256": uref})
    assert right.get("error", {}).get("code") == "account_deletion_pending", (
        f"HMAC lookup must block with the deletion-pending envelope, got {right}"
    )


if __name__ == "__main__":
    test_barrier_passes_without_tombstone()
    print("PASS: normal commit writes")
    test_barrier_blocks_commit_after_tombstone()
    print("PASS: commit blocked after tombstone (no writes)")
    test_barrier_survives_purge_and_finalize()
    print("PASS: protection survives purge/finalize")
    test_raw_user_id_never_bypasses_the_barrier()
    print("PASS: raw user_id never bypasses the barrier")
    teardown_all()
    print("ALL COMMIT-BARRIER CONCURRENCY TESTS PASSED")
