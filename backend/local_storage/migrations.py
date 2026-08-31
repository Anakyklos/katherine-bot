"""Ordered, versioned SQLite schema migrations (#335).

Each migration is ``(version, sql)`` with a strict positive integer
version. Migrations are:

- **ordered** — applied strictly in ascending version order;
- **idempotent to re-run** — applied versions are recorded in
  ``schema_migrations`` and skipped on later opens;
- **atomic** — each migration's DDL runs inside one transaction together
  with the insertion of its version row: a migration that fails in the
  middle is neither applied nor recorded (SQLite rolls the transaction
  back). A later open retries it from scratch.

Migrations live in code (a frozen list), not in loose SQL files: no new
dependency, deterministic ordering, and the exact text is reviewable in
one place. New schema changes append a new tuple — existing migrations
are never edited.
"""

from __future__ import annotations

_SCHEMA_MIGRATIONS_TABLE = """
create table if not exists schema_migrations (
    version integer primary key,
    applied_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

#: Migration 1 — the initial local-first schema (single user, no
#: ``user_id`` columns, no RLS/ACL equivalents: the database file itself
#: is the trust boundary on the user's own machine).
_MIGRATION_0001 = """
create table profiles (
    id integer primary key check (id = 1),
    persona_config text,
    user_profile text not null default '{}',
    emotional_state text not null default '{}',
    relationship_state text not null default '{}',
    revision integer not null default 0 check (revision >= 0),
    updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

create table chat_logs (
    id integer primary key autoincrement,
    role text not null check (role in ('user', 'assistant')),
    content text not null,
    created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index chat_logs_created_idx on chat_logs (created_at, id);

create table turn_requests (
    request_id text primary key,
    payload_hash_sha256 text not null,
    status text not null
        check (status in ('pending', 'completed', 'failed')),
    expected_revision integer not null default 0,
    committed_revision integer,
    user_message_chat_log_id integer,
    assistant_message_chat_log_id integer,
    replay_payload text,
    error_code text,
    created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at text,
    foreign key (user_message_chat_log_id) references chat_logs (id)
        on delete set null,
    foreign key (assistant_message_chat_log_id) references chat_logs (id)
        on delete set null
);
create index turn_requests_status_idx on turn_requests (status);

create table outbox_events (
    id text primary key,
    event_type text not null,
    idempotency_key text not null unique,
    status text not null default 'pending'
        check (status in ('pending', 'completed', 'dead_letter')),
    turn_request_id text not null
        references turn_requests (request_id),
    payload text not null check (json_valid(payload)),
    created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    processed_at text
);
create index outbox_events_status_idx on outbox_events (status);

create table memories (
    id integer primary key autoincrement,
    content text not null,
    metadata text not null check (json_valid(metadata)),
    created_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
create index memories_created_idx on memories (created_at);

create table privacy_operations (
    id integer primary key autoincrement,
    operation text not null
        check (operation in ('delete_history', 'delete_memories',
                             'reset_emotional_state', 'reset_relationship_state')),
    status text not null check (status = 'applied'),
    result text not null check (json_valid(result)),
    applied_at text not null default (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

#: The ordered migration list. Append-only by convention.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _MIGRATION_0001),
)

#: SQL that guarantees the bookkeeping table exists before any version
#: is recorded. Runs outside migration transactions, is itself
#: idempotent (``if not exists``), and never fails partially.
BOOTSTRAP_SQL = _SCHEMA_MIGRATIONS_TABLE


def pending_versions(applied: set[int]) -> list[int]:
    """Return the not-yet-applied versions in ascending order."""
    return [version for version, _ in MIGRATIONS if version not in applied]


def migration_sql(version: int) -> str:
    """Return the SQL of one migration (KeyError for unknown versions)."""
    for known_version, sql in MIGRATIONS:
        if known_version == version:
            return sql
    raise KeyError(f"unknown migration version: {version}")
