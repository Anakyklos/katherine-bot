"""
Pure-domain module for provider input envelope estimation, validation,
and context pruning.

This module depends only on the Python standard library and on
``backend.admission_contracts`` (for the 16 000-unit constant).  It must
be importable without:

- FastAPI or Pydantic
- Groq SDK
- Supabase / PostgREST
- sentence_transformers
- ConversationEngine, memory, or engine
- environment variables
- network or filesystem access
- clock or randomness

Usage::

    from backend.provider_envelope import (
        estimate_provider_input_units,
        validate_provider_input,
        fit_optional_context,
        ProviderEnvelopeError,
        OMISSION_MARKER,
    )

    validate_provider_input([{"role": "user", "content": "Hi"}])
    validate_provider_input(large_messages, max_units=16000)
    pruned = fit_optional_context(mandatory, optional_components)
"""

from __future__ import annotations

import json

from .admission_contracts import PROVIDER_INPUT_MAX_ESTIMATED_UNITS, estimate_text_units


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_ROLES: frozenset = frozenset({"system", "user", "assistant"})
"""Set of valid message roles."""

VALID_MESSAGE_KEYS: frozenset = frozenset({"role", "content"})
"""Set of valid keys in a message dict."""

OMISSION_MARKER = "[...]"
"""Constant marker appended when an individual string value is truncated."""


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------

class ProviderEnvelopeError(Exception):
    """Validation failure for provider input envelopes.

    Carries only structured fields — never exposes raw content, prompts,
    UUIDs, users, IP addresses, HMACs, or secrets in ``str()``, ``repr()``,
    or public attributes.
    """

    def __init__(
        self,
        code: str,
        *,
        actual_units: int = 0,
        max_units: int = 0,
    ) -> None:
        self.code = code
        self.actual_units = actual_units
        self.max_units = max_units
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"ProviderEnvelopeError(code={self.code!r})"


# ---------------------------------------------------------------------------
# Canonical estimation
# ---------------------------------------------------------------------------

def estimate_provider_input_units(messages: list) -> int:
    """Estimate the input cost of a logical messages list.

    Uses a deterministic representation equivalent to::

        json.dumps(messages, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"))

    then applies the UTF-8 byte count (the same algorithm used by
    ``estimate_text_units``).  The estimate includes:

    * order of messages
    * roles
    * contents
    * key names
    * JSON structure (braces, brackets, colons, commas)
    * escaping (backslashes, quotes)
    * UTF-8 bytes

    Returns at least ``1`` for any input (including a list with an
    empty message).

    Raises ``ProviderEnvelopeError`` if *messages* is not a list.
    """
    if not isinstance(messages, list):
        raise ProviderEnvelopeError("invalid_envelope_type")

    try:
        serialized = json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise ProviderEnvelopeError("serialization_failed")

    return estimate_text_units(serialized)


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------

def validate_provider_input(
    messages: list,
    max_units: int = PROVIDER_INPUT_MAX_ESTIMATED_UNITS,
) -> None:
    """Validate the structural integrity and budget of a messages list.

    Fails closed when:

    * the collection is empty or not a list
    * an item is not a valid mapping (dict)
    * a message contains an unknown role
    * ``content`` is not a string
    * there are unknown keys in the currently supported format
    * the estimated input units exceed *max_units*

    Exceptions are typed and sanitised — they never contain raw content,
    prompts, UUIDs, or secrets.

    Raises ``ProviderEnvelopeError`` on failure; returns ``None`` on success.
    """
    # Collection checks
    if not isinstance(messages, list):
        raise ProviderEnvelopeError("invalid_envelope_type")
    if len(messages) == 0:
        raise ProviderEnvelopeError("empty_messages")

    for i, msg in enumerate(messages):
        # Item must be a dict
        if not isinstance(msg, dict):
            raise ProviderEnvelopeError(
                "invalid_message_structure",
            )

        # No unknown keys
        unknown_keys = set(msg.keys()) - VALID_MESSAGE_KEYS
        if unknown_keys:
            raise ProviderEnvelopeError(
                "invalid_message_keys",
            )

        # Role must be valid
        role = msg.get("role")
        if role not in VALID_ROLES:
            raise ProviderEnvelopeError(
                "invalid_role",
            )

        # Content must be a string
        content = msg.get("content")
        if not isinstance(content, str):
            raise ProviderEnvelopeError(
                "invalid_content",
            )

    # Budget check
    units = estimate_provider_input_units(messages)
    if units > max_units:
        raise ProviderEnvelopeError(
            "budget_exceeded",
            actual_units=units,
            max_units=max_units,
        )


# ---------------------------------------------------------------------------
# Context pruning
# ---------------------------------------------------------------------------

