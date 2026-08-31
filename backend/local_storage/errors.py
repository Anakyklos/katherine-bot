"""Error contracts for the local storage foundation (#335).

The three stable domain errors mirror the web contract names in
``backend/atomic_turn_commit.py`` so callers can reuse their existing
error handling: ``ConflictError`` (concurrent modification), 
``ValidationError`` (invalid input) and ``PersistenceError`` (unexpected
store failure, sanitized constant message). ``StorageCorruptError``
extends ``PersistenceError`` with an explicit corruption code: the store
is never silently reset (issue #335, test 9).

No exception ever carries SQL text, file paths, driver details, raw
message content or tracebacks: messages are stable constants.
"""

from __future__ import annotations


class ConflictError(Exception):
    """Concurrent modification detected (revision CAS mismatch)."""

    __slots__ = ("code", "message", "expected_revision", "actual_revision", "request_id")

    def __init__(
        self,
        code: str,
        message: str,
        expected_revision: int,
        actual_revision: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision
        self.request_id = request_id

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class ValidationError(Exception):
    """Invalid input detected before any write."""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class PersistenceError(Exception):
    """Unexpected store failure (sanitized constant message)."""

    __slots__ = ("code", "message")

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class StorageCorruptError(PersistenceError):
    """The database file is corrupt; recovery is never a silent reset.

    Raised by ``open_local_storage`` when SQLite reports corruption.
    The corrupted file is left untouched: automatic recreation would
    destroy the user's data without consent.
    """

    __slots__ = ()
