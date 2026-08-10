"""Durable account deletion ledger contract (#324).

Server-side Python contract for the account deletion job ledger exposed by
migration ``20260810120000_account_deletion_ledger``. This module is pure
Python: no FastAPI, no Groq, no embeddings, no Supabase client construction
at import time. It mirrors ``backend.privacy_operations`` and
``backend.atomic_turn_commit``: validation runs BEFORE the RPC and unexpected
persistence failures surface as sanitized ``PersistenceError``.

Scope (deliberately narrow)
==========================
This PR stops at the database foundation:

* domain models and strict parsers for the RPC envelopes;
* a ``compute_account_deletion_user_ref`` HMAC helper with a DEDICATED
  account-deletion domain (never correlated with the message/network/
  correlation/user-reference domains);
* a repository protocol + Supabase adapter for the eight runtime RPCs.

There is deliberately NO HTTP endpoint, NO Supabase Auth Admin call and NO
worker/CLI here: those belong to #325 (Auth deletion) and #326 (HTTP gate).

Design guarantees (enforced by the database, mirrored here)
===========================================================
* Identity comes ONLY from ``authenticated_user_id`` (server-side
  boundary). The persistent reference is an HMAC-SHA256 (lowercase hex,
  64 chars) under the dedicated ``account-deletion`` domain.
* Idempotency: the same (user_ref, operation_id) with the same intent
  fingerprint is an exact replay; a divergent fingerprint is a sanitized
  ``operation_conflict``. Different users never collide because the
  reference is a domain-separated HMAC of the identity.
* State machine: pending -> processing -> (failed | pending retry) ->
  processing -> completed. ``db_purged_at`` is set only in the purge
  transaction; ``user_id`` is minimized to NULL on finalize.
* Every RPC result parser is strict: allowlisted fields, exact types
  (bools rejected where ints are expected), UUID/hex/status validation,
  unknown or missing fields fail closed, and a divergent identity in an
  RPC result never leaks into an exception message.
* ``acquire_lease`` returns ``None`` for the NOMINAL empty-queue result
  (RPC envelope ``{"found": false}``, #325): no eligible job is not an
  error. Every other malformed payload still fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid as _uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from backend.atomic_turn_commit import (
    ConflictError,
    PersistenceError,
    ValidationError,
)
from backend.admission import _validate_secret_bytes

#: Dedicated HMAC domain for account deletion references. MUST stay distinct
#: from the message/network/turn-correlation/user-reference domains so a
#: persisted reference can never be correlated with USER_REFERENCE_DOMAIN
#: values or any other purpose.
ACCOUNT_DELETION_HMAC_DOMAIN = b"account-deletion"

#: Ledger statuses (must match the database CHECK constraint).
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
ACCOUNT_DELETION_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_PROCESSING, STATUS_COMPLETED, STATUS_FAILED}
)

#: Sanitized error codes used by the SQL boundary.
ERROR_VALIDATION_FAILED = "validation_failed"
ERROR_OPERATION_CONFLICT = "operation_conflict"
ERROR_STATE_CONFLICT = "state_conflict"
ERROR_ATTEMPTS_EXHAUSTED = "attempts_exhausted"

#: Deterministic exhaustion ceiling (mirrors the SQL constant): a failed
#: job whose attempts reach this ceiling is terminal (next_attempt_at NULL)
#: and never claimed automatically again.
ACCOUNT_DELETION_MAX_ATTEMPTS = 100

#: Lowercase hex patterns (same as the database).
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")


def compute_account_deletion_user_ref(secret_bytes: bytes, user_id: object) -> str:
    """Compute the persistent, non-reversible HMAC reference for a user.

    Domain-separated from every other HMAC purpose (dedicated
    ``account-deletion`` domain), so the persisted reference can never be
    correlated with ``USER_REFERENCE_DOMAIN`` values. Returns the exact
    lowercase 64-character hex HMAC. The raw ``user_id`` is never logged.
    """
    validated_secret = _validate_secret_bytes(secret_bytes)
    if not isinstance(user_id, str) or not user_id or not user_id.strip():
        raise ValidationError("invalid_user_id", "user_id is invalid")
    return hmac.new(
        validated_secret,
        ACCOUNT_DELETION_HMAC_DOMAIN + b"\x00" + user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def compute_intent_fingerprint(payload: Mapping[str, Any]) -> str:
    """Deterministic SHA-256 of the canonical JSON intent payload.

    Mirrors the SQL helper ``account_deletion_intent_fingerprint_sha256``:
    canonical ``json.dumps`` with sorted keys, compact separators, matching
    the canonical ``jsonb::text`` serialization used by the database for
    object payloads. Returns lowercase hex (64 chars).
    """
    if not isinstance(payload, Mapping):
        raise ValidationError("invalid_intent_payload", "intent payload must be a JSON object")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ─── Strict envelope parsers ────────────────────────────────────────────────


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PersistenceError("database_error", "malformed retention response")
    return value


def _require_fields(
    envelope: Mapping[str, Any],
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str = "",
) -> None:
    """Require exactly *required* plus optionally *optional* fields.

    PostgREST omits JSON null values from responses, so optional nullable
    fields may be absent and are parsed as None.
    """
    keys = set(envelope)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise PersistenceError("database_error", "malformed retention response")


def _require_text(envelope: Mapping[str, Any], key: str) -> str:
    value = envelope.get(key)
    if not isinstance(value, str):
        raise PersistenceError("database_error", "malformed retention response")
    return value


def _require_uuid_text(envelope: Mapping[str, Any], key: str) -> str:
    value = _require_text(envelope, key)
    if not _UUID_RE.match(value):
        raise PersistenceError("database_error", "malformed retention response")
    return value


def _require_hex64(envelope: Mapping[str, Any], key: str) -> str:
    value = _require_text(envelope, key)
    if not _HEX64_RE.match(value):
        raise PersistenceError("database_error", "malformed retention response")
    return value


def _optional_text(envelope: Mapping[str, Any], key: str) -> Optional[str]:
    value = envelope.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PersistenceError("database_error", "malformed retention response")
    return value


def _require_int(envelope: Mapping[str, Any], key: str) -> int:
    value = envelope.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PersistenceError("database_error", "malformed retention response")
    return value


def _parse_error_envelope(envelope: Mapping[str, Any]) -> "AccountDeletionError":
    if set(envelope) != frozenset({"error"}):
        raise PersistenceError("database_error", "malformed retention response")
    error = _require_mapping(envelope["error"], "error")
    if set(error) != frozenset({"code", "message"}):
        raise PersistenceError("database_error", "malformed retention response")
    code = _require_text(error, "code")
    message = _require_text(error, "message")
    return AccountDeletionError(code=code, message=message)


# ─── Domain results ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AccountDeletionError:
    """Structured, sanitized error returned by the SQL boundary."""

    code: str
    message: str


@dataclass(frozen=True)
class RequestResult:
    """Outcome of ``account_deletion_request``."""

    status: str  # 'created' | 'replay'
    job_id: str
    job_status: str
    db_purged_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass(frozen=True)
class TombstoneStatus:
    """Outcome of ``account_deletion_has_tombstone``."""

    exists: bool
    status: Optional[str]


@dataclass(frozen=True)
class AcquiredJob:
    """One job claimed by a worker via ``account_deletion_acquire_lease``."""

    job_id: str
    user_id: str
    user_ref_hmac_sha256: str
    operation_id: str
    status: str
    lease_owner: str
    lease_expires_at: str
    attempts: int
    db_purged_at: Optional[str]
    intent_fingerprint_sha256: str


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of ``account_deletion_purge``."""

    status: str  # 'purged' | 'already_purged'
    job_id: str
    db_purged_at: str
    counts: Mapping[str, int]