def _truncate_utf8_safe(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate *text* to fit within *max_bytes* UTF-8 bytes.

    Preserves the **beginning** of the text.  If truncation is applied,
    completes the last full UTF-8 character and appends ``OMISSION_MARKER``.

    Returns ``(truncated_text, was_truncated)``.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False

    # Account for omission marker space
    marker_bytes = len(OMISSION_MARKER.encode("utf-8"))
    available = max(max_bytes - marker_bytes, 1)

    # Decode up to *available* bytes, handling partial characters
    truncated = encoded[:available].decode("utf-8", errors="ignore")
    return truncated + OMISSION_MARKER, True


def _truncate_utf8_safe_head_tail(
    text: str,
    max_bytes: int,
    head_ratio: float = 0.6,
) -> tuple[str, bool]:
    """Truncate *text* preserving both **beginning** and **end**.

    If truncation is needed, the text is split into a head portion
    (first *head_ratio* fraction of available bytes) and a tail portion
    (the remainder), with ``OMISSION_MARKER`` inserted between them.

    ``head_ratio`` controls the split between head and tail (default 0.6 = 60% head).
    Both portions are decoded at full UTF-8 character boundaries.
    If *max_bytes* is too small for both head and tail plus the marker,
    falls back to head-only truncation (``_truncate_utf8_safe`` semantics).

    Returns ``(truncated_text, was_truncated)``.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False

    marker_bytes = len(OMISSION_MARKER.encode("utf-8"))

    # Need space for head + marker + tail
    if max_bytes < marker_bytes + 4:
        # Too small for meaningful head+tail — fall back to head-only
        return _truncate_utf8_safe(text, max_bytes)

    total_data = max_bytes - marker_bytes
    head_bytes = max(1, int(total_data * head_ratio))
    tail_bytes = max(1, total_data - head_bytes)

    # Head: beginning of text, up to head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")

    # Tail: end of text, last tail_bytes
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")

    # If combined head + OMISSION_MARKER + tail still exceeds max_bytes,
    # try reducing tail (tail is less important for structure)
    result = head + OMISSION_MARKER + tail
    while len(result.encode("utf-8")) > max_bytes and len(tail) > 0:
        # Remove one character from tail at a time
        tail = tail[:-1]
        result = head + OMISSION_MARKER + tail

    return result, True



def fit_optional_context(
    mandatory_messages: list,
    optional_context_components: list[tuple[str, str]],
    max_units: int = PROVIDER_INPUT_MAX_ESTIMATED_UNITS,
    truncate_long_messages: bool = False,
) -> list:
    """Build a messages list that fits within *max_units*.

    ``mandatory_messages``
        List of message dicts that **must** be included.  These represent
        the current user message, all safety/identity rules, emotional
        state, relationship state, acting instruction, and any other
        fixed prompt sections.  They are never truncated.

    ``optional_context_components``
        List of ``(section_label, section_content)`` tuples in **priority
        order** (highest priority first).  All sections are included until
        the budget is exhausted.  Section labels are only used for
        observability — they are not included in the messages list.

    ``max_units``
        Maximum estimated input units for the final messages list.
        Defaults to ``PROVIDER_INPUT_MAX_ESTIMATED_UNITS`` (16 000).

    ``truncate_long_messages``
        If ``True``, truncate individual optional component contents
        that would exceed the remaining budget.  This is used for
        archival extraction where a single legacy message may be too
        large.  When ``False``, entire components are included or
        excluded as atomic units.

    Returns:
        A new messages list that fits within *max_units*.

    Raises:
        ``ProviderEnvelopeError("budget_exceeded")``
        if the mandatory messages alone exceed *max_units*.

    The selection of optional context follows this priority order
    (highest first), which matches the caller's ordering:

    1. identity / persona contextual
    2. recent history (most recent first)
    3. relevant memories
    4. serialised user profile

    Invariants:

    * Mandatory messages are never modified.
    * The returned list is a new list; the input is not mutated.
    * Optional components are included as atomic units unless
      *truncate_long_messages* is ``True``.
    * UTF-8 sequences are never split.
    * No randomness is used — the output is deterministic.
    """
    # Validate mandatory messages first
    validate_provider_input(mandatory_messages, max_units=max_units)

    # Start with mandatory messages
    current_messages = list(mandatory_messages)

    if not optional_context_components:
        return current_messages

    # We need to check if we can add optional components.
    # The optional components are content strings that will be added
    # into the system message. Work with the system message content
    # for each addition.
    system_content_index: int | None = None
    for i, msg in enumerate(current_messages):
        if msg.get("role") == "system":
            system_content_index = i
            break

    if system_content_index is None:
        # No system message — optional components can't be added
        return current_messages

    for label, content in optional_context_components:
        if not content:
            continue

        if truncate_long_messages:
            # Try with full content first
            pass

        # Tentatively add this component to the system message
        test_messages = list(current_messages)
        original_content = test_messages[system_content_index]["content"]
        test_messages[system_content_index] = {
            "role": "system",
            "content": original_content + "\n" + content,
        }

        try:
            validate_provider_input(test_messages, max_units=max_units)
            # Fits — keep it
            current_messages = test_messages
        except ProviderEnvelopeError:
            if not truncate_long_messages:
                # Doesn't fit — try next (lower priority) component
                continue
            else:
                # Try truncated version
                remaining = _estimate_remaining_budget(
                    current_messages, max_units
                )
                if remaining <= 0:
                    continue
                truncated, _ = _truncate_utf8_safe(content, remaining)
                if truncated != content:
                    test_truncated = list(current_messages)
                    test_truncated[system_content_index] = {
                        "role": "system",
                        "content": original_content + "\n" + truncated,
                    }
                    try:
                        validate_provider_input(
                            test_truncated, max_units=max_units
                        )
                        current_messages = test_truncated
                    except ProviderEnvelopeError:
                        pass

    return current_messages


def _estimate_remaining_budget(
    messages: list,
    max_units: int,
) -> int:
    """Estimate remaining budget before hitting *max_units*."""
    units = estimate_provider_input_units(messages)
    return max(0, max_units - units)
