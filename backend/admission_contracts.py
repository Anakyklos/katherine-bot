"""
Pure admission contracts for request identity, input estimation, and message validation.

This module is the single source of truth for admission limits and provides
pure, I/O-free validation functions.  It uses only the Python standard library
and must be importable without:

- FastAPI
- Pydantic
- Groq SDK
- Supabase / PostgREST
- sentence_transformers
- ConversationEngine, memory, or engine
- environment variables
- network or filesystem access
- clock or randomness

Usage::

    from backend.admission_contracts import (
        RequestIdentity,
        estimate_text_units,
        validate_new_message,
        AdmissionConfig,
    )

    identity = RequestIdentity.parse("550e8400-e29b-41d4-a716-446655440000")
    units = estimate_text_units("Hello, world!")
    validate_new_message("Hello!")   # raises AdmissionError on failure
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Constants — single source of truth for admission limits               #285
# ---------------------------------------------------------------------------

NEW_MESSAGE_MAX_CHARS = 4000
"""Maximum character count for a single new user message."""

LEGACY_HISTORY_MAX_CHARS = 10000
"""Maximum character count for legacy history (from ``backend/memory.py``).
Not altered here — preserved as a separate contract."""

MESSAGE_MAX_ESTIMATED_UNITS = 6000
"""Maximum estimated input units for a single new user message."""

PROVIDER_INPUT_MAX_ESTIMATED_UNITS = 16000
"""Maximum estimated input units for the full provider call (reserved)."""

USER_REQUESTS_PER_MINUTE = 20
"""Maximum user requests per minute (reserved)."""

NETWORK_REQUESTS_PER_MINUTE = 60
"""Maximum network-level requests per minute (reserved)."""

APPLICATION_REQUESTS_PER_MINUTE = 25
"""Maximum application-level requests per minute (reserved)."""

USER_REQUESTS_PER_DAY = 200
"""Maximum user requests per calendar day (reserved)."""

USER_ESTIMATED_UNITS_PER_DAY = 250000
"""Maximum estimated input units per user per day (reserved)."""


@dataclass(frozen=True)
class AdmissionConfig:
    """Immutable, user-independent admission configuration.

    All attributes are frozen at runtime.  No mutable state, no environment
    fallback, no per-user overrides.
    """

    new_message_max_chars: int = NEW_MESSAGE_MAX_CHARS
    legacy_history_max_chars: int = LEGACY_HISTORY_MAX_CHARS
    message_max_estimated_units: int = MESSAGE_MAX_ESTIMATED_UNITS
    provider_input_max_estimated_units: int = PROVIDER_INPUT_MAX_ESTIMATED_UNITS

    user_requests_per_minute: int = USER_REQUESTS_PER_MINUTE
    network_requests_per_minute: int = NETWORK_REQUESTS_PER_MINUTE
    application_requests_per_minute: int = APPLICATION_REQUESTS_PER_MINUTE
    user_requests_per_day: int = USER_REQUESTS_PER_DAY
    user_estimated_units_per_day: int = USER_ESTIMATED_UNITS_PER_DAY


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

class AdmissionError(Exception):
    """Validation failure for admission contracts.

    Carries only structured fields — never exposes raw content, UUIDs, users,
    or IP addresses in ``str()``, ``repr()``, or public attributes.
    """

    def __init__(
        self,
        code: str,
        *,
        actual_chars: int = 0,
        actual_units: int = 0,
        max_chars: int = 0,
        max_units: int = 0,
    ) -> None:
        self.code = code
        self.actual_chars = actual_chars
        self.actual_units = actual_units
        self.max_chars = max_chars
        self.max_units = max_units
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"AdmissionError(code={self.code!r})"


# ---------------------------------------------------------------------------
# Request identity
# ---------------------------------------------------------------------------

def _is_hex(s: str) -> bool:
    """Return ``True`` if *s* contains only ASCII hex digits ``[0-9a-f]``."""
    for ch in s:
        if ch not in "0123456789abcdef":
            return False
    return True


def _validate_canonical_uuid(raw: str) -> str:
    """Validate and normalise a canonical UUID string.

    Returns the lowercased canonical form on success.
    Raises ``AdmissionError(code="invalid_request_id")`` on any failure.
    The invalid input is never exposed in the exception.
    """
    # Must be exactly 36 characters (8-4-4-4-12 with hyphens)
    if len(raw) != 36:
        raise AdmissionError(code="invalid_request_id")

    # No leading / trailing whitespace
    if raw != raw.strip():
        raise AdmissionError(code="invalid_request_id")

    # Reject {uuid} format (e.g. ``{550e8400-...}``)
    if raw.startswith("{") or raw.endswith("}"):
        raise AdmissionError(code="invalid_request_id")

    # Reject ``urn:uuid:...`` prefix
    if raw.lower().startswith("urn:"):
        raise AdmissionError(code="invalid_request_id")

    # Split into 5 hyphen-separated parts
    parts = raw.split("-")
    if len(parts) != 5:
        raise AdmissionError(code="invalid_request_id")

    # Lengths must be 8-4-4-4-12
    expected_lengths = (8, 4, 4, 4, 12)
    for part, expected in zip(parts, expected_lengths):
        if len(part) != expected:
            raise AdmissionError(code="invalid_request_id")

    # Normalise to lowercase and verify hex
    normalized = raw.lower()
    for part in normalized.split("-"):
        if not _is_hex(part):
            raise AdmissionError(code="invalid_request_id")

    return normalized


@dataclass(frozen=True)
class RequestIdentity:
    """Immutable, validated request identity.

    The ``.request_id`` attribute holds the normalised canonical UUID string
    (lowercase, with hyphens).  No UUID is generated automatically, and
    the identity is not associated with any user.

    Usage::

        identity = RequestIdentity.parse("550e8400-e29b-41d4-a716-446655440000")
        assert identity.request_id == "550e8400-e29b-41d4-a716-446655440000"
    """

    request_id: str

    @staticmethod
    def parse(raw: str) -> RequestIdentity:
        """Parse and validate a request ID from a raw string.

        Raises ``AdmissionError(code="invalid_request_id")`` if the value
        is not a valid canonical UUID.  The invalid value is **never**
        exposed in the exception, its ``str()``, ``repr()``, or any public
        attribute.
        """
        if not isinstance(raw, str):
            raise AdmissionError(code="invalid_request_id")
        normalized = _validate_canonical_uuid(raw)
        return RequestIdentity(request_id=normalized)


# ---------------------------------------------------------------------------
# Input estimation
# ---------------------------------------------------------------------------

def estimate_text_units(text: str) -> int:
    """Estimate the input cost of *text* as the number of UTF-8 bytes.

    Returns at least ``1`` for any input (including empty strings).
    Does **not** truncate, normalise Unicode, log, call external services,
    or accept non-string types.

    Raises ``TypeError`` for non-string arguments.
    """
    if not isinstance(text, str):
        raise TypeError("estimate_text_units expects a str")
    return max(1, len(text.encode("utf-8")))


# ---------------------------------------------------------------------------
# Message validation
# ---------------------------------------------------------------------------

def validate_new_message(text: str) -> None:
    """Validate a new user message against character and unit limits.

    Precedence (deterministic):

    1. Invalid type or ``len(text) > NEW_MESSAGE_MAX_CHARS`` →
       ``AdmissionError(code="message_too_long")``.
    2. ``estimate_text_units(text) > MESSAGE_MAX_ESTIMATED_UNITS`` →
       ``AdmissionError(code="message_budget_exceeded")``.

    Raises ``AdmissionError`` on failure; returns ``None`` on success.
    """
    if not isinstance(text, str):
        raise AdmissionError(
            code="message_too_long",
            actual_chars=0,
            max_chars=NEW_MESSAGE_MAX_CHARS,
        )

    char_count = len(text)
    if char_count > NEW_MESSAGE_MAX_CHARS:
        raise AdmissionError(
            code="message_too_long",
            actual_chars=char_count,
            max_chars=NEW_MESSAGE_MAX_CHARS,
        )

    unit_count = estimate_text_units(text)
    if unit_count > MESSAGE_MAX_ESTIMATED_UNITS:
        raise AdmissionError(
            code="message_budget_exceeded",
            actual_chars=char_count,
            actual_units=unit_count,
            max_chars=NEW_MESSAGE_MAX_CHARS,
            max_units=MESSAGE_MAX_ESTIMATED_UNITS,
        )
