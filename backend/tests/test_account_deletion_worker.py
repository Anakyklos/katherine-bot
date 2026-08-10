"""Unit tests for the #325 durable account deletion worker and CLI.

Everything here runs without network: the Auth Admin boundary is always a
fake and the repository is a scriptable in-memory fake. The real Supabase
Auth adapter is exercised against fake admin APIs that raise the structured
exceptions of the installed SDK (``AuthApiError``/``AuthRetryableError``/
``AuthUnknownError``) so classification is tested against the real types.

Mandatory scenarios covered (from issue #325):

 1.  ``found:false`` is "no work", never an error.
 2.  CLI ``--once`` with no jobs terminates deterministically (exit 0).
 3.  DB purge happens BEFORE Auth.
 4.  Auth is never called when the purge fails.
 5.  ``db_purged_at`` present skips destructive DB mutations.
 6.  Auth success calls ``finalize``.
 7.  Auth already-absent (structured) is idempotent success.
 8.  "not found" is NEVER detected by exception text.
 9.  Transient Auth failure records a sanitized retry.
10.  ``db_purged_at`` is preserved across Auth failures/retries.
11.  ``next_attempt_at`` gating never requires worker sleeps.
12.  Attempts exhausted is not reprocessed automatically.
13.  Two workers with independent clients never process the same job.
14.  A lost lease prevents the old worker from finalizing.
15.  Recovery after a lost lease stays safe.
16.  Crash after purge, before Auth, is re-executable.
17.  Cancellation abandons no thread/task and leaves a recoverable state.
18.  No singleton/global holds per-user state.
19.  Importing the worker/CLI builds no Auth/Supabase/Groq/embeddings.
20.  Fakes are sufficient for the entire unit suite.
24.  Logs never contain user_id/HMAC/operation_id/job_id/email/token or an
     upstream exception marker.

Real-Supabase integration scenarios (21-23) live in
``test_account_deletion_integration.py``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.account_deletion import (
    AcquiredJob,
    FinalizeResult,
    FailureResult,
    PurgeResult,
    RetryResult,
)
from backend.account_deletion_worker import (
    ERROR_AUTH_FAILED,
    ERROR_AUTH_FORBIDDEN,
    ERROR_AUTH_UNAVAILABLE,
    OUTCOME_ALREADY_ABSENT,
    OUTCOME_AUTH_ERROR,
    OUTCOME_DELETED,
    AccountDeletionWorker,
    AccountDeletionWorkerConfig,
    AuthDeleteResult,
    SupabaseAccountDeletionAuthAdmin,
)
import backend.account_deletion_cli as cli_module
from backend.account_deletion_cli import (
    AccountDeletionRuntimeConfig,
    main as cli_main,
)
from backend.atomic_turn_commit import PersistenceError
from supabase_auth.errors import (
    AuthApiError,
    AuthRetryableError,
    AuthUnknownError,
)

SECRET_SENTINEL = "SENTINEL-UPSTREAM-SECRET"
USER_ID = "user-A-1234"
USER_ID_B = "user-B-5678"
JOB_ID = "22222222-2222-2222-2222-222222222222"
JOB_ID_B = "33333333-3333-3333-3333-333333333333"
REF = "a" * 64
FINGERPRINT = "b" * 64
OPERATION_ID = "11111111-1111-1111-1111-111111111111"
EMAIL = "user-a@example.com"
ACCESS_TOKEN = "fake-access-token"
DB_PURGED_AT = "2026-08-09T00:00:00+00:00"


def _job(
    *,
    job_id: str = JOB_ID,
    user_id: str = USER_ID,
    db_purged_at: object = None,
    attempts: int = 1,
) -> AcquiredJob:
    return AcquiredJob(
        job_id=job_id,
        user_id=user_id,
        user_ref_hmac_sha256=REF,
        operation_id=OPERATION_ID,
        status="processing",
        lease_owner="worker-1",
        lease_expires_at="2026-08-09T01:00:00+00:00",
        attempts=attempts,
        db_purged_at=db_purged_at,
        intent_fingerprint_sha256=FINGERPRINT,
    )


# ─── Fakes ──────────────────────────────────────────────────────────────────


class FakeRepository:
    """Scriptable in-memory repository recording the call order."""

    def __init__(self, jobs: list[AcquiredJob] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.calls: list[tuple] = []
        self.recorded_failures: list[str] = []
        self.recorded_retries: list[str] = []
        self.acquire_error: BaseException | None = None
        self.purge_error: BaseException | None = None
        self.finalize_error: BaseException | None = None
        self.record_failure_error: BaseException | None = None
        self.record_retry_error: BaseException | None = None

    def acquire_lease(self, worker_id, lease_seconds, max_batch):
        self.calls.append(("acquire_lease",))
        if self.acquire_error is not None:
            raise self.acquire_error
        if not self.jobs:
            return None
        return self.jobs.pop(0)

    def purge(self, job_id, worker_id, intent_fingerprint_sha256, expected_user_id=None):
        self.calls.append(("purge", job_id))
        if self.purge_error is not None:
            raise self.purge_error
        return PurgeResult(
            status="purged",
            job_id=job_id,
            db_purged_at=DB_PURGED_AT,
            counts={
                "outbox_events": 0,
                "turn_requests": 0,
                "archival_extractions": 0,
                "memories": 0,
                "chat_logs": 0,
                "admission_reservations": 0,
                "privacy_operations": 0,
                "profiles": 0,
            },
        )

    def record_failure(self, job_id, worker_id, error_code):
        self.calls.append(("record_failure", job_id, error_code))
        if self.record_failure_error is not None:
            raise self.record_failure_error
        self.recorded_failures.append(error_code)
        return FailureResult(
            status="failed",
            job_id=job_id,
            error_code=error_code,
            next_attempt_at="2026-08-09T01:00:00+00:00",
        )

    def record_retry(self, job_id, worker_id):
        self.calls.append(("record_retry", job_id))
        if self.record_retry_error is not None:
            raise self.record_retry_error
        self.recorded_retries.append(job_id)
        return RetryResult(
            status="retry_scheduled",
            job_id=job_id,
            next_attempt_at="2026-08-09T01:00:00+00:00",
        )

    def finalize(self, job_id, worker_id):
        self.calls.append(("finalize", job_id))
        if self.finalize_error is not None:
            raise self.finalize_error
        return FinalizeResult(
            status="completed",
            job_id=job_id,
            completed_at="2026-08-09T02:00:00+00:00",
            db_purged_at=DB_PURGED_AT,
        )


class FakeAuthAdmin:
    """Scriptable fake Auth Admin boundary."""

    def __init__(self, result: AuthDeleteResult | None = None, exc: BaseException | None = None) -> None:
        self.result = result or AuthDeleteResult(outcome=OUTCOME_DELETED)
        self.exc = exc
        self.calls: list[str] = []

    def hard_delete(self, user_id: str) -> AuthDeleteResult:
        self.calls.append(user_id)
        if self.exc is not None:
            raise self.exc
        return self.result


def _worker(
    repository: FakeRepository,
    auth_admin: FakeAuthAdmin | SupabaseAccountDeletionAuthAdmin,
    worker_id: str = "worker-1",
) -> AccountDeletionWorker:
    return AccountDeletionWorker(
        repository=repository,
        auth_admin=auth_admin,
        config=AccountDeletionWorkerConfig(worker_id=worker_id, lease_seconds=300, max_batch=10),
    )


def _domain_calls(repo: FakeRepository) -> list[tuple]:
    """Call sequence ignoring the trailing empty-queue acquire probe."""
    return [call for call in repo.calls if call[0] != "acquire_lease"]


# ─── 1. Empty queue is nominal ──────────────────────────────────────────────


def test_found_false_is_no_work_not_error(caplog):
    repo = FakeRepository(jobs=[])
    auth = FakeAuthAdmin()
    with caplog.at_level(logging.INFO):
        result = _worker(repo, auth).run_once()
    assert result.no_work is True
    assert result.completed == 0
    assert auth.calls == [], "Auth must never be called on an empty queue"
    assert "account_deletion_no_work" in caplog.text


def test_run_once_never_sleeps_or_polls(caplog):
    """A single acquire returning None ends the round immediately."""
    repo = FakeRepository(jobs=[])
    started = threading.active_count()
    result = _worker(repo, auth_admin=FakeAuthAdmin()).run_once()
    assert result.no_work is True
    assert repo.calls == [("acquire_lease",)]
    assert threading.active_count() == started


# ─── 2. CLI --once empty queue ──────────────────────────────────────────────


def test_cli_once_empty_queue_exits_zero(capsys):
    def factory(config: AccountDeletionRuntimeConfig) -> AccountDeletionWorker:
        return _worker(FakeRepository(jobs=[]), FakeAuthAdmin())

    code = cli_main(["--once"], env={"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}, worker_factory=factory)
    assert code == 0
    out = capsys.readouterr().out
    assert "no_work=True" in out
    assert USER_ID not in out


def test_cli_once_missing_config_exits_one():
    assert cli_main(["--once"], env={}, worker_factory=lambda cfg: _worker(FakeRepository(), FakeAuthAdmin())) == 1


def test_cli_once_invalid_lease_exits_one():
    env = {"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k", "ACCOUNT_DELETION_LEASE_SECONDS": "0"}
    assert cli_main(["--once"], env=env, worker_factory=lambda cfg: _worker(FakeRepository(), FakeAuthAdmin())) == 1


def test_cli_once_invalid_max_batch_exits_one():
    env = {"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k", "ACCOUNT_DELETION_MAX_BATCH": "2000"}
    assert cli_main(["--once"], env=env, worker_factory=lambda cfg: _worker(FakeRepository(), FakeAuthAdmin())) == 1


def test_cli_once_auth_timeout_greater_than_lease_exits_one():
    env = {
        "SUPABASE_URL": "http://x",
        "SUPABASE_SERVICE_ROLE_KEY": "k",
        "ACCOUNT_DELETION_LEASE_SECONDS": "60",
        "ACCOUNT_DELETION_AUTH_TIMEOUT_SECONDS": "120",
    }
    assert cli_main(["--once"], env=env, worker_factory=lambda cfg: _worker(FakeRepository(), FakeAuthAdmin())) == 1


def test_cli_default_worker_id_respects_allowlist():
    import re

    env = {"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}
    config = AccountDeletionRuntimeConfig.from_env(env)
    assert re.match(r"^[A-Za-z0-9_.:-]{1,64}$", config.worker_id)
    assert config.worker_id.startswith("cli-worker-")


def test_cli_once_acquisition_failure_exits_one(caplog):
    repo = FakeRepository(jobs=[])
    repo.acquire_error = PersistenceError("database_error", "persistence error")

    def factory(config: AccountDeletionRuntimeConfig) -> AccountDeletionWorker:
        return _worker(repo, FakeAuthAdmin())

    assert cli_main(["--once"], env={"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}, worker_factory=factory) == 1


def test_cli_once_interrupt_propagates_never_round_failed():
    """Operator-initiated termination (KeyboardInterrupt) must propagate
    with its original semantics and never be reported as round_failed."""

    def factory(config: AccountDeletionRuntimeConfig) -> AccountDeletionWorker:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        cli_main(["--once"], env={"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}, worker_factory=factory)


def test_cli_once_system_exit_propagates():
    def factory(config: AccountDeletionRuntimeConfig) -> AccountDeletionWorker:
        raise SystemExit(3)

    with pytest.raises(SystemExit) as exc:
        cli_main(["--once"], env={"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"}, worker_factory=factory)
    assert exc.value.code == 3


def test_cli_reuses_worker_defaults_single_source_of_truth():
    """The CLI must not redefine the SQL-bounded defaults: they come from
    the worker module so the two entrypoints can never drift."""
    from backend.account_deletion_worker import (
        DEFAULT_LEASE_SECONDS as WORKER_LEASE,
        DEFAULT_MAX_BATCH as WORKER_BATCH,
    )

    assert cli_module.DEFAULT_LEASE_SECONDS is WORKER_LEASE
    assert cli_module.DEFAULT_MAX_BATCH is WORKER_BATCH
    # The CLI-only auth timeout stays local (no worker counterpart).
    assert cli_module.DEFAULT_AUTH_TIMEOUT_SECONDS == 10.0


# ─── 3. DB purge before Auth ────────────────────────────────────────────────


def test_db_purge_before_auth_and_finalize_after(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=None)])
    auth = FakeAuthAdmin()
    with caplog.at_level(logging.INFO):
        result = _worker(repo, auth).run_once()
    assert result.completed == 1
    assert result.no_work is False, "a round that claimed a job is never no_work"
    assert [c[0] for c in _domain_calls(repo)] == ["purge", "finalize"]
    assert auth.calls == [USER_ID]
    assert "account_deletion_db_purged" in caplog.text
    assert "account_deletion_auth_deleted" in caplog.text
    assert "account_deletion_completed" in caplog.text


# ─── 4. Auth never called when purge fails ──────────────────────────────────


def test_auth_never_called_when_purge_fails(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=None)])
    repo.purge_error = PersistenceError("database_error", "persistence error")
    auth = FakeAuthAdmin()
    result = _worker(repo, auth).run_once()
    assert result.failed == 1
    assert auth.calls == [], "Auth MUST NOT be called when the purge fails"
    assert ("purge", JOB_ID) in repo.calls
    assert "account_deletion_failed" in caplog.text
    assert "db_purge_failed" in caplog.text
    assert not any(c[0] == "finalize" for c in repo.calls)


# ─── 5. db_purged_at skips destructive DB mutations ─────────────────────────


def test_db_purged_at_present_skips_purge(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin()
    with caplog.at_level(logging.INFO):
        result = _worker(repo, auth).run_once()
    assert result.completed == 1
    assert [c[0] for c in _domain_calls(repo)] == ["finalize"], "purge must be skipped when db_purged_at is set"
    assert auth.calls == [USER_ID]
    assert "account_deletion_auth_deleted" in caplog.text


# ─── 6. Auth success calls finalize ─────────────────────────────────────────


def test_auth_success_calls_finalize():
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin(result=AuthDeleteResult(outcome=OUTCOME_DELETED))
    result = _worker(repo, auth).run_once()
    assert result.completed == 1
    assert ("finalize", JOB_ID) in repo.calls


# ─── 7. Auth already absent is idempotent success ───────────────────────────


def test_auth_already_absent_is_idempotent_success(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin(result=AuthDeleteResult(outcome=OUTCOME_ALREADY_ABSENT))
    with caplog.at_level(logging.INFO):
        result = _worker(repo, auth).run_once()
    assert result.completed == 1
    assert ("finalize", JOB_ID) in repo.calls
    assert "account_deletion_auth_already_absent" in caplog.text
    assert "account_deletion_completed" in caplog.text


# ─── 8. "not found" is detected structurally, never by text ─────────────────


def _admin_api_with_delete(exc: BaseException | None = None, ok: bool = True) -> SimpleNamespace:
    def delete_user(user_id: str, should_soft_delete: bool = False):
        if exc is not None:
            raise exc
        return None

    api = SimpleNamespace(delete_user=delete_user)
    return api


def test_adapter_user_not_found_code_is_already_absent():
    exc = AuthApiError("User not found", 404, "user_not_found")
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.outcome == OUTCOME_ALREADY_ABSENT
    assert result.error_code is None


def test_adapter_user_not_found_text_alone_is_not_success():
    """An AuthApiError whose message contains 'user not found' but whose
    structured code is absent/different is NOT treated as already absent."""
    exc = AuthApiError("User not found", 404, None)
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.outcome == OUTCOME_AUTH_ERROR
    assert result.error_code == ERROR_AUTH_FAILED


def test_adapter_plain_runtime_error_with_not_found_text_is_not_success():
    exc = RuntimeError(f"user not found {EMAIL}")
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    # Never parsed by text: a raw RuntimeError is transient unavailability.
    assert result.outcome == OUTCOME_AUTH_ERROR
    assert result.error_code == ERROR_AUTH_UNAVAILABLE


# ─── 9. Transient Auth failure records sanitized retry ──────────────────────


def test_transient_auth_failure_records_retry(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin(
        result=AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE)
    )
    with caplog.at_level(logging.INFO):
        result = _worker(repo, auth).run_once()
    assert result.retry_scheduled == 1
    assert ("record_retry", JOB_ID) in repo.calls
    assert not any(c[0] == "finalize" for c in repo.calls)
    assert "account_deletion_retry_scheduled" in caplog.text
    assert "auth_unavailable" in caplog.text


def test_adapter_retryable_error_classifies_unavailable():
    exc = AuthRetryableError("upstream unavailable", 503)
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.outcome == OUTCOME_AUTH_ERROR
    assert result.error_code == ERROR_AUTH_UNAVAILABLE


def test_adapter_api_error_retryable_status_classifies_unavailable():
    for status in (502, 503, 504, 520, 521, 522, 523, 524, 530):
        exc = AuthApiError("upstream", status, None)
        adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
        result = adapter.hard_delete(USER_ID)
        assert result.error_code == ERROR_AUTH_UNAVAILABLE, status


def test_adapter_rate_limit_code_classifies_unavailable():
    exc = AuthApiError("rate limited", 429, "over_request_rate_limit")
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.error_code == ERROR_AUTH_UNAVAILABLE


def test_adapter_unknown_error_classifies_unavailable():
    exc = AuthUnknownError("could not parse upstream", RuntimeError("boom"))
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.error_code == ERROR_AUTH_UNAVAILABLE


def test_adapter_transport_exception_classifies_unavailable_without_leak():
    class _ConnectError(Exception):
        pass

    exc = _ConnectError(f"connection refused {SECRET_SENTINEL}")
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.error_code == ERROR_AUTH_UNAVAILABLE
    # The upstream message must never leak into the structured result.
    assert SECRET_SENTINEL not in str(result)


def test_adapter_client_side_validation_error_classifies_failed():
    """The real SDK raises a bare ValueError for an invalid UUID before any
    network call (supabase_auth.helpers.validate_uuid). That is a
    deterministic client-side failure -> auth_failed, never transient, and
    the echoed id must not leak into the structured result."""
    exc = ValueError(f"Invalid id, '{USER_ID}' is not a valid uuid")
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.outcome == OUTCOME_AUTH_ERROR
    assert result.error_code == ERROR_AUTH_FAILED
    assert USER_ID not in str(result)


def test_adapter_forbidden_classifies_forbidden():
    for status in (401, 403):
        exc = AuthApiError("not authorized", status, "no_authorization")
        adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
        result = adapter.hard_delete(USER_ID)
        assert result.outcome == OUTCOME_AUTH_ERROR
        assert result.error_code == ERROR_AUTH_FORBIDDEN


def test_adapter_hard_failure_classifies_failed():
    exc = AuthApiError("user banned", 400, "user_banned")
    adapter = SupabaseAccountDeletionAuthAdmin(_admin_api_with_delete(exc=exc))
    result = adapter.hard_delete(USER_ID)
    assert result.error_code == ERROR_AUTH_FAILED


def test_adapter_success_passes_soft_delete_false():
    seen: list[tuple] = []

    def delete_user(user_id: str, should_soft_delete: bool = False):
        seen.append((user_id, should_soft_delete))

    adapter = SupabaseAccountDeletionAuthAdmin(SimpleNamespace(delete_user=delete_user))
    result = adapter.hard_delete(USER_ID)
    assert result.outcome == OUTCOME_DELETED
    assert seen == [(USER_ID, False)], "hard delete must never soft-delete"


def test_deterministic_auth_failure_records_failure(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin(
        result=AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_FORBIDDEN)
    )
    result = _worker(repo, auth).run_once()
    assert result.failed == 1
    assert ("record_failure", JOB_ID, ERROR_AUTH_FORBIDDEN) in repo.calls
    assert not any(c[0] == "finalize" for c in repo.calls)


# ─── 10. db_purged_at preserved across Auth failures/retries ────────────────


def test_db_purged_at_preserved_across_auth_failure_and_retry():
    """The worker never mutates db_purged_at; a failed job keeps it and the
    next round skips the destructive purge entirely (SQL also preserves it,
    verified by the integration suite)."""
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin(
        result=AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE)
    )
    first = _worker(repo, auth).run_once()
    assert first.retry_scheduled == 1
    # The same job reappears with db_purged_at intact for the next worker.
    repo2 = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    second = _worker(repo2, FakeAuthAdmin()).run_once()
    assert second.completed == 1
    assert [c[0] for c in _domain_calls(repo2)] == ["finalize"], "purge must stay skipped after a retry"


# ─── 11. next_attempt_at gating never requires worker sleeps ────────────────


def test_next_attempt_gating_is_database_authority_not_sleeps():
    """The worker never sleeps or polls: the DB returns no eligible job
    while next_attempt_at is in the future, and run_once ends immediately."""
    repo = FakeRepository(jobs=[])
    result = _worker(repo, FakeAuthAdmin()).run_once()
    assert result.no_work is True
    assert repo.calls == [("acquire_lease",)]


# ─── 12. Attempts exhausted is not reprocessed ──────────────────────────────


def test_exhausted_job_not_reprocessed():
    """The SQL never hands out a job at/above the ceiling; a worker whose
    deferral hits the ceiling counts the terminal failed outcome."""
    # Terminal retry outcome from the DB (record_retry at the ceiling).
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT, attempts=100)])
    auth = FakeAuthAdmin(
        result=AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE)
    )

    class _TerminalFakeRepository(FakeRepository):
        def record_retry(self, job_id, worker_id):
            self.calls.append(("record_retry", job_id))
            return RetryResult(status="failed", job_id=job_id, next_attempt_at=None)

    repo = _TerminalFakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT, attempts=100)])
    result = _worker(repo, auth).run_once()
    assert result.failed == 1
    assert result.retry_scheduled == 0
    # A subsequent round sees no eligible job (SQL never re-claims it).
    repo2 = FakeRepository(jobs=[])
    result2 = _worker(repo2, FakeAuthAdmin()).run_once()
    assert result2.no_work is True


# ─── 13. Two workers with independent clients ───────────────────────────────


def test_two_workers_never_process_same_job():
    """Two workers with independent repository/auth clients race on a shared
    job store; the store (simulating FOR UPDATE SKIP LOCKED) hands each job
    to exactly one worker."""
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    store: list[AcquiredJob] = [_job(job_id=JOB_ID, user_id=USER_ID)]
    claims: list[str] = []
    completed: list[str] = []

    def _claim():
        with lock:
            if not store:
                return None
            return store.pop(0)

    class _SharedRepo(FakeRepository):
        def __init__(self):
            super().__init__(jobs=[])
            self._first_acquire = True

        def acquire_lease(self, worker_id, lease_seconds, max_batch):
            self.calls.append(("acquire_lease",))
            if self._first_acquire:
                self._first_acquire = False
                # Race both workers' FIRST claim against the shared store.
                barrier.wait(timeout=30)
            job = _claim()
            if job is None:
                return None
            with lock:
                claims.append(job.job_id)
            return job

    def _run(worker_id: str) -> tuple[int, int]:
        repo = _SharedRepo()
        auth = FakeAuthAdmin()
        result = _worker(repo, auth, worker_id=worker_id).run_once()
        return result.completed, result.no_work

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run, "worker-a"), pool.submit(_run, "worker-b")]
        outcomes = [future.result(timeout=60) for future in futures]

    assert len(claims) == 1, "exactly one worker must claim the single job"
    # Exactly one worker completed the job. The loser observed an empty
    # queue; the winner may or may not (its trailing probe drains mid-round,
    # which is not no_work).
    assert sum(o[0] for o in outcomes) == 1
    assert any(o[1] for o in outcomes)


# ─── 14. Lost lease prevents finalize ───────────────────────────────────────


def test_lease_lost_prevents_finalize(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    repo.finalize_error = PersistenceError("database_error", "persistence error")
    with caplog.at_level(logging.INFO):
        result = _worker(repo, FakeAuthAdmin()).run_once()
    assert result.lease_lost == 1
    assert result.completed == 0
    assert "account_deletion_lease_lost" in caplog.text
    assert "finalize_blocked" in caplog.text


def test_lease_lost_during_failure_transition():
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    repo.record_failure_error = PersistenceError("database_error", "persistence error")
    auth = FakeAuthAdmin(
        result=AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_FORBIDDEN)
    )
    result = _worker(repo, auth).run_once()
    assert result.lease_lost == 1
    assert result.failed == 0


def test_lease_lost_during_retry_transition():
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    repo.record_retry_error = PersistenceError("database_error", "persistence error")
    auth = FakeAuthAdmin(
        result=AuthDeleteResult(outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE)
    )
    result = _worker(repo, auth).run_once()
    assert result.lease_lost == 1
    assert result.retry_scheduled == 0


# ─── 15. Recovery after lease loss stays safe ───────────────────────────────


def test_recovery_after_lease_loss_stays_safe():
    """Worker A purges, then loses its lease (finalize fails closed). Worker
    B reclaims the job with db_purged_at intact, skips the purge and
    completes without repeating any destructive mutation."""
    repo_a = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    repo_a.finalize_error = PersistenceError("database_error", "persistence error")
    first = _worker(repo_a, FakeAuthAdmin(), worker_id="worker-a").run_once()
    assert first.lease_lost == 1

    repo_b = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    second = _worker(repo_b, FakeAuthAdmin(), worker_id="worker-b").run_once()
    assert second.completed == 1
    assert [c[0] for c in _domain_calls(repo_b)] == ["finalize"]
    assert ("purge", JOB_ID) not in repo_b.calls


# ─── 16. Crash after purge, before Auth, is re-executable ───────────────────


def test_crash_after_purge_before_auth_is_reexecutable():
    """The purge committed (db_purged_at set in the DB). The worker died
    before Auth. A new worker sees the marker, skips the purge, deletes Auth
    and finalizes. The destructive purge runs exactly once overall."""
    repo_a = FakeRepository(jobs=[_job(db_purged_at=None)])

    class _CrashAfterPurgeRepo(FakeRepository):
        def __init__(self):
            super().__init__(jobs=[_job(db_purged_at=None)])
            self.purged_job_db_state: dict[str, object] = {}

        def purge(self, job_id, worker_id, intent_fingerprint_sha256, expected_user_id=None):
            self.calls.append(("purge", job_id))
            # Simulate the transactional commit: the marker is now durable.
            self.purged_job_db_state[job_id] = DB_PURGED_AT
            return PurgeResult(
                status="purged",
                job_id=job_id,
                db_purged_at=DB_PURGED_AT,
                counts={"outbox_events": 0, "turn_requests": 0, "archival_extractions": 0, "memories": 0, "chat_logs": 0, "admission_reservations": 0, "privacy_operations": 0, "profiles": 0},
            )

    class _CrashingAuth(FakeAuthAdmin):
        def hard_delete(self, user_id: str):
            self.calls.append(user_id)
            raise KeyboardInterrupt  # the worker dies right here

    repo_a = _CrashAfterPurgeRepo()
    auth_a = _CrashingAuth()
    with pytest.raises(KeyboardInterrupt):
        _worker(repo_a, auth_a, worker_id="worker-a").run_once()
    assert auth_a.calls == [USER_ID]
    assert ("purge", JOB_ID) in repo_a.calls

    # Worker B: same durable state, marker present.
    repo_b = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    second = _worker(repo_b, FakeAuthAdmin(), worker_id="worker-b").run_once()
    assert second.completed == 1
    assert [c[0] for c in _domain_calls(repo_b)] == ["finalize"]
    assert ("purge", JOB_ID) not in repo_b.calls, "destructive purge must not repeat"


# ─── 17. Cancellation abandons no thread/task ───────────────────────────────


def test_cancellation_propagates_without_abandoning_threads():
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin(exc=KeyboardInterrupt())
    threads_before = len(threading.enumerate())
    with pytest.raises(KeyboardInterrupt):
        _worker(repo, auth).run_once()
    assert len(threading.enumerate()) == threads_before
    # No finalize happened; the job is still owned by the dead worker's
    # lease and will be recovered by another worker (tested above).
    assert not any(c[0] == "finalize" for c in repo.calls)
    # The same worker object can be reused after cancellation (stateless).
    repo.jobs = [_job(db_purged_at=DB_PURGED_AT)]
    worker = _worker(repo, FakeAuthAdmin())
    result = worker.run_once()
    assert result.completed == 1


# ─── 18. No singleton/global per-user state ─────────────────────────────────


def test_no_singleton_or_global_per_user_state():
    import backend.account_deletion_worker as module

    # The module holds no user state: the only globals are constants.
    globals_ = vars(module)
    for key, value in globals_.items():
        if key.startswith("__"):
            continue
        assert not isinstance(value, (dict, list, set)), key

    # Two sequential runs with different users share nothing mutable.
    repo_a = FakeRepository(jobs=[_job(job_id=JOB_ID, user_id=USER_ID, db_purged_at=DB_PURGED_AT)])
    worker_a = _worker(repo_a, FakeAuthAdmin(), worker_id="worker-a")
    result_a = worker_a.run_once()
    assert result_a.completed == 1

    repo_b = FakeRepository(jobs=[_job(job_id=JOB_ID_B, user_id=USER_ID_B, db_purged_at=DB_PURGED_AT)])
    worker_b = _worker(repo_b, FakeAuthAdmin(), worker_id="worker-b")
    result_b = worker_b.run_once()
    assert result_b.completed == 1
    assert USER_ID_B not in vars(worker_a)
    assert USER_ID not in vars(worker_b)


# ─── 19. Import purity (no Auth/Supabase/Groq/embeddings/network) ───────────

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

    # Block Groq and embeddings before the import.
    def _blocked(*args, **kwargs):
        raise AssertionError("heavy dependency constructed during import")

    sys.modules["groq"] = type(sys)("_fake_groq_module")
    _groq = sys.modules["groq"]
    _groq.AsyncGroq = _blocked
    _groq.Groq = _blocked

    sys.modules["sentence_transformers"] = type(sys)("_fake_st_module")
    _st = sys.modules["sentence_transformers"]
    _st.SentenceTransformer = _blocked

    threads_before = len(threading.enumerate())

    import backend.account_deletion_worker
    import backend.account_deletion_cli

    threads_after = len(threading.enumerate())

    assert threads_after == threads_before, "import started a thread"
    print("ACCOUNT_DELETION_WORKER_PURITY_OK")
    """
)