@dataclass(frozen=True)
class FailureResult:
    """Outcome of ``account_deletion_record_failure``."""

    status: str
    job_id: str
    error_code: str
    next_attempt_at: str


@dataclass(frozen=True)
class RetryResult:
    """Outcome of ``account_deletion_record_retry``."""

    status: str
    job_id: str
    next_attempt_at: str


@dataclass(frozen=True)
class FinalizeResult:
    """Outcome of ``account_deletion_finalize``."""

    status: str
    job_id: str
    completed_at: str
    db_purged_at: str


# ─── Parsers ────────────────────────────────────────────────────────────────


def _parse_request_result(response: object) -> RequestResult:
    envelope = _require_mapping(response, "request result")
    if "error" in envelope:
        error = _parse_error_envelope(envelope)
        if error.code == ERROR_OPERATION_CONFLICT:
            raise ConflictError(ERROR_OPERATION_CONFLICT, error.message, 0)
        raise ValidationError(ERROR_VALIDATION_FAILED, error.message)
    _require_fields(
        envelope,
        frozenset({"status", "job_id", "job_status"}),
        frozenset({"db_purged_at", "completed_at"}),
        "request result",
    )
    status = _require_text(envelope, "status")
    if status not in ("created", "replay"):
        raise PersistenceError("database_error", "malformed retention response")
    job_status = _require_text(envelope, "job_status")
    if job_status not in ACCOUNT_DELETION_STATUSES:
        raise PersistenceError("database_error", "malformed retention response")
    return RequestResult(
        status=status,
        job_id=_require_uuid_text(envelope, "job_id"),
        job_status=job_status,
        db_purged_at=_optional_text(envelope, "db_purged_at"),
        completed_at=_optional_text(envelope, "completed_at"),
    )


