"""CORS origin allowlist parsing for the API.

Pure module without dependencies: ``parse_cors_allowed_origins`` converts
the ``CORS_ALLOWED_ORIGINS`` environment variable into the origin tuple used
by the middleware. Failures never include the raw input.
"""

from __future__ import annotations

DEFAULT_ALLOWED_ORIGINS = ("http://localhost:3000",)


def parse_cors_allowed_origins(raw: str | None) -> tuple[str, ...]:
    """Parse the CORS origin allowlist from a raw environment value.

    - ``None`` (variable absent) preserves the legacy default.
    - Empty or whitespace-only input raises ``ValueError``: production must
      fail fast instead of silently disabling CORS.
    - A ``*`` wildcard entry raises ``ValueError``: credentials are enabled,
      and a wildcard with credentials is not an acceptable production
      configuration.
    - Entries are trimmed and deduplicated, preserving first-seen order.

    Args:
        raw: Raw value of ``CORS_ALLOWED_ORIGINS``, or ``None`` if unset.

    Returns:
        A tuple of allowed origins (never empty).

    Raises:
        ValueError: For empty/whitespace input or a ``*`` wildcard entry.
    """
    if raw is None:
        return DEFAULT_ALLOWED_ORIGINS

    entries = [entry.strip() for entry in raw.split(",")]
    if not any(entries):
        raise ValueError("CORS_ALLOWED_ORIGINS must not be empty")

    if any(entry == "*" for entry in entries):
        raise ValueError("CORS_ALLOWED_ORIGINS must not contain a wildcard")

    seen: dict[str, None] = {}
    for entry in entries:
        if entry:
            seen.setdefault(entry, None)
    return tuple(seen)
