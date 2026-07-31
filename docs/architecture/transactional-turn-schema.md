# Transactional Turn Schema — Foundation

## Status

Approved for issue #270 (recovery #264, tracking #269). This document is the
architectural record for the persistence foundation that the future atomic
commit flow (issue #271) and the outbox worker will build on.

## Problem

Today a turn writes messages and snapshots in separate, uncoordinated
operations:

- No persisted revision allows lost-update detection between concurrent
  writes to a user's state.
- No durable structure exists to claim a `request_id`, distinguish pending /
  completed / expired requests, replay a response after connection loss, or
  prevent persistent duplication of requests and outbox events.
- No outbox table can host events published atomically with a future turn
  commit.

Bad schema decisions here would propagate concurrency, privacy and
idempotency risk through the whole chain (#271, #272). This task therefore
creates **only** schema, constraints, minimal serialization contracts,
documentation, and real database tests. No active `ConversationEngine` flow
is wired to the new objects.

## Scope boundaries

| Deliberately NOT done | Where it lands |
|---|---|
| `commit_turn` / full claim-reclaim | #271 |
| `ConversationEngine` integration | #271 |
| Outbox worker | #272 |
| `save_turn()` / `sync_state()` changes | out of scope |
| `backend/trusted_context.py`, prompts, emotional/relationship domains | out of scope |
| Redis or any external coordinator | rejected — PostgreSQL-only |

---

## 1. Revision strategy

`profiles.revision` is a `bigint NOT NULL DEFAULT 0` optimistic-concurrency
counter.

- **Monotonic per user.** Each committed turn bumps the user's revision by 1.
- **Default 0 and deterministic backfill.** `ADD COLUMN ... NOT NULL DEFAULT 0`
  backfills every existing profile with 0 in a single metadata-only
  operation (no table rewrite for existing rows in modern PostgreSQL), so
  the backfill is deterministic and lossless.
- **Upsert compatible.** Future `ON CONFLICT` upserts that omit the column
  start at 0 because of the default.
- **Fail-closed.** `CHECK (revision >= 0)` rejects negative values; the
  migration preflight fails loudly if the column already exists (drift).

### `updated_at` policy

`profiles.updated_at` is **application-maintained**: the backend refreshes it
explicitly when it writes state. No trigger is introduced here — adding one
would change the active write path, which is out of scope. The future atomic
commit flow must refresh `updated_at` in the same statement that bumps
`revision`.

---

## 2. `turn_requests` states

| Status | Meaning | Required shape |
|---|---|---|
| `pending` | Claimed by a worker, work in progress | `lease_owner` + `lease_expires_at` set; `completed_at`/`committed_revision`/`replay_payload`/`error_code` NULL |
| `completed` | Turn committed durably | `completed_at`, `committed_revision`, `replay_payload` set; lease and error cleared |
| `expired` | Lease expired or abandoned | error_code set; no completion data; no lease |

The `turn_requests_status_coherence_check` constraint makes every state fully
determined — a row cannot be half-pending, half-completed. This is the
fail-closed guarantee that prevents "stuck" requests.

---

## 3. Lease semantics and expiration

- A `pending` request carries `lease_owner` (sanitized worker identifier,
  max 64 chars, `^[A-Za-z0-9_.:-]+$` — enforced by CHECK on both
  `turn_requests` and `outbox_events`) and `lease_expires_at`.
- The **lease pair check** enforces both-or-neither: a lease can never be
  half-written.
- Expiration is **time-based only** — no background timer. The future worker
  reclaims rows where `status = 'pending' AND lease_expires_at < now()` by
  atomically transitioning them to `expired` with a sanitized
  `error_code` (e.g. `lease_expired`).
- Reclaim must be done with an atomic conditional update (compare-and-swap
  on `lease_expires_at`), serialized by the future per-user transaction
  lock; this task does not implement it.

---

## 4. Canonical payload hash

`payload_hash_sha256` is the **canonical hash of the request payload** used
to detect duplicate/conflicting requests.

- Computed by the application as SHA-256 over the **canonical JSON
  serialization** of the payload: `json.dumps(payload, sort_keys=True,
  separators=(",", ":"), ensure_ascii=False, allow_nan=False)` — same
  canonical JSON convention already used by `backend/trusted_context.py`
  and `backend/memory.py`.
- `allow_nan=False` is mandatory: `NaN`/`Infinity`/`-Infinity` are not
  interoperable JSON and `jsonb` rejects them. Hashing them would produce
  digests for values the database refuses, silently breaking idempotency.
- The database validates **format only** (`~ '^[0-9a-f]{64}$'`); it never
  recomputes the hash, keeping SQL and Python from diverging on semantics.
- `backend/transactional_schema.py::canonical_payload_hash()` is the single
  implementation of the digest; the DB CHECK is the guardrail.
- Idempotency semantics (replay vs. conflict) follow the same model already
  proven by `admission_reservations`: same `(user_id, request_id)` + same
  hash → replay; same IDs + different hash → conflict.

---

## 5. Data needed for replay

`turn_requests.replay_payload` stores **only the minimal public result**
needed to reproduce a response after connection loss:

- **Explicit top-level allowlist** (public contract): `response`,
  `emotion_state`, `message_id`, `request_id`, `duration_ms`. Any other
  top-level key is rejected by the `jsonb_keys_subset_of` CHECK.
- **Recursive forbidden-key validation**: `prompt`, `system_prompt`,
  `meta_cognition`, `internal_instructions`, `message`, `user_message`,
  `assistant_message`, `content` are rejected at **any depth** (objects and
  arrays) by the `jsonb_has_forbidden_key` CHECK — a forbidden key can
  never hide inside a nested structure.
- Hard size cap (8 KB serialized).

Message IDs (`user_message_chat_log_id`, `assistant_message_chat_log_id`)
are stable references to `chat_logs` rows. The message FKs are **composite
`(user_id, message_id)`** (baseline `chat_logs` already has
`UNIQUE (user_id, id)`) so a request can never reference another user's
messages; they are nulled on message deletion and replay must never depend
on their presence — the `replay_payload` is authoritative for replay.

---

## 6. Outbox states

| Status | Meaning | Required shape (exact, mutually exclusive) |
|---|---|---|
| `pending` | Available for claim | `processed_at`/`dead_lettered_at`/`retention_until` NULL; no lease; `next_attempt_at` set; `attempts = 0`; no error |
| `processing` | Leased by a worker | `processed_at`/`dead_lettered_at`/`retention_until` NULL; lease set; `attempts` 1..10; no error |
| `completed` | Delivered successfully | `processed_at` set; `dead_lettered_at` NULL; `next_attempt_at` NULL; no lease; `attempts` 1..10; no error; `retention_until` set |
| `failed` | Retryable failure | `processed_at`/`dead_lettered_at`/`retention_until` NULL; no lease; `next_attempt_at` set; `attempts` 1..9; error set |
| `dead_letter` | Exhausted retries | `dead_lettered_at` set; `retention_until` set; `next_attempt_at` NULL; no lease; `attempts = 10`; error set |

The `outbox_events_status_coherence_check` gives every status an exact,
mutually exclusive shape — no field of another state can leak in
(`completed` cannot carry `next_attempt_at` or dead-letter fields;
`dead_letter` requires error + retention; `processing` cannot carry failure
fields), fail-closed.

---

## 7. Outbox idempotency key

- `idempotency_key` is `NOT NULL`, bounded (1..128 chars,
  `^[A-Za-z0-9_.:-]+$`), and unique per **`(user_id, idempotency_key)`**.
- The same key for different users is allowed (user-scoped idempotency).
- A duplicate key within the same user is rejected by the unique constraint,
  making repeated publication attempts idempotent without a worker-side
  lock. The bound prevents unbounded index/storage growth.

---

## 8. Retry policy

- `attempts` counts delivery attempts: `0` on enqueue, incremented on each
  claim.
- Cap: **10 attempts** (`CHECK attempts >= 0 AND attempts <= 10`).
- `failed` rows are eligible for retry in the 1..9 range with a future
  `next_attempt_at`; at 10 the row transitions to `dead_letter`.
- The exact backoff schedule (exponential with jitter, retry-after) is
  decided by the worker task (#272); the schema fixes only the bounds.

---

## 9. Outbox lease

- `processing` events are leased with `lease_owner` + `lease_expires_at`
  (same both-or-neither pair check as `turn_requests`).
- Lease expiry recovery is the worker's job (claim rows where
  `status = 'processing' AND lease_expires_at < now()`), serialized by the
  per-user transaction lock. Not implemented here.

---

## 10. Retention and dead-letter

- `dead_lettered_at` records when an event exhausted its retries.
- `retention_until` is the operational purge horizon; the worker (or a
  maintenance job) purges `dead_letter` rows after retention and `completed`
  rows after their (shorter) retention.
- No automatic purge is implemented in this task; the columns exist so the
  policy has a durable home. Retention durations are operational constants
  to be set in #272.

---

## 11. Foreign keys and ON DELETE

| FK | Target | ON DELETE | Rationale |
|---|---|---|---|
| `turn_requests.user_id` | `profiles(user_id)` | `CASCADE` | User deletion removes their request ledger; no orphans |
| `turn_requests.(user_id, user_message_chat_log_id)` | `chat_logs(user_id, id)` | `SET NULL` | **Composite FK** — a request can only reference messages of the same user; message pruning must not block; replay uses `replay_payload` |
| `turn_requests.(user_id, assistant_message_chat_log_id)` | `chat_logs(user_id, id)` | `SET NULL` | Same as above |
| `outbox_events.user_id` | `profiles(user_id)` | `CASCADE` | User deletion removes their events |
| `outbox_events.(user_id, turn_request_id)` | `turn_requests(user_id, id)` | `CASCADE` | **Composite FK** — an event can only reference a request of the same user (candidate key `turn_requests_user_id_id_key`); deleting the request deletes its events |

Because a composite `ON DELETE SET NULL` would also NULL the `NOT NULL`
`user_id`, a `BEFORE DELETE` trigger on `chat_logs`
(`turn_requests_message_refs_null_trigger`, SECURITY DEFINER) nulls **only**
the message references first; the FK action then has nothing left to null.

Note: `chat_logs.user_id → profiles(user_id)` remains **NO ACTION** (from
baseline). Operational user deletion therefore follows the existing order:
extractions → memories → chat_logs → profiles, with the new server-owned
tables cascading automatically at the profile step. This is the policy
tested by the pgTAP orphan tests.

---

## 12. Indexes

`turn_requests`:

- `turn_requests_user_id_request_id_key` — UNIQUE, covers replay lookup by
  `(user_id, request_id)`.
- `turn_requests_user_id_id_key` — UNIQUE candidate key `(user_id, id)`,
  referenced by the outbox composite FK.
- `turn_requests_user_id_created_at_idx` — recent requests per user.
- `turn_requests_status_lease_expiry_idx` — claim of expired leases.
- `turn_requests_user_committed_revision_idx` — per-user revision queries.

`outbox_events`:

- `outbox_events_user_id_idempotency_key_key` — UNIQUE idempotency scope.
- `outbox_events_status_next_attempt_idx` — claim of available events
  (`status` + `next_attempt_at`), independent of timestamp-only ordering.
- `outbox_events_status_lease_expiry_idx` — reclaim of expired leases.
- `outbox_events_turn_request_id_idx` — FK lookups.

No ordering depends on timestamps alone: every critical query has a
purpose-built index, verified by exact `pg_get_indexdef` assertions in the
pgTAP suite.

---

## 13. Operational rollback

The migration is **purely additive** (new column with default, two new
empty tables, new grants) and does not touch the active write path. That
makes a real, non-destructive rollback possible today:

```sql
-- Operational rollback (only valid while nothing has written real data):
-- Dependency order: trigger first, then tables (their CHECKs reference the
-- payload helpers), then the helpers and the column.
DROP TRIGGER IF EXISTS turn_requests_message_refs_null_trigger ON public.chat_logs;
DROP FUNCTION IF EXISTS public.turn_requests_null_message_refs();
DROP TABLE IF EXISTS public.outbox_events;
DROP TABLE IF EXISTS public.turn_requests;
ALTER TABLE public.profiles DROP COLUMN IF EXISTS revision;
DROP FUNCTION IF EXISTS public.jsonb_has_forbidden_key(jsonb, text[]);
DROP FUNCTION IF EXISTS public.jsonb_keys_subset_of(jsonb, text[]);
```

This is exercised by `test_transactional_schema_legacy.py` and is safe only
before production writes exist. We deliberately do **not** ship a destructive
downgrade migration for the sake of "reversibility"; after any real data
lands, recovery is forward-only (new migration), consistent with
`docs/operations/supabase-security-upgrade.md`.

---

## 14. RLS and grants boundaries

Both new tables are **server-owned internal infrastructure**:

- `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`, **no policies**.
- `anon`, `authenticated`, `PUBLIC`: zero privileges (explicit REVOKE +
  verified by pgTAP).
- `service_role`: full `SELECT, INSERT, UPDATE, DELETE` (it bypasses RLS via
  `BYPASSRLS` in Supabase), mirroring the #265 guarantees on the
  user-facing tables.

No client-authenticated role can reach `turn_requests` or `outbox_events`,
directly or through the PostgREST API. The pgTAP suite asserts the exact
grant matrix.

The payload validation helpers (`jsonb_has_forbidden_key`,
`jsonb_keys_subset_of`) are server-only too: EXECUTE is revoked from
`PUBLIC`, `anon` and `authenticated`, and granted only to `service_role`
(asserted by pgTAP).

---

## 15. Compatibility with the future commit flow

The design is intentionally compatible with the #271 combination of a
**per-user transactional lock** plus **revision validation**:

1. Claim: `INSERT ... ON CONFLICT (user_id, request_id) DO NOTHING` inside
   the per-user lock; read `profiles.revision` as `expected_revision`.
2. Execute + persist messages, then update `profiles.revision = revision + 1`
   and `updated_at` in the same transaction.
3. Commit: `UPDATE turn_requests SET status='completed', committed_revision
   = :new_revision, replay_payload = :minimal_public_result, completed_at =
   now()` guarded by `WHERE status='pending' AND expected_revision = :read`.
4. Enqueue `outbox_events` rows in the same transaction.

No Redis. All coordination is PostgreSQL transactions + advisory locks +
constraints.

## File layout

```
supabase/
  migrations/20240101000004_transactional_turn_schema.sql
  tests/database/03_transactional_turn_schema.test.sql
backend/
  transactional_schema.py              # minimal serialization contracts
  tests/test_transactional_schema.py   # pure unit tests
  tests/test_transactional_schema_legacy.py      # real-DB legacy upgrade
  tests/test_transactional_schema_integration.py # real-DB authz matrix
docs/
  architecture/transactional-turn-schema.md      # this document
```