def _parse_tombstone_result(response: object) -> TombstoneStatus:
    envelope = _require_mapping(response, "tombstone result")
    if "error" in envelope:
        error = _parse_error_envelope(envelope)
        raise ValidationError(ERROR_VALIDATION_FAILED, error.message)
    _require_fields(envelope, frozenset({"exists"}), frozenset({"status"}), label="tombstone result")
    exists = envelope.get("exists")
    if not isinstance(exists, bool):
        raise PersistenceError("database_error", "malformed retention response")
    status = envelope.get("status")
    if status is not None and (
        not isinstance(status, str) or status not in ACCOUNT_DELETION_STATUSES
    ):
        raise PersistenceError("database_error", "malformed retention response")
    return TombstoneStatus(exists=exists, status=status)


def _parse_acquired_job(
    response: object, expected_user_id: Optional[str] = None
) -> Optional[AcquiredJob]:
    """Parse one ``account_deletion_acquire_lease`` result.

    ``{"found": false}`` is the NOMINAL empty-queue contract (#325): no
    eligible job exists and the caller must not treat that as a persistence
    failure. Returns ``None``. Any other payload shape (including a
    ``found:false`` envelope carrying extra fields) is malformed and fails
    closed with ``PersistenceError``.
    """
    envelope = _require_mapping(response, "acquire result")
    if "error" in envelope:
        error = _parse_error_envelope(envelope)
        raise ValidationError(ERROR_VALIDATION_FAILED, error.message)
    if envelope.get("found") is False:
        if set(envelope) != frozenset({"found"}):
            raise PersistenceError("database_error", "malformed retention response")
        return None
    _require_fields(
        envelope,
        frozenset(
            {
                "found",
                "job_id",
                "user_id",
                "user_ref_hmac_sha256",
                "operation_id",
                "status",
                "lease_owner",
                "lease_expires_at",
                "attempts",
                "intent_fingerprint_sha256",
            }
        ),
        frozenset({"db_purged_at"}),
        "acquire result",
    )
    found = envelope.get("found")
    if not isinstance(found, bool) or found is not True:
        raise PersistenceError("database_error", "malformed retention response")
    user_id = _require_text(envelope, "user_id")
    if expected_user_id is not None and user_id != expected_user_id:
        # Never echo the divergent identity: fail closed sanitized.
        raise PersistenceError("database_error", "identity mismatch")
    status = _require_text(envelope, "status")
    if status != STATUS_PROCESSING:
        raise PersistenceError("database_error", "malformed retention response")
    return AcquiredJob(
        job_id=_require_uuid_text(envelope, "job_id"),
        user_id=user_id,
        user_ref_hmac_sha256=_require_hex64(envelope, "user_ref_hmac_sha256"),
        operation_id=_require_uuid_text(envelope, "operation_id"),
        status=status,
        lease_owner=_require_text(envelope, "lease_owner"),
        lease_expires_at=_require_text(envelope, "lease_expires_at"),
        attempts=_require_int(envelope, "attempts"),
        db_purged_at=_optional_text(envelope, "db_purged_at"),
        intent_fingerprint_sha256=_require_hex64(envelope, "intent_fingerprint_sha256"),
    )


def _parse_purge_result(response: object) -> PurgeResult:
    envelope = _require_mapping(response, "purge result")
    if "error" in envelope:
        error = _parse_error_envelope(envelope)
        if error.code == ERROR_OPERATION_CONFLICT:
            raise ConflictError(ERROR_OPERATION_CONFLICT, error.message, 0)
        raise ValidationError(ERROR_VALIDATION_FAILED, error.message)
    _require_fields(envelope, frozenset({"status", "job_id", "db_purged_at", "counts"}), label="purge result")
    status = _require_text(envelope, "status")
    if status not in ("purged", "already_purged"):
        raise PersistenceError("database_error", "malformed retention response")
    counts = _require_mapping(envelope["counts"], "purge counts")
    expected_counts = frozenset(
        {
            "outbox_events",
            "turn_requests",
            "archival_extractions",
            "memories",
            "chat_logs",
            "admission_reservations",
            "privacy_operations",
            "profiles",
        }
    )
    if set(counts) != expected_counts:
        raise PersistenceError("database_error", "malformed retention response")
    parsed_counts = {key: _require_int(counts, key) for key in expected_counts}
    return PurgeResult(
        status=status,
        job_id=_require_uuid_text(envelope, "job_id"),
        db_purged_at=_require_text(envelope, "db_purged_at"),
        counts=parsed_counts,
    )


