"""Application boundary for the #326 authenticated account deletion API.

This module is the thin, stateless application frontier between the
authenticated HTTP layer (``backend.main``) and the #324 Python contract
(``backend.account_deletion``). It deliberately contains no FastAPI routing,
no per-user state, and no re-implementation of the #324 ledger semantics:
every operation delegates to ``AccountDeletionRepository`` through the
existing operational write helper, and the HTTP layer only maps domain
exceptions to sanitized status codes.

Components
==========

* ``AccountDeletionRequestResponse`` — the explicit public projection of a
  ``RequestResult``. It exposes ONLY ``status`` (``accepted`` or
  ``completed``). It never exposes ``user_id``, ``operation_id``, ``job_id``,
  HMACs, timestamps, upstream errors or SQL. ``completed`` is returned only
  when the ledger itself confirms completion (``job_status == completed``
  with a persisted ``db_purged_at``); the API never promises completion
  before the #325 worker finishes.
* ``AccountDeletionService`` — a stateless, process-wide application service.
  The authenticated identity and the operation_id are per-call arguments; no
  reference, fingerprint or identifier is retained between requests. The
  server-side HMAC reference is derived exclusively from
  ``current_user.id`` through ``compute_account_deletion_user_ref`` using
  the existing admission secret (dedicated ``account-deletion`` HMAC
  domain). The intent fingerprint is deterministic: a constant
  ``delete_account`` payload with no user data, so the same
  (user, operation_id, intent) is an exact ledger replay.
* ``assert_active`` — the single reusable, fail-closed gate for normal
  account actions. Any anomaly (persistence failure, timeout, malformed
  payload, unavailable store, invalid derived reference) raises
  ``AccountDeletionUnavailable`` (HTTP 503) and never allows the route to
  continue; a present tombstone raises ``AccountDeletionBlocked``
  (HTTP 423 ``account_deletion_pending``). Routes apply this gate through
  exactly one helper in ``backend.main``, never by copying the tombstone
  logic.

Ownership and testability
=========================

* The service is stateless and may live in the application container.
  Identity and operation_id are always per-call arguments.
* The repository is injectable (fakes can record or stub RPC calls), the
  operational ``TurnExecutionConfig`` is injectable (budget/timeout
  behavior is configurable), and the admission configuration is injectable
  (the HMAC secret). Tests inject fakes without Supabase/Groq/embeddings.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from backend.account_deletion import (
    STATUS_COMPLETED,
    AccountDeletionRepository,
    TombstoneStatus,
    compute_account_deletion_user_ref,
    compute_intent_fingerprint,
)
from backend.atomic_turn_commit import (
    ConflictError,
    PersistenceError,
    ValidationError,
)
from backend.turn_execution import (
    DeadlineExceeded,
    TurnExecutionConfig,
    TurnExecutionError,
    create_budget,
    run_blocking_write,
)

#: Stable, low-cardinality intent payload for account deletion. Deliberately
#: contains NO ``user_id``, ``operation_id``, timestamps or variable data:
#: the fingerprint must be deterministic so the same user + operation_id is
#: an exact ledger replay and never a second job.
DELETE_ACCOUNT_INTENT = {"op": "delete_account", "scope": ["db", "auth"]}

#: Sanitized stage labels used by the write helper for every account deletion
#: RPC (constant, low cardinality, never carries identifiers).
_REQUEST_STAGE = "account_deletion_request"
_TOMBSTONE_STAGE = "account_deletion_tombstone"


class AccountDeletionBlocked(Exception):
    """A deletion tombstone exists; normal account actions must be blocked.

    Mapped by the HTTP layer to ``423 account_deletion_pending``. The
    exception never carries the user identity, the reference, the status or
    any internal detail.
    """

    code = "account_deletion_pending"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "AccountDeletionBlocked()"


class AccountDeletionUnavailable(Exception):
    """Fail-closed: the tombstone store could not be consulted safely.

    Mapped by the HTTP layer to a sanitized 503. A failure to consult the
    tombstone is NEVER interpreted as "user active".
    """

    code = "service_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)

    def __repr__(self) -> str:
        return "AccountDeletionUnavailable()"


class AccountDeletionRequestResponse(BaseModel):
    """Public projection of an account deletion request.

    Exposes ONLY ``status``:

    * ``accepted`` — the ledger registered (or replayed) the request and the
      deletion is pending/in progress; the tombstone blocks normal actions.
    * ``completed`` — returned ONLY when the ledger confirms completion
      (``job_status == completed`` with a persisted ``db_purged_at``).

    Identity, operation_id, job_id, HMACs, internal timestamps, upstream
    errors and SQL never appear here.
    """

    status: str


class AccountDeletionService:
    """Stateless application service for account deletion (#326).

    The service holds no per-user state: the authenticated identity and the
    operation_id are constructed per call. The server-side HMAC reference is
    derived from ``admission_config.secret_bytes`` under the dedicated
    ``account-deletion`` domain, the intent fingerprint is deterministic,
    and every blocking RPC is dispatched through ``run_blocking_write``
    (operational budget + Supabase transport timeout, drain-on-cancellation,
    no orphaned tasks).
    """

    def __init__(
        self,
        *,
        repository: AccountDeletionRepository,
        turn_config: TurnExecutionConfig,
        admission_config: Any,
    ) -> None:
        self._repository = repository
        self._turn_config = turn_config
        self._admission_config = admission_config

    # ─── Public frontier ─────────────────────────────────────────────────────

    async def request(
        self, authenticated_user_id: str, operation_id: str
    ) -> AccountDeletionRequestResponse:
        """Register (or replay) an account deletion request for the identity.

        The identity comes exclusively from the authenticated user; the HMAC
        reference and the intent fingerprint are derived server-side. The
        same user + operation_id + intent is an exact ledger replay (no
        second job); a divergent intent raises ``ConflictError``
        (``operation_conflict``) for the HTTP layer to map to 409.
        """
        budget = create_budget(self._turn_config)
        user_ref = compute_account_deletion_user_ref(
            self._admission_config.secret_bytes, authenticated_user_id
        )
        fingerprint = compute_intent_fingerprint(DELETE_ACCOUNT_INTENT)
        result = await run_blocking_write(
            _REQUEST_STAGE,
            budget,
            self._turn_config.supabase_timeout,
            self._repository.request,
            authenticated_user_id,
            user_ref,
            operation_id,
            fingerprint,
            allowlist_exceptions=(ConflictError, ValidationError, PersistenceError),
        )
        return AccountDeletionRequestResponse(
            status=self._project_status(result.job_status, result.db_purged_at)
        )

    async def assert_active(self, authenticated_user_id: str) -> None:
        """Fail-closed tombstone gate for normal account actions.

        Consults ``AccountDeletionRepository.has_tombstone`` through the
        operational write helper. Raises:

        * ``AccountDeletionBlocked`` when a tombstone exists (pending,
          processing, failed, or completed while still within retention);
        * ``AccountDeletionUnavailable`` when the store cannot be consulted
          (persistence failure, timeout, malformed payload, invalid derived
          reference) — the caller must never treat that as "user active".

        This is the SINGLE reusable boundary; routes apply it through exactly
        one helper in ``backend.main`` and never copy the tombstone logic.
        """
        budget = create_budget(self._turn_config)
        try:
            user_ref = compute_account_deletion_user_ref(
                self._admission_config.secret_bytes, authenticated_user_id
            )
            status = await run_blocking_write(
                _TOMBSTONE_STAGE,
                budget,
                self._turn_config.supabase_timeout,
                self._repository.has_tombstone,
                user_ref,
                allowlist_exceptions=(ValidationError, PersistenceError),
            )
        except (
            DeadlineExceeded,
            TurnExecutionError,
            ValidationError,
            PersistenceError,
        ):
            raise AccountDeletionUnavailable() from None
        if status.exists:
            raise AccountDeletionBlocked()

    # ─── Projection ─────────────────────────────────────────────────────────

    @staticmethod
    def _project_status(job_status: str, db_purged_at: Optional[str]) -> str:
        """Project the ledger result into the public status vocabulary.

        ``completed`` requires the ledger to confirm completion (finalize
        happened with a persisted purge marker). Everything else is the
        honest ``accepted`` state: created/replayed, pending/processing/
        failed, or even completed-without-a-purge-marker (a contract anomaly
        that must never be presented as completion).
        """
        if job_status == STATUS_COMPLETED and db_purged_at is not None:
            return "completed"
        return "accepted"
