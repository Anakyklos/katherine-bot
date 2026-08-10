"""Unit tests for the #324 account deletion ledger Python contract.

Covers the pure-Python domain/adapter layer of ``backend.account_deletion``
without any network:

 1.  Valid domain results parse from well-formed RPC envelopes.
 2.  Invalid UUIDs, user_ids, HMACs and fingerprints are rejected.
 3.  Unknown statuses, extra fields, incomplete payloads and bools in
     integer fields fail closed.
 4.  RPC results with a divergent identity fail closed sanitized (the
     identity is never echoed in the exception).
 5.  Structured errors (validation/conflict) map to the right exceptions.
 6.  Malformed responses fail closed with PersistenceError.
 7.  Importing the module constructs no infrastructure and opens no
     sockets (same purity gate as the retention modules).
 8.  No secret or identifier ever appears in an exception message or in
     captured logs.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from backend.account_deletion import (
    ACCOUNT_DELETION_HMAC_DOMAIN,
    ERROR_OPERATION_CONFLICT,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    SupabaseAccountDeletionRepository,
    compute_account_deletion_user_ref,
    compute_intent_fingerprint,
)
from backend.atomic_turn_commit import (
    ConflictError,
    PersistenceError,
    ValidationError,
)

SECRET = b"ci-test-secret-0123456789abcdef0123456789abcdef"

USER_ID = "user-A-1234"
REF = "a" * 64
FINGERPRINT = "b" * 64
OPERATION_ID = "11111111-1111-1111-1111-111111111111"
JOB_ID = "22222222-2222-2222-2222-222222222222"


def _client(data) -> SupabaseAccountDeletionRepository:
    client = SimpleNamespace(
        rpc=lambda name, params: SimpleNamespace(execute=lambda: SimpleNamespace(data=data))
    )
    return SupabaseAccountDeletionRepository(client)


# ─── 1. Valid results parse ─────────────────────────────────────────────────


def test_request_created_parses():
    repo = _client(
        {
            "status": "created",
            "job_id": JOB_ID,
            "job_status": "pending",
            "db_purged_at": None,
            "completed_at": None,
        }
    )
    result = repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)
    assert result.status == "created"
    assert result.job_id == JOB_ID
    assert result.job_status == STATUS_PENDING


def test_request_replay_parses():
    repo = _client(
        {
            "status": "replay",
            "job_id": JOB_ID,
            "job_status": "completed",
            "db_purged_at": "2026-08-09T00:00:00+00:00",
            "completed_at": "2026-08-09T01:00:00+00:00",
        }
    )
    result = repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)
    assert result.status == "replay"
    assert result.job_status == STATUS_COMPLETED
    assert result.db_purged_at is not None


def test_absent_nullable_fields_parse_as_none():
    """PostgREST omits JSON null values: absent optional fields are None."""
    repo = _client(
        {
            "status": "created",
            "job_id": JOB_ID,
            "job_status": "pending",
        }
    )
    result = repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)
    assert result.status == "created"
    assert result.db_purged_at is None
    assert result.completed_at is None

    repo = _client({"exists": False})
    result = repo.has_tombstone(REF)
    assert result.exists is False
    assert result.status is None

    repo = _client(
        {
            "found": True,
            "job_id": JOB_ID,
            "user_id": USER_ID,
            "user_ref_hmac_sha256": REF,
            "operation_id": OPERATION_ID,
            "status": "processing",
            "lease_owner": "worker-1",
            "lease_expires_at": "2026-08-09T01:00:00+00:00",
            "attempts": 1,
            "intent_fingerprint_sha256": FINGERPRINT,
        }
    )
    job = repo.acquire_lease("worker-1", 60, 100)
    assert job.db_purged_at is None


def test_tombstone_parses():
    repo = _client({"exists": True, "status": "processing"})
    result = repo.has_tombstone(REF)
    assert result.exists is True
    assert result.status == STATUS_PROCESSING


def test_acquire_lease_parses():
    repo = _client(
        {
            "found": True,
            "job_id": JOB_ID,
            "user_id": USER_ID,
            "user_ref_hmac_sha256": REF,
            "operation_id": OPERATION_ID,
            "status": "processing",
            "lease_owner": "worker-1",
            "lease_expires_at": "2026-08-09T01:00:00+00:00",
            "attempts": 1,
            "db_purged_at": None,
            "intent_fingerprint_sha256": FINGERPRINT,
        }
    )
    job = repo.acquire_lease("worker-1", 60, 100)
    assert job.user_id == USER_ID
    assert job.attempts == 1
    assert job.status == STATUS_PROCESSING


def test_acquire_lease_empty_queue_is_nominal_none():
    """#325: the RPC ``found:false`` envelope is the NOMINAL empty-queue
    result. It returns ``None`` and never raises a persistence error."""
    repo = _client({"found": False})
    assert repo.acquire_lease("worker-1", 60, 100) is None


def test_acquire_lease_empty_queue_with_extra_fields_fails_closed():
    """A ``found:false`` envelope carrying extra fields is malformed: the
    empty-queue contract is exactly ``{"found": false}``, nothing else. An
    envelope that omits ``found`` entirely also fails closed."""
    for bad in (
        {},
        {"found": False, "sneaky": 1},
        {"found": False, "job_id": JOB_ID},
        {"found": "false"},
    ):
        repo = _client(bad)
        with pytest.raises(PersistenceError):
            repo.acquire_lease("worker-1", 60, 100)


def test_purge_parses_counts():
    repo = _client(
        {
            "status": "purged",
            "job_id": JOB_ID,
            "db_purged_at": "2026-08-09T00:00:00+00:00",
            "counts": {
                "outbox_events": 1,
                "turn_requests": 2,
                "archival_extractions": 3,
                "memories": 4,
                "chat_logs": 5,
                "admission_reservations": 6,
                "privacy_operations": 7,
                "profiles": 8,
            },
        }
    )
    result = repo.purge(JOB_ID, "worker-1", FINGERPRINT, expected_user_id=USER_ID)
    assert result.status == "purged"
    assert result.counts["profiles"] == 8


def test_already_purged_replay_parses():
    repo = _client(
        {
            "status": "already_purged",
            "job_id": JOB_ID,
            "db_purged_at": "2026-08-09T00:00:00+00:00",
            "counts": {
                "outbox_events": 0,
                "turn_requests": 0,
                "archival_extractions": 0,
                "memories": 0,
                "chat_logs": 0,
                "admission_reservations": 0,
                "privacy_operations": 0,
                "profiles": 0,
            },
        }
    )
    result = repo.purge(JOB_ID, "worker-1", FINGERPRINT)
    assert result.status == "already_purged"


def test_failure_and_retry_and_finalize_parse():
    repo = _client(
        {
            "status": "failed",
            "job_id": JOB_ID,
            "error_code": "auth_unavailable",
            "next_attempt_at": "2026-08-09T01:00:00+00:00",
        }
    )
    failure = repo.record_failure(JOB_ID, "worker-1", "auth_unavailable")
    assert failure.status == "failed"
    assert failure.error_code == "auth_unavailable"

    repo = _client(
        {
            "status": "retry_scheduled",
            "job_id": JOB_ID,
            "next_attempt_at": "2026-08-09T01:00:00+00:00",
        }
    )
    retry = repo.record_retry(JOB_ID, "worker-1")
    assert retry.status == "retry_scheduled"

    repo = _client(
        {
            "status": "completed",
            "job_id": JOB_ID,
            "completed_at": "2026-08-09T01:00:00+00:00",
            "db_purged_at": "2026-08-09T00:00:00+00:00",
        }
    )
    finalize = repo.finalize(JOB_ID, "worker-1")
    assert finalize.status == "completed"


def test_exhausted_retry_parses_as_terminal_failed():
    """record_retry at the attempts ceiling returns a terminal failed job
    (error_code attempts_exhausted, next_attempt_at absent/NULL)."""
    repo = _client(
        {
            "status": "failed",
            "job_id": JOB_ID,
            "error_code": "attempts_exhausted",
        }
    )
    result = repo.record_retry(JOB_ID, "worker-1")
    assert result.status == "failed"
    assert result.next_attempt_at is None


def test_purge_completed_int_parses():
    repo = _client(3)
    assert repo.purge_completed("2026-01-01T00:00:00+00:00", 10) == 3


# ─── 2/3. HMAC and fingerprint domain helpers ───────────────────────────────


def test_user_ref_hmac_is_lowercase_hex64_with_dedicated_domain():
    ref = compute_account_deletion_user_ref(SECRET, USER_ID)
    assert len(ref) == 64
    assert all(c in "0123456789abcdef" for c in ref)
    # Stable across calls, dedicated domain (never the user-reference domain).
    assert ref == compute_account_deletion_user_ref(SECRET, USER_ID)
    import hmac
    import hashlib

    expected = hmac.new(
        SECRET,
        ACCOUNT_DELETION_HMAC_DOMAIN + b"\x00" + USER_ID.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert ref == expected
    assert ref != hmac.new(
        SECRET, b"user-reference\x00" + USER_ID.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def test_user_ref_rejects_invalid_user_id():
    with pytest.raises(ValidationError):
        compute_account_deletion_user_ref(SECRET, "")
    with pytest.raises(ValidationError):
        compute_account_deletion_user_ref(SECRET, "   ")
    with pytest.raises(ValidationError):
        compute_account_deletion_user_ref(SECRET, 123)


def test_user_ref_rejects_invalid_secret():
    with pytest.raises(Exception):
        compute_account_deletion_user_ref(b"short", USER_ID)


def test_intent_fingerprint_is_stable_and_object_only():
    payload = {"op": "delete_account", "scope": ["db", "auth"]}
    fp = compute_intent_fingerprint(payload)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
    assert fp == compute_intent_fingerprint({"op": "delete_account", "scope": ["db", "auth"]})
    with pytest.raises(ValidationError):
        compute_intent_fingerprint(["not", "an", "object"])
    with pytest.raises(ValidationError):
        compute_intent_fingerprint("text")


# ─── 4/5/6/7. Strict parsers fail closed ────────────────────────────────────


def test_invalid_uuid_in_job_id_fails_closed():
    repo = _client(
        {
            "status": "created",
            "job_id": "NOT-A-UUID",
            "job_status": "pending",
            "db_purged_at": None,
            "completed_at": None,
        }
    )
    with pytest.raises(PersistenceError):
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


def test_invalid_hmac_in_response_fails_closed():
    repo = _client(
        {
            "found": True,
            "job_id": JOB_ID,
            "user_id": USER_ID,
            "user_ref_hmac_sha256": "XYZ",
            "operation_id": OPERATION_ID,
            "status": "processing",
            "lease_owner": "worker-1",
            "lease_expires_at": "2026-08-09T01:00:00+00:00",
            "attempts": 1,
            "db_purged_at": None,
            "intent_fingerprint_sha256": FINGERPRINT,
        }
    )
    with pytest.raises(PersistenceError):
        repo.acquire_lease("worker-1", 60, 100)


def test_unknown_status_fails_closed():
    repo = _client(
        {
            "status": "created",
            "job_id": JOB_ID,
            "job_status": "exploded",
            "db_purged_at": None,
            "completed_at": None,
        }
    )
    with pytest.raises(PersistenceError):
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


def test_extra_fields_fail_closed():
    repo = _client(
        {
            "status": "created",
            "job_id": JOB_ID,
            "job_status": "pending",
            "db_purged_at": None,
            "completed_at": None,
            "sneaky": "field",
        }
    )
    with pytest.raises(PersistenceError):
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


def test_incomplete_payload_fails_closed():
    repo = _client({"status": "created", "job_id": JOB_ID})
    with pytest.raises(PersistenceError):
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


def test_bool_in_int_field_fails_closed():
    repo = _client(
        {
            "found": True,
            "job_id": JOB_ID,
            "user_id": USER_ID,
            "user_ref_hmac_sha256": REF,
            "operation_id": OPERATION_ID,
            "status": "processing",
            "lease_owner": "worker-1",
            "lease_expires_at": "2026-08-09T01:00:00+00:00",
            "attempts": True,
            "db_purged_at": None,
            "intent_fingerprint_sha256": FINGERPRINT,
        }
    )
    with pytest.raises(PersistenceError):
        repo.acquire_lease("worker-1", 60, 100)


def test_bool_int_purge_completed_fails_closed():
    repo = _client(True)
    with pytest.raises(PersistenceError):
        repo.purge_completed("2026-01-01T00:00:00+00:00", 10)
    repo = _client(-1)
    with pytest.raises(PersistenceError):
        repo.purge_completed("2026-01-01T00:00:00+00:00", 10)


def test_divergent_identity_fails_closed_without_echo():
    repo = _client(
        {
            "found": True,
            "job_id": JOB_ID,
            "user_id": "some-other-user",
            "user_ref_hmac_sha256": REF,
            "operation_id": OPERATION_ID,
            "status": "processing",
            "lease_owner": "worker-1",
            "lease_expires_at": "2026-08-09T01:00:00+00:00",
            "attempts": 1,
            "db_purged_at": None,
            "intent_fingerprint_sha256": FINGERPRINT,
        }
    )
    with pytest.raises(PersistenceError) as exc:
        repo.acquire_lease("worker-1", 60, 100, expected_user_id=USER_ID)
    assert "some-other-user" not in str(exc.value)
    assert USER_ID not in str(exc.value)


# ─── 10. Structured errors map correctly ────────────────────────────────────


def test_operation_conflict_raises_conflict_error():
    repo = _client(
        {
            "error": {
                "code": ERROR_OPERATION_CONFLICT,
                "message": "operation_id already used with a different intent",
            }
        }
    )
    with pytest.raises(ConflictError):
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


def test_validation_error_raises_validation_error():
    repo = _client(
        {
            "error": {
                "code": "validation_failed",
                "message": "user_ref_hmac_sha256 must be 64 lowercase hex characters",
            }
        }
    )
    with pytest.raises(ValidationError):
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


def test_malformed_response_fails_closed():
    for bad in ("not-a-mapping", 42, None, [], {"error": "not-an-object"}, {"error": {}}):
        repo = _client(bad)
        with pytest.raises(PersistenceError):
            repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


def test_upstream_exception_maps_to_sanitized_persistence_error():
    class ExplodingClient:
        def rpc(self, name, params):
            raise RuntimeError("SENTINEL-UPSTREAM-SECRET")

    repo = SupabaseAccountDeletionRepository(ExplodingClient())
    with pytest.raises(PersistenceError) as exc:
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)
    assert "SENTINEL-UPSTREAM-SECRET" not in str(exc.value)


def test_repository_without_client_fails_closed():
    repo = SupabaseAccountDeletionRepository(None)
    with pytest.raises(PersistenceError):
        repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)


# ─── 12. No secret or identifier in exceptions/logs ─────────────────────────


def test_exceptions_never_echo_secrets_or_identities(caplog):
    with caplog.at_level(logging.INFO):
        repo = _client(
            {
                "status": "created",
                "job_id": JOB_ID,
                "job_status": "exploded",
                "db_purged_at": None,
                "completed_at": None,
            }
        )
        with pytest.raises(PersistenceError):
            repo.request(USER_ID, REF, OPERATION_ID, FINGERPRINT)
    for sentinel in (SECRET.decode(), USER_ID, REF, FINGERPRINT, OPERATION_ID, JOB_ID):
        assert sentinel not in caplog.text


# ─── 13. Pure importability ─────────────────────────────────────────────────

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

    import backend.account_deletion

    threads_after = len(threading.enumerate())

    assert threads_after == threads_before, "import started a thread"
    print("ACCOUNT_DELETION_PURITY_OK")
    """
)


def test_account_deletion_import_is_pure():
    result = subprocess.run(
        [sys.executable, "-c", _PURITY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "ACCOUNT_DELETION_PURITY_OK" in result.stdout