def _parse_failure_result(response: object) -> FailureResult:
    envelope = _require_mapping(response, "failure result")
    if "error" in envelope:
        error = _parse_error_envelope(envelope)
        raise ValidationError(ERROR_VALIDATION_FAILED, error.message)
    _require_fields(
        envelope,
        frozenset({"status", "job_id", "error_code"}),
        frozenset({"next_attempt_at"}),
        label="failure result",
    )
    if _require_text(envelope, "status") != "failed":
        raise PersistenceError("database_error", "malformed retention response")
    error_code = _require_text(envelope, "error_code")
    if not _ERROR_CODE_RE.match(error_code):
        raise PersistenceError("database_error", "malformed retention response")
    # next_attempt_at is NULL for a TERMINAL exhausted job (PostgREST may
    # send the field as null or omit it).
    return FailureResult(
        status="failed",
        job_id=_require_uuid_text(envelope, "job_id"),
        error_code=error_code,
        next_attempt_at=_optional_text(envelope, "next_attempt_at"),
    )


def _parse_retry_result(response: object) -> RetryResult:
    envelope = _require_mapping(response, "retry result")
    if "error" in envelope:
        error = _parse_error_envelope(envelope)
        raise ValidationError(ERROR_VALIDATION_FAILED, error.message)
    # Normal deferral: back to pending with a scheduled next_attempt_at.
    if envelope.get("status") == "retry_scheduled":
        _require_fields(
            envelope,
            frozenset({"status", "job_id", "next_attempt_at"}),
            label="retry result",
        )
        return RetryResult(
            status="retry_scheduled",
            job_id=_require_uuid_text(envelope, "job_id"),
            next_attempt_at=_require_text(envelope, "next_attempt_at"),
        )
    # Exhausted deferral: the attempts ceiling was reached, the job became
    # a terminal failed job (next_attempt_at NULL, absent from the
    # PostgREST response).
    if envelope.get("status") == "failed" and envelope.get("error_code") == ERROR_ATTEMPTS_EXHAUSTED:
        _require_fields(
            envelope,
            frozenset({"status", "job_id", "error_code"}),
            frozenset({"next_attempt_at"}),
            label="retry result",
        )
        return RetryResult(
            status="failed",
            job_id=_require_uuid_text(envelope, "job_id"),
            next_attempt_at=None,
        )
    raise PersistenceError("database_error", "malformed retention response")


def _parse_finalize_result(response: object) -> FinalizeResult:
    envelope = _require_mapping(response, "finalize result")
    if "error" in envelope:
        error = _parse_error_envelope(envelope)
        if error.code == ERROR_STATE_CONFLICT:
            raise ConflictError(ERROR_OPERATION_CONFLICT, error.message, 0)
        raise ValidationError(ERROR_VALIDATION_FAILED, error.message)
    _require_fields(envelope, frozenset({"status", "job_id", "completed_at", "db_purged_at"}), label="finalize result")
    if _require_text(envelope, "status") != "completed":
        raise PersistenceError("database_error", "malformed retention response")
    return FinalizeResult(
        status="completed",
        job_id=_require_uuid_text(envelope, "job_id"),
        completed_at=_require_text(envelope, "completed_at"),
        db_purged_at=_require_text(envelope, "db_purged_at"),
    )


def _parse_int_result(response: object) -> int:
    if isinstance(response, bool) or not isinstance(response, int) or response < 0:
        raise PersistenceError("database_error", "malformed retention response")
    return response


# ─── Repository boundary ────────────────────────────────────────────────────


