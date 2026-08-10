"""Durable account deletion worker (#325).

Composes the #324 ledger contract (``backend.account_deletion``) with the
Supabase Auth Admin hard delete through a small, injectable Auth adapter.

Guarantees (enforced by the database, mirrored here):

* **DB-first ordering.** ``account_deletion_purge`` is called BEFORE any
  Auth operation. If the DB purge fails, Auth is never called. A job that
  already carries ``db_purged_at`` (crash after DB commit, before Auth)
  skips all destructive DB mutations and goes straight to Auth.
* **Lease ownership is the database's.** Jobs are acquired exclusively via
  ``account_deletion_acquire_lease`` (``FOR UPDATE SKIP LOCKED``). A worker
  that lost its lease can no longer purge/fail/retry/finalize: those RPCs
  fail closed and the worker emits ``account_deletion_lease_lost``. Another
  worker recovers the job later.
* **``completed`` only after Auth.** ``account_deletion_finalize`` is called
  only after the Auth hard delete succeeded or the user was proven absent
  through a structured SDK property. Finalize minimizes the raw ``user_id``.
* **Retry/attempts are governed by the DB.** ``record_retry`` is used for
  transient Auth unavailability (the SQL RPC is explicitly the voluntary
  deferral path: back to pending, backoff via ``next_attempt_at``).
  ``record_failure`` is used for deterministic Auth failures (the job moves
  to ``failed`` with a sanitized ``error_code`` and DB backoff). There is no
  Python backoff: ``next_attempt_at`` is the authority.
* **Empty queue is nominal.** ``acquire_lease`` returning ``None`` (RPC
  ``found:false``) is a result, never an error: no Auth call, no sleep, the
  run terminates.
* **Sanitized everywhere.** Raw user ids, HMACs, operation ids, job ids,
  emails, tokens, payloads, SQL and upstream exception text never appear in
  logs, metrics, exceptions or results. Auth errors are classified into a
  small constant taxonomy (``auth_unavailable``, ``auth_forbidden``,
  ``auth_failed``) using structured SDK properties only; parsing exception
  text is forbidden.

This module is import-pure: importing it constructs no Supabase client, no
Auth client, opens no socket and loads no Groq/embeddings. Only the CLI
composition root builds real clients.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from backend.account_deletion import (
    AccountDeletionRepository,
    AcquiredJob,
)
from backend.atomic_turn_commit import (
    ConflictError,
    PersistenceError,
    ValidationError,
)
from backend.observability import (
    EVENT_ACCOUNT_DELETION_AUTH_ALREADY_ABSENT,
    EVENT_ACCOUNT_DELETION_AUTH_DELETED,
    EVENT_ACCOUNT_DELETION_COMPLETED,
    EVENT_ACCOUNT_DELETION_DB_PURGED,
    EVENT_ACCOUNT_DELETION_FAILED,
    EVENT_ACCOUNT_DELETION_LEASE_LOST,
    EVENT_ACCOUNT_DELETION_NO_WORK,
    EVENT_ACCOUNT_DELETION_RETRY_SCHEDULED,
    EVENT_ACCOUNT_DELETION_WORKER_STARTED,
    emit_event,
)
from supabase_auth.errors import (
    AuthApiError,
    AuthRetryableError,
    AuthUnknownError,
)

logger = logging.getLogger(__name__)

# ─── Conservative defaults (SQL bounds: lease_seconds 1..3600, max_batch 1..1000) ──

DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_BATCH = 10

_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# ─── Auth outcome/error taxonomy (low cardinality, constant) ────────────────

OUTCOME_DELETED = "deleted"
OUTCOME_ALREADY_ABSENT = "already_absent"
OUTCOME_AUTH_ERROR = "auth_error"

ERROR_AUTH_UNAVAILABLE = "auth_unavailable"
ERROR_AUTH_FORBIDDEN = "auth_forbidden"
ERROR_AUTH_FAILED = "auth_failed"
ERROR_DB_PURGE_FAILED = "db_purge_failed"

#: Structured SDK status codes considered transient infrastructure failures
#: (mirrors the network_error_codes list of the installed supabase-auth SDK).
_RETRYABLE_AUTH_STATUSES = frozenset({502, 503, 504, 520, 521, 522, 523, 524, 530})
#: Structured SDK error codes considered transient (rate limiting/timeouts).
_RETRYABLE_AUTH_CODES = frozenset({"over_request_rate_limit", "request_timeout"})
#: Structured SDK error code proving the Auth user no longer exists.
_AUTH_USER_NOT_FOUND = "user_not_found"


@dataclass(frozen=True)
class AuthDeleteResult:
    """Structured outcome of one Auth admin hard delete (#325).

    ``outcome`` is ``deleted``, ``already_absent`` or ``auth_error``. When
    ``auth_error``, ``error_code`` is one of the constant taxonomy above.
    Never contains upstream text, ids, tokens or URLs.
    """

    outcome: str
    error_code: Optional[str] = None


class AccountDeletionAuthAdmin(Protocol):
    """Injectable Auth Admin boundary used by the worker.

    Only the CLI composition root may construct a real implementation with a
    live Supabase Auth Admin client; unit tests always use fakes.
    """

    def hard_delete(self, user_id: str) -> AuthDeleteResult:
        """Hard-delete the Auth user and return a structured outcome."""
        ...


class SupabaseAccountDeletionAuthAdmin:
    """Adapter over ``client.auth.admin.delete_user`` (server-side only).

    Uses the API of the SDK locked in ``backend/requirements.txt``
    (supabase-auth 2.31.0): ``delete_user(id, should_soft_delete=False)`` is
    the definitive hard delete (the SDK default for soft delete is
    ``False``; it is passed explicitly). There is no soft-delete and no undo.

    Classification uses ONLY structured exception properties (``code``,
    ``status``, exception type). Parsing exception text is forbidden.
    """

    def __init__(self, admin_api: Any) -> None:
        self._admin_api = admin_api

    def hard_delete(self, user_id: str) -> AuthDeleteResult:
        try:
            self._admin_api.delete_user(user_id, should_soft_delete=False)
            return AuthDeleteResult(outcome=OUTCOME_DELETED)
        except AuthApiError as exc:
            if exc.code == _AUTH_USER_NOT_FOUND:
                # Idempotent success: the user was already removed.
                return AuthDeleteResult(outcome=OUTCOME_ALREADY_ABSENT)
            if isinstance(exc.status, int) and exc.status in (401, 403):
                # Not retryable in the ordinary sense: credentials/scope.
                return AuthDeleteResult(
                    outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_FORBIDDEN
                )
            if exc.code in _RETRYABLE_AUTH_CODES or (
                isinstance(exc.status, int) and exc.status in _RETRYABLE_AUTH_STATUSES
            ):
                return AuthDeleteResult(
                    outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE
                )
            return AuthDeleteResult(
                outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_FAILED
            )
        except (AuthRetryableError, AuthUnknownError):
            return AuthDeleteResult(
                outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE
            )
        except ValueError:
            # Client-side validation failure (e.g. an invalid UUID rejected
            # by the SDK before any network call). Deterministic, never
            # transient. The upstream message (which may echo the id) is
            # never propagated or logged.
            return AuthDeleteResult(
                outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_FAILED
            )
        except Exception:
            # Transport-level failures the SDK lets escape (connect errors,
            # read timeouts) are transient. The upstream message is never
            # propagated or logged.
            return AuthDeleteResult(
                outcome=OUTCOME_AUTH_ERROR, error_code=ERROR_AUTH_UNAVAILABLE
            )


@dataclass(frozen=True)
class AccountDeletionWorkerConfig:
    """Bounded worker configuration, mirrored from the SQL validation."""

    worker_id: str
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    max_batch: int = DEFAULT_MAX_BATCH

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not _WORKER_ID_RE.match(self.worker_id):
            raise ValueError("worker_id is invalid")
        if self.lease_seconds < 1 or self.lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if self.max_batch < 1 or self.max_batch > 1000:
            raise ValueError("max_batch must be between 1 and 1000")


@dataclass
class WorkerRunResult:
    """Aggregate, sanitized outcome of one ``run_once`` round."""

    no_work: bool = False
    completed: int = 0
    retry_scheduled: int = 0
    failed: int = 0
    lease_lost: int = 0


class AccountDeletionWorker:
    """Stateless, synchronous worker: one bounded round per ``run_once``.

    No loop, no daemon, no background thread and no global state. All
    dependencies (repository, Auth adapter, config, logger) are injected;
    the raw ``user_id`` exists only on the local stack of a job in flight.
    """

    def __init__(
        self,
        *,
        repository: AccountDeletionRepository,
        auth_admin: AccountDeletionAuthAdmin,
        config: AccountDeletionWorkerConfig,
        worker_logger: Optional[logging.Logger] = None,
    ) -> None:
        self._repository = repository
        self._auth_admin = auth_admin
        self._config = config
        self._logger = worker_logger or logger

    def run_once(self) -> WorkerRunResult:
        """Process up to ``max_batch`` jobs and return a sanitized summary.

        Never sleeps and never polls. ``no_work`` means the round found an
        entirely empty queue (zero jobs claimed): it is set only when the
        very first acquire returns ``None``. A queue that drains mid-round
        (after jobs were processed) does not set ``no_work``.

        Raises:
            PersistenceError/ValidationError: On an operational failure of
                the acquisition step itself (database unreachable, invalid
                worker parameters). Per-job failures never propagate.
        """
        emit_event(
            self._logger,
            EVENT_ACCOUNT_DELETION_WORKER_STARTED,
            code="started",
        )
        result = WorkerRunResult()
        claimed_any = False
        for _ in range(self._config.max_batch):
            job = self._repository.acquire_lease(
                self._config.worker_id,
                self._config.lease_seconds,
                self._config.max_batch,
            )
            if job is None:
                # Queue drained for this round: nominal, never an error.
                break
            claimed_any = True
            self._process_job(job, result)
        if not claimed_any:
            # An entirely empty queue: no job was eligible at all.
            result.no_work = True
            emit_event(self._logger, EVENT_ACCOUNT_DELETION_NO_WORK)
        return result

    # ─── Per-job pipeline (DB-first, fail-closed) ───────────────────────────

    def _process_job(self, job: AcquiredJob, result: WorkerRunResult) -> None:
        # 1. PostgreSQL first. The purge is skipped entirely when the job
        # already carries db_purged_at (crash after DB commit): no
        # destructive mutation is repeated, the marker is authoritative.
        if job.db_purged_at is None:
            try:
                purge = self._repository.purge(
                    job.job_id,
                    self._config.worker_id,
                    job.intent_fingerprint_sha256,
                )
            except (PersistenceError, ConflictError, ValidationError):
                # Purge failed: NEVER call Auth. Fail closed; the lease will
                # expire and another worker will retry the transactional
                # purge (a failed purge rolled back every delete).
                emit_event(
                    self._logger,
                    EVENT_ACCOUNT_DELETION_FAILED,
                    level=logging.ERROR,
                    code=ERROR_DB_PURGE_FAILED,
                    attempt=job.attempts,
                )
                result.failed += 1
                return
            emit_event(
                self._logger,
                EVENT_ACCOUNT_DELETION_DB_PURGED,
                code=purge.status,
            )

        # 2. Auth Admin hard delete (bounded by the transport timeout).
        auth_result = self._auth_admin.hard_delete(job.user_id)

        if auth_result.outcome == OUTCOME_AUTH_ERROR:
            if auth_result.error_code == ERROR_AUTH_UNAVAILABLE:
                self._record_retry(job, result, auth_result.error_code)
            else:
                self._record_failure(job, result, auth_result.error_code)
            return

        if auth_result.outcome == OUTCOME_ALREADY_ABSENT:
            emit_event(self._logger, EVENT_ACCOUNT_DELETION_AUTH_ALREADY_ABSENT)
        elif auth_result.outcome == OUTCOME_DELETED:
            emit_event(self._logger, EVENT_ACCOUNT_DELETION_AUTH_DELETED)
        else:
            # Unknown adapter outcome: never conclude the account was
            # removed. Fail closed and let the DB schedule a retry.
            self._record_failure(job, result, ERROR_AUTH_FAILED)
            return

        # 3. Finalize ONLY after Auth is confirmed gone. The lease must
        # still belong to this worker; if it was lost while the external
        # call ran, finalize fails closed and another worker recovers.
        try:
            self._repository.finalize(job.job_id, self._config.worker_id)
        except (PersistenceError, ConflictError):
            emit_event(
                self._logger,
                EVENT_ACCOUNT_DELETION_LEASE_LOST,
                level=logging.ERROR,
                code="finalize_blocked",
                attempt=job.attempts,
            )
            result.lease_lost += 1
            return

        emit_event(
            self._logger,
            EVENT_ACCOUNT_DELETION_COMPLETED,
            attempt=job.attempts,
        )
        result.completed += 1

    def _record_retry(
        self, job: AcquiredJob, result: WorkerRunResult, error_code: str
    ) -> None:
        """Transient Auth unavailability: voluntary deferral (SQL semantics).

        ``account_deletion_record_retry`` returns the job to ``pending``
        with a DB-computed ``next_attempt_at`` backoff. When the attempts
        ceiling is reached the DB returns a terminal ``failed`` job instead.
        """
        try:
            retry = self._repository.record_retry(job.job_id, self._config.worker_id)
        except (PersistenceError, ConflictError, ValidationError):
            emit_event(
                self._logger,
                EVENT_ACCOUNT_DELETION_LEASE_LOST,
                level=logging.ERROR,
                code="transition_blocked",
                attempt=job.attempts,
            )
            result.lease_lost += 1
            return
        if retry.status == "failed":
            emit_event(
                self._logger,
                EVENT_ACCOUNT_DELETION_FAILED,
                level=logging.ERROR,
                code="attempts_exhausted",
                attempt=job.attempts,
            )
            result.failed += 1
            return
        emit_event(
            self._logger,
            EVENT_ACCOUNT_DELETION_RETRY_SCHEDULED,
            code=error_code,
            attempt=job.attempts,
        )
        result.retry_scheduled += 1

    def _record_failure(
        self, job: AcquiredJob, result: WorkerRunResult, error_code: str
    ) -> None:
        """Deterministic Auth failure: record a sanitized failure.

        ``account_deletion_record_failure`` moves the job to ``failed`` with
        ``error_code`` and a DB-computed ``next_attempt_at`` backoff; the
        raw ``user_id`` stays for the retry while ``db_purged_at`` (if the
        purge already committed) is preserved by the SQL.
        """
        try:
            failure = self._repository.record_failure(
                job.job_id, self._config.worker_id, error_code
            )
        except (PersistenceError, ConflictError, ValidationError):
            emit_event(
                self._logger,
                EVENT_ACCOUNT_DELETION_LEASE_LOST,
                level=logging.ERROR,
                code="transition_blocked",
                attempt=job.attempts,
            )
            result.lease_lost += 1
            return
        emit_event(
            self._logger,
            EVENT_ACCOUNT_DELETION_FAILED,
            level=logging.ERROR,
            code=failure.error_code,
            attempt=job.attempts,
        )
        result.failed += 1
