"""Versioned operational data retention policy (#316).

Pure Python, no network, no client construction at import. This module is
the versioned source of truth for the retention *policy*: which operational
categories are covered, their horizons, and the eligibility rules. The SQL
boundary (``supabase/migrations/20260809030000_operational_data_retention.sql``)
enforces the same contracts fail-closed, and cross-boundary tests pin both
sides.

Scope
=====
The policy covers ONLY operational/transitory data:

* ``admission_reservations`` — rows older than the 24h horizon are eligible
  for purge; rows inside the horizon stay. ``delete_history`` continues to
  never touch this ledger, so cleanup cannot bypass quota.
* ``privacy_operations`` — applied #314 ledger rows older than the 30-day
  horizon are eligible; rows inside the horizon stay, preserving the
  replay/idempotency/conflict semantics.
* ``outbox_events`` — only FINAL states (``completed``, ``dead_letter``)
  whose ``retention_until`` has passed are eligible. Active states
  (``pending``, ``processing``, ``failed``) are never purged by age.

User-controlled data (``chat_logs``, ``memories``, ``archival_extractions``,
``profiles`` snapshots) and the replay/history ledger (``turn_requests``)
are deliberately OUTSIDE the policy: no automatic TTL.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import Enum
from typing import Optional

#: Schema version of the retention policy registry. Bumped only when the
#: policy semantics change in a way that requires coordinated migration.
RETENTION_POLICY_SCHEMA_VERSION = 1

#: Maximum batch size accepted by the purge SQL boundary (fail closed above).
RETENTION_MAX_BATCH_SIZE = 1000

#: Outbox states eligible for age-based purge (final states only).
OUTBOX_FINAL_STATUSES = frozenset({"completed", "dead_letter"})

#: Outbox states that must NEVER be purged by age alone.
OUTBOX_ACTIVE_STATUSES = frozenset({"pending", "processing", "failed"})

#: The three operational categories covered by the policy.
#: Values match the purge RPC names in the migration.
class RetentionCategory(str, Enum):
    ADMISSION_RESERVATIONS = "admission_reservations"
    PRIVACY_OPERATIONS = "privacy_operations"
    OUTBOX_EVENTS = "outbox_events"


#: Default horizon: rows with reserved_at older than this are eligible.
DEFAULT_ADMISSION_HORIZON = _dt.timedelta(hours=24)

#: Default horizon: applied privacy ledger rows older than this are eligible.
DEFAULT_PRIVACY_OPERATIONS_HORIZON = _dt.timedelta(days=30)


@dataclass(frozen=True)
class RetentionPolicy:
    """Versioned retention policy registry.

    ``schema_version`` pins the exact semantics of this policy. Horizons are
    explicit and immutable after construction.

    ``outbox_events`` has NO age horizon: eligibility is defined by the final
    state + ``retention_until`` semantics (enforced in SQL), so it carries no
    timedelta here.
    """

    schema_version: int = RETENTION_POLICY_SCHEMA_VERSION
    admission_reservations_horizon: _dt.timedelta = DEFAULT_ADMISSION_HORIZON
    privacy_operations_horizon: _dt.timedelta = DEFAULT_PRIVACY_OPERATIONS_HORIZON


def default_retention_policy() -> RetentionPolicy:
    """Return the canonical v1 retention policy."""
    return RetentionPolicy()


def compute_cutoff(
    now_epoch: float,
    horizon: _dt.timedelta,
) -> _dt.datetime:
    """Return the UTC-aware purge cutoff for *now_epoch* minus *horizon*.

    A row is eligible when its timestamp is strictly BEFORE the cutoff.
    """
    return _dt.datetime.fromtimestamp(now_epoch, tz=_dt.timezone.utc) - horizon


def retention_cutoffs(
    now_epoch: float,
    policy: Optional[RetentionPolicy] = None,
) -> dict[str, str]:
    """Compute the ISO-8601 purge cutoffs for every covered category.

    ``admission_reservations``: now - 24h.
    ``privacy_operations``: now - 30d.
    ``outbox_events``: now (``retention_until`` must already be past).

    Returns a mapping keyed by :class:`RetentionCategory` value with
    ``datetime.isoformat()`` strings ready for the purge RPCs.
    """
    effective = policy if policy is not None else default_retention_policy()
    return {
        RetentionCategory.ADMISSION_RESERVATIONS.value: compute_cutoff(
            now_epoch, effective.admission_reservations_horizon
        ).isoformat(),
        RetentionCategory.PRIVACY_OPERATIONS.value: compute_cutoff(
            now_epoch, effective.privacy_operations_horizon
        ).isoformat(),
        RetentionCategory.OUTBOX_EVENTS.value: _dt.datetime.fromtimestamp(
            now_epoch, tz=_dt.timezone.utc
        ).isoformat(),
    }