class AccountDeletionRepository:
    """Synchronous RPC contract for the account deletion ledger.

    All methods are thread-bound and never awaited directly. Responses are
    strictly validated; malformed payloads and divergent identities fail
    closed with sanitized ``PersistenceError``.
    """

    def request(
        self,
        authenticated_user_id: str,
        user_ref_hmac_sha256: str,
        operation_id: str,
        intent_fingerprint_sha256: str,
    ) -> RequestResult: ...

    def has_tombstone(self, user_ref_hmac_sha256: str) -> TombstoneStatus: ...

    def acquire_lease(
        self, worker_id: str, lease_seconds: int, max_batch: int
    ) -> Optional[AcquiredJob]:
        """Claim one eligible job, or ``None`` when the queue is empty.

        ``None`` (from the RPC ``found:false`` envelope) is the nominal
        empty-queue result (#325), never a persistence failure.
        """
        ...

    def purge(
        self,
        job_id: str,
        worker_id: str,
        intent_fingerprint_sha256: str,
        expected_user_id: Optional[str] = None,
    ) -> PurgeResult: ...

    def record_failure(self, job_id: str, worker_id: str, error_code: str) -> FailureResult: ...

    def record_retry(self, job_id: str, worker_id: str) -> RetryResult: ...

    def finalize(self, job_id: str, worker_id: str) -> FinalizeResult: ...

    def purge_completed(self, cutoff: str, batch_size: int) -> int: ...


def _rpc_call(
    client: Any,
    rpc_name: str,
    params: Mapping[str, Any],
) -> Any:
    if client is None:
        raise PersistenceError("database_error", "persistence error")
    try:
        return client.rpc(rpc_name, params).execute().data
    except PersistenceError:
        raise
    except Exception:
        raise PersistenceError("database_error", "persistence error") from None


class SupabaseAccountDeletionRepository:
    """Synchronous adapter over ``client.rpc(name, params).execute()``.

    Upstream exceptions (which may carry connection details, identifiers or
    payload content) are never surfaced: everything maps to a sanitized
    ``PersistenceError``. Results are parsed fail-closed by the strict
    parsers above.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def request(
        self,
        authenticated_user_id: str,
        user_ref_hmac_sha256: str,
        operation_id: str,
        intent_fingerprint_sha256: str,
    ) -> RequestResult:
        data = _rpc_call(
            self._client,
            "account_deletion_request",
            {
                "p_authenticated_user_id": authenticated_user_id,
                "p_user_ref_hmac_sha256": user_ref_hmac_sha256,
                "p_operation_id": operation_id,
                "p_intent_fingerprint_sha256": intent_fingerprint_sha256,
            },
        )
        return _parse_request_result(data)

    def has_tombstone(self, user_ref_hmac_sha256: str) -> TombstoneStatus:
        data = _rpc_call(
            self._client,
            "account_deletion_has_tombstone",
            {"p_user_ref_hmac_sha256": user_ref_hmac_sha256},
        )
        return _parse_tombstone_result(data)

    def acquire_lease(
        self,
        worker_id: str,
        lease_seconds: int,
        max_batch: int,
        expected_user_id: Optional[str] = None,
    ) -> Optional[AcquiredJob]:
        data = _rpc_call(
            self._client,
            "account_deletion_acquire_lease",
            {
                "p_worker_id": worker_id,
                "p_lease_seconds": lease_seconds,
                "p_max_batch": max_batch,
            },
        )
        return _parse_acquired_job(data, expected_user_id=expected_user_id)

    def purge(
        self,
        job_id: str,
        worker_id: str,
        intent_fingerprint_sha256: str,
        expected_user_id: Optional[str] = None,
    ) -> PurgeResult:
        data = _rpc_call(
            self._client,
            "account_deletion_purge",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_intent_fingerprint_sha256": intent_fingerprint_sha256,
            },
        )
        return _parse_purge_result(data)

    def record_failure(self, job_id: str, worker_id: str, error_code: str) -> FailureResult:
        data = _rpc_call(
            self._client,
            "account_deletion_record_failure",
            {
                "p_job_id": job_id,
                "p_worker_id": worker_id,
                "p_error_code": error_code,
            },
        )
        return _parse_failure_result(data)

    def record_retry(self, job_id: str, worker_id: str) -> RetryResult:
        data = _rpc_call(
            self._client,
            "account_deletion_record_retry",
            {"p_job_id": job_id, "p_worker_id": worker_id},
        )
        return _parse_retry_result(data)

    def finalize(self, job_id: str, worker_id: str) -> FinalizeResult:
        data = _rpc_call(
            self._client,
            "account_deletion_finalize",
            {"p_job_id": job_id, "p_worker_id": worker_id},
        )
        return _parse_finalize_result(data)

    def purge_completed(self, cutoff: str, batch_size: int) -> int:
        data = _rpc_call(
            self._client,
            "account_deletion_purge_completed",
            {"p_cutoff": cutoff, "p_batch_size": batch_size},
        )
        return _parse_int_result(data)