def test_worker_and_cli_import_is_pure():
    result = subprocess.run(
        [sys.executable, "-c", _PURITY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "ACCOUNT_DELETION_WORKER_PURITY_OK" in result.stdout


def test_cli_real_client_builder_is_lazy():
    """Building the real client is only reachable from the composition root;
    importing the CLI and calling main with a worker_factory never touches
    the Supabase SDK constructor."""
    import backend.account_deletion_cli as cli

    assert cli._build_client is not None  # exists, but is never called in tests
    # The main() path with worker_factory does not import create_client.
    code = cli_main(
        ["--once"],
        env={"SUPABASE_URL": "http://x", "SUPABASE_SERVICE_ROLE_KEY": "k"},
        worker_factory=lambda cfg: _worker(FakeRepository(jobs=[]), FakeAuthAdmin()),
    )
    assert code == 0


def test_cli_build_client_silences_httpx_request_logs():
    """The raw Auth user UUID is embedded in the admin request URL; httpx
    INFO request logging must be silenced by the only real-client builder.
    Building the client with a dummy URL opens no socket (no request)."""
    import logging

    import httpx
    import supabase._sync.client as supabase_sync

    orig_level = logging.getLogger("httpx").level
    orig_create = supabase_sync.create_client
    try:
        # Ensure the real client is actually constructed (no factory).
        config = AccountDeletionRuntimeConfig.from_env(
            {"SUPABASE_URL": "http://127.0.0.1:1", "SUPABASE_SERVICE_ROLE_KEY": "k"}
        )
        client = None
        try:
            client = cli_module._build_client(config)
            assert isinstance(client, supabase_sync.Client)
            assert logging.getLogger("httpx").level == logging.WARNING
        finally:
            if client is not None:
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
    finally:
        logging.getLogger("httpx").setLevel(orig_level)
        supabase_sync.create_client = orig_create


# ─── 24. Log sanitization ───────────────────────────────────────────────────


def test_logs_never_contain_sensitive_markers(caplog):
    repo = FakeRepository(jobs=[_job(db_purged_at=None)])
    with caplog.at_level(logging.INFO):
        # The adapter swallows the upstream exception; the worker emits only
        # sanitized events. An unclassified exception must not reach logs.
        adapter = SupabaseAccountDeletionAuthAdmin(
            _admin_api_with_delete(exc=RuntimeError(f"boom {USER_ID} {REF} {OPERATION_ID} {JOB_ID} {EMAIL} {ACCESS_TOKEN} {SECRET_SENTINEL}"))
        )
        result = adapter.hard_delete(USER_ID)
        assert result.error_code == ERROR_AUTH_UNAVAILABLE
        _worker(repo, adapter).run_once()
    for sentinel in (USER_ID, REF, OPERATION_ID, JOB_ID, EMAIL, ACCESS_TOKEN, SECRET_SENTINEL):
        assert sentinel not in caplog.text


def test_unknown_auth_outcome_fails_closed_never_finalizes():
    """An adapter outcome outside the documented taxonomy must never be
    treated as 'deleted': fail closed and record a sanitized failure."""
    repo = FakeRepository(jobs=[_job(db_purged_at=DB_PURGED_AT)])
    auth = FakeAuthAdmin(result=AuthDeleteResult(outcome="bogus_outcome"))
    result = _worker(repo, auth).run_once()
    assert result.failed == 1
    assert result.completed == 0
    assert ("record_failure", JOB_ID, ERROR_AUTH_FAILED) in repo.calls
    assert not any(c[0] == "finalize" for c in repo.calls)


def test_worker_events_are_constant_and_registered():
    from backend.observability import EVENT_NAMES

    for event in (
        "account_deletion_worker_started",
        "account_deletion_no_work",
        "account_deletion_db_purged",
        "account_deletion_auth_deleted",
        "account_deletion_auth_already_absent",
        "account_deletion_retry_scheduled",
        "account_deletion_failed",
        "account_deletion_completed",
        "account_deletion_lease_lost",
    ):
        assert event in EVENT_NAMES
