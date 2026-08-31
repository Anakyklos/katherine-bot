"""SQLite local persistence foundation for the Katherine desktop app (#335).

Public contract (the only names importers should rely on):

- ``LocalStorage`` — the store facade: open, migrate, commit a turn
  atomically, load state/history, replay, privacy operations, trim,
  backup, metrics, close.
- ``open_local_storage(path)`` — open (creating if needed) and migrate a
  database file. The connection lifecycle belongs to the application.
- ``default_database_path(env)`` — XDG-compliant default location with an
  injectable environment mapping for tests.
- ``StorageCorruptError`` — explicit corruption signal; the store is
  never silently reset.

Design invariants (mirrors the PostgreSQL contract, local-first):

1. **Atomic turn commit.** Messages, profile revision CAS, turn ledger
   row and outbox rows are written inside one ``BEGIN IMMEDIATE``
   transaction. Any failure rolls the whole turn back — messages and
   snapshots can never diverge after a crash.
2. **Revision CAS.** ``profiles.revision`` is the optimistic-concurrency
   token: a commit with a stale ``expected_revision`` raises
   ``ConflictError("revision_mismatch")`` and persists nothing.
3. **Versioned, ordered, idempotent migrations** recorded in
   ``schema_migrations``. A migration that fails partially leaves no
   trace of completion (each migration runs in its own transaction and
   its version row is only inserted after its DDL succeeds).
4. **Foreign keys enforced** on every connection; integrity is real,
   not advisory.
5. **Explicit durability policy**: WAL journaling + ``synchronous=FULL``
   (documented in ``docs/architecture/local-sqlite-persistence.md``).
6. **Serialization policy for the single process**: one writer lock per
   store; writers take ``BEGIN IMMEDIATE`` before the first write so
   lock upgrades cannot fail mid-transaction; readers use per-thread
   connections and WAL gives them a consistent committed snapshot.
7. **Sanitized errors.** Public errors are stable code+message pairs.
   SQL text, paths, raw content and driver details never cross the API.
8. **No cloud imports.** The package imports only stdlib sqlite3/JSON —
   importing it never pulls Supabase/PostgREST/HTTP stacks.

Removed versus the web schema (each removal documented in the issue
#335 invariant table): multi-user ``user_id`` columns, RLS, service-role
grants, distributed advisory locks, identity HMAC, and lease/claim
machinery (single process ⇒ the writer lock + pending→failed recovery
policy replace distributed leases).
"""

from __future__ import annotations

from . import migrations
from .storage import (
    ConflictError,
    LocalStorage,
    PersistenceError,
    StorageCorruptError,
    ValidationError,
    default_database_path,
    open_local_storage,
)

__all__ = [
    "ConflictError",
    "LocalStorage",
    "PersistenceError",
    "StorageCorruptError",
    "ValidationError",
    "default_database_path",
    "migrations",
    "open_local_storage",
]
