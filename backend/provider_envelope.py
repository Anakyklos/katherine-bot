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
import logging

from .admission_contracts import PROVIDER_INPUT_MAX_ESTIMATED_UNITS, estimate_text_units


logger = logging.getLogger(__name__)


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
    *,
    suffix: str = "",
    selection_priority: list[int] | None = None,
) -> list:
    """Build a messages list that fits within *max_units*.

    ``mandatory_messages``
        List of message dicts that **must** be included.  These represent
        the current user message, all safety/identity rules, emotional
        state, relationship state, acting instruction, and any other
        fixed prompt sections.  They are never truncated.

    ``optional_context_components``
        List of ``(section_label, section_content)`` tuples in **visual
        order** (the order they should appear in the final system prompt).
        All sections whose combined content + suffix fits within *max_units*
        (when added to mandatory messages) are included.  Sections that
        would exceed the budget are excluded atomically.

    ``max_units``
        Maximum estimated input units for the final messages list.
        Defaults to ``PROVIDER_INPUT_MAX_ESTIMATED_UNITS`` (16 000).

    ``truncate_long_messages``
        If ``True``, truncate individual optional component contents
        that would exceed the remaining budget.  This is used for
        archival extraction where a single legacy message may be too
        large.  When ``False``, entire components are included or
        excluded as atomic units.

    ``suffix``
        Optional mandatory suffix that **must** be appended to the system
        content after optional components.  The budget calculation includes
        this suffix during each selection check, ensuring optional context
        never pushes the final envelope over budget.

    ``selection_priority``
        Optional list of indices into *optional_context_components*
        indicating the **selection priority order** (highest priority
        first).  When provided, components are tried for budget inclusion
        in this priority order, but rendered in the original
        *optional_context_components* visual order.

        If ``None`` (default), components are tried in the same order
        they appear in *optional_context_components* (no separation
        between selection and visual order).

    Returns:
        A new messages list that fits within *max_units*.
        If *suffix* is provided, it is appended to the system message
        content before being returned.

    Raises:
        ``ProviderEnvelopeError("budget_exceeded")``
        if the mandatory messages alone exceed *max_units*.

    Invariants:

    * Mandatory messages are never modified.
    * The returned list is a new list; the input is not mutated.
    * Optional components are included as atomic units unless
      *truncate_long_messages* is ``True``.
    * The suffix (if provided) is included in every budget check.
    * Components are rendered in the order they appear in
      *optional_context_components* (visual order).
    * UTF-8 sequences are never split.
    * No randomness is used — the output is deterministic.
    """
    # Validate mandatory messages first
    validate_provider_input(mandatory_messages, max_units=max_units)

    # Start with mandatory messages
    current_messages = list(mandatory_messages)

    if not optional_context_components:
        # Append suffix to system message if provided
        if suffix:
            _append_to_system(current_messages, "\n\n" + suffix)
            validate_provider_input(current_messages, max_units=max_units)
        return current_messages

    # Find system message index
    system_content_index: int | None = None
    for i, msg in enumerate(current_messages):
        if msg.get("role") == "system":
            system_content_index = i
            break

    if system_content_index is None:
        # No system message — optional components can't be added
        if suffix:
            logger.warning("Cannot append suffix: no system message in mandatory messages.")
        return current_messages

    # Pre-compute the suffix payload once
    suffix_payload = "\n\n" + suffix if suffix else ""

    # Determine selection order: use selection_priority if provided,
    # otherwise use the visual order (list order)
    if selection_priority is not None:
        _validate_selection_priority(selection_priority, optional_context_components)
        selection_order = selection_priority
    else:
        selection_order = list(range(len(optional_context_components)))

    # Track which components (by index) have been selected
    selected_indices: set[int] = set()

    # Get the mandatory header content (before any optional components)
    mandatory_header = current_messages[system_content_index]["content"]

    for idx in selection_order:
        label, content = optional_context_components[idx]
        if not content:
            continue

        # Tentatively add this component
        selected_indices.add(idx)

        # Build candidate from SCRATCH: mandatory header + all selected components (visual order) + suffix
        candidate_components = [
            optional_context_components[i]
            for i in range(len(optional_context_components))
            if i in selected_indices
        ]

        built_content = mandatory_header
        for _, comp_content in candidate_components:
            built_content += "\n" + comp_content
        built_content += suffix_payload

        test_messages = list(current_messages)
        test_messages[system_content_index] = {
            "role": "system",
            "content": built_content,
        }

        try:
            validate_provider_input(test_messages, max_units=max_units)
            # Fits — keep it (without suffix in stored content)
            current_messages = test_messages
            if suffix:
                current_messages[system_content_index] = {
                    "role": "system",
                    "content": built_content[:-len(suffix_payload)] if suffix_payload else built_content,
                }
        except ProviderEnvelopeError:
            # Doesn't fit — remove from selected
            selected_indices.discard(idx)
            if not truncate_long_messages:
                continue
            else:
                # Try truncated version
                remaining = _estimate_remaining_budget(
                    current_messages, max_units, suffix_payload
                )
                if remaining <= 0:
                    continue
                truncated, _ = _truncate_utf8_safe(content, remaining)
                if truncated != content:
                    selected_indices.add(idx)
                    candidate_components = [
                        (optional_context_components[i][0],
                         truncated if i == idx else optional_context_components[i][1])
                        for i in range(len(optional_context_components))
                        if i in selected_indices
                    ]
                    built_content = mandatory_header
                    for _, comp_content in candidate_components:
                        built_content += "\n" + comp_content
                    built_content += suffix_payload

                    test_truncated = list(current_messages)
                    test_truncated[system_content_index] = {
                        "role": "system",
                        "content": built_content,
                    }
                    try:
                        validate_provider_input(
                            test_truncated, max_units=max_units
                        )
                        current_messages = test_truncated
                        if suffix:
                            current_messages[system_content_index] = {
                                "role": "system",
                                "content": built_content[:-len(suffix_payload)] if suffix_payload else built_content,
                            }
                    except ProviderEnvelopeError:
                        selected_indices.discard(idx)

    # Append suffix to final system content
    if suffix:
        current_messages = _append_to_system(current_messages, suffix_payload)
        validate_provider_input(current_messages, max_units=max_units)

    return current_messages


def _validate_selection_priority(
    priority: list[int],
    components: list[tuple[str, str]],
) -> None:
    """Validate that *priority* covers all component indices exactly once."""
    if sorted(priority) != list(range(len(components))):
        raise ValueError(
            "selection_priority must contain each component index exactly once"
        )


def _estimate_remaining_budget(
    messages: list,
    max_units: int,
    suffix_payload: str = "",
) -> int:
    """Estimate remaining budget before hitting *max_units*.

    If *suffix_payload* is provided, its estimated units are subtracted
    from the remaining budget so that suffix space is reserved.
    """
    units = estimate_provider_input_units(messages)
    suffix_units = estimate_text_units(suffix_payload) if suffix_payload else 0
    return max(0, max_units - units - suffix_units)


def _append_to_system(messages: list, payload: str) -> list:
    """Append *payload* to the system message content in *messages*.

    Returns a new list; does not mutate the input.
    Raises ``ProviderEnvelopeError`` if no system message exists.
    """
    result = list(messages)
    for i, msg in enumerate(result):
        if msg.get("role") == "system":
            result[i] = {
                "role": "system",
                "content": msg["content"] + payload,
            }
            return result
    raise ProviderEnvelopeError("no_system_message")
