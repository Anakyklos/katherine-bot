"""Minimal, sanitized observability contract for the backend.

This module defines constant event names and a single ``emit_event`` helper
that renders structured log lines like::

    event=turn_completed code=ok duration_ms=1234 correlation=<hmac>

Guarantees:

* Event names are constants from a closed registry (low cardinality).
* Field names are allowlisted; forbidden names (``user_id``, ``message``,
  ``response``, ``prompt``, ``memory``, ``token``, ``secret``, ...) are
  rejected at the call site, so content can never reach a log by accident.
* Values are restricted to ``str``/``int``/``float``/``bool``; string values
  are scrubbed of control characters and collapsed whitespace to prevent log
  injection.
* No ``logging.basicConfig`` here and no global state.

Raw user identifiers are never logged. When correlation is needed, the
admission runtime provides an HMAC-SHA256 reference under a dedicated domain
(see ``backend.admission.compute_turn_correlation``); this module never
computes hashes itself and never accepts raw identifiers as fields.
"""

from __future__ import annotations

import logging
import re
from typing import Any

# ─── Event names (closed registry) ─────────────────────────────────────────

EVENT_APP_STARTUP_FAILED = "app_startup_failed"
EVENT_APP_SHUTDOWN_FAILED = "app_shutdown_failed"
EVENT_SUPABASE_CLIENT_CREATION_FAILED = "supabase_client_creation_failed"
EVENT_READINESS_CHECK_FAILED = "readiness_check_failed"
EVENT_AUTH_FAILED = "auth_failed"
EVENT_AUTH_COMPLETED = "auth_completed"
EVENT_TURN_COMPLETED = "turn_completed"
EVENT_TURN_FAILED = "turn_failed"
EVENT_REQUEST_CONFLICT = "request_conflict"
EVENT_HTTP_RESULT = "http_result"
EVENT_RETENTION_COMPLETED = "retention_completed"
EVENT_RETENTION_FAILED = "retention_failed"
EVENT_ACCOUNT_DELETION_WORKER_STARTED = "account_deletion_worker_started"
EVENT_ACCOUNT_DELETION_NO_WORK = "account_deletion_no_work"
EVENT_ACCOUNT_DELETION_DB_PURGED = "account_deletion_db_purged"
EVENT_ACCOUNT_DELETION_AUTH_DELETED = "account_deletion_auth_deleted"
EVENT_ACCOUNT_DELETION_AUTH_ALREADY_ABSENT = "account_deletion_auth_already_absent"
EVENT_ACCOUNT_DELETION_RETRY_SCHEDULED = "account_deletion_retry_scheduled"
EVENT_ACCOUNT_DELETION_FAILED = "account_deletion_failed"
EVENT_ACCOUNT_DELETION_COMPLETED = "account_deletion_completed"
EVENT_ACCOUNT_DELETION_LEASE_LOST = "account_deletion_lease_lost"
EVENT_ACCOUNT_DELETION_REQUESTED = "account_deletion_requested"
EVENT_ACCOUNT_DELETION_BLOCKED = "account_deletion_blocked"
EVENT_ACCOUNT_DELETION_GATE_UNAVAILABLE = "account_deletion_gate_unavailable"

EVENT_NAMES = frozenset(
    {
        EVENT_APP_STARTUP_FAILED,
        EVENT_APP_SHUTDOWN_FAILED,
        EVENT_SUPABASE_CLIENT_CREATION_FAILED,
        EVENT_READINESS_CHECK_FAILED,
        EVENT_AUTH_FAILED,
        EVENT_AUTH_COMPLETED,
        EVENT_TURN_COMPLETED,
        EVENT_TURN_FAILED,
        EVENT_REQUEST_CONFLICT,
        EVENT_HTTP_RESULT,
        EVENT_RETENTION_COMPLETED,
        EVENT_RETENTION_FAILED,
        EVENT_ACCOUNT_DELETION_WORKER_STARTED,
        EVENT_ACCOUNT_DELETION_NO_WORK,
        EVENT_ACCOUNT_DELETION_DB_PURGED,
        EVENT_ACCOUNT_DELETION_AUTH_DELETED,
        EVENT_ACCOUNT_DELETION_AUTH_ALREADY_ABSENT,
        EVENT_ACCOUNT_DELETION_RETRY_SCHEDULED,
        EVENT_ACCOUNT_DELETION_FAILED,
        EVENT_ACCOUNT_DELETION_COMPLETED,
        EVENT_ACCOUNT_DELETION_LEASE_LOST,
        EVENT_ACCOUNT_DELETION_REQUESTED,
        EVENT_ACCOUNT_DELETION_BLOCKED,
        EVENT_ACCOUNT_DELETION_GATE_UNAVAILABLE,
    }
)

# ─── Field contract ─────────────────────────────────────────────────────────

#: Allowed field names for event payloads. Low-cardinality, sanitized values
#: only (codes, phases, durations, HTTP statuses, HMAC correlations, and the
#: non-reversible HMAC user reference ``user_ref``).
ALLOWED_FIELDS = frozenset(
    {
        "correlation",
        "user_ref",
        "code",
        "stage",
        "outcome",
        "phase",
        "duration_ms",
        "latency_ms",
        "attempt",
        "http_status",
        "component",
        "mode",
        "reason",
        "result",
        "retry",
        "conflict",
        "deadline_ms",
    }
)

#: Field names that are never accepted, even if a future caller insists.
#: Content and identifiers must not reach logs.
FORBIDDEN_FIELDS = frozenset(
    {
        "user_id",
        "request_id",
        "message",
        "response",
        "prompt",
        "memory",
        "appraisal",
        "snapshot",
        "token",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "key",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ObservabilityContractError(ValueError):
    """Raised when an event violates the sanitized observability contract.

    This is a programmer error: it fails fast instead of silently logging
    content that must never be emitted.
    """


def sanitize_value(value: Any) -> str:
    """Render an event field value safely.

    Accepts only ``str``, ``int``, ``float`` (finite), and ``bool``. Strings
    have control characters removed and whitespace runs collapsed, so
    multi-line or injected payloads cannot break log framing.
    """
    if isinstance(value, str):
        return _CONTROL_RE.sub("", _WHITESPACE_RE.sub(" ", value)).strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            raise ObservabilityContractError("event field values must be finite")
        return f"{value:.0f}"
    raise ObservabilityContractError(
        f"event field values must be str/int/float/bool, got {type(value).__name__}"
    )


def emit_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emit one sanitized structured event line.

    Args:
        logger: Logger to emit on.
        event: One of the constant names in :data:`EVENT_NAMES`.
        level: Logging level (``logging.INFO`` or ``logging.ERROR``).
        **fields: Allowlisted, sanitized payload fields.

    Raises:
        ObservabilityContractError: For unknown events, unknown field names,
            forbidden field names, or unsupported value types. Failing fast
            prevents content from ever reaching a log.
    """
    if event not in EVENT_NAMES:
        raise ObservabilityContractError(f"unknown event name: {event!r}")

    parts = [f"event={event}"]
    for name, value in fields.items():
        if name in FORBIDDEN_FIELDS:
            raise ObservabilityContractError(
                f"field {name!r} is forbidden in event payloads"
            )
        if name not in ALLOWED_FIELDS:
            raise ObservabilityContractError(f"unknown event field: {name!r}")
        if value is None:
            # Absent optional fields are simply omitted from the line.
            continue
        parts.append(f"{name}={sanitize_value(value)}")

    logger.log(level, " ".join(parts))


def emit_error(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a sanitized event at ERROR level."""
    emit_event(logger, event, level=logging.ERROR, **fields)
