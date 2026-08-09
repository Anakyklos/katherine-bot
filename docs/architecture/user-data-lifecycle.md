# User Data Lifecycle — Privacy Data Operations

## Status

The transactional, idempotent, server-owned privacy primitives are implemented
by issue #314 (privacy chain #274, gate PROD-0 recovery #264), and the
authenticated HTTP frontier exposing them is implemented by issue #315
(``POST /privacy/delete-history``, ``/privacy/delete-memories``,
``/privacy/reset-emotional-state`` and ``/privacy/reset-relationship``).
This document is the architectural record for the chain that future leaves
build on.

## Scope

This leaf creates ONLY the transactional/server-side foundation (#314) plus
the authenticated HTTP application layer (#315):

- `delete_history` — atomic removal of turn history and its derivatives.
- `delete_memories` — atomic removal of memories and ungoverned candidates.
- `reset_emotional_state` — surgical replacement of the emotional snapshot.
- `reset_relationship_state` — surgical replacement of the relationship
  snapshot.

#315 exposes those four primitives through authenticated HTTP endpoints
(`backend/privacy_service.py` + routes in `backend/main.py`) without touching
the #314 semantics: the service is a stateless application layer that
delegates every operation to `run_privacy_operation`.

Explicitly NOT done here: UI, account/Auth deletion (#317), durable
workers/jobs (#276), physical retention/cleanup policy (#316), export,
model/formula changes, reactivation of archival extraction.

## Problem

Privacy operations must coexist with the existing transactional model
(`profiles.revision`, `turn_requests`, `outbox_events`, versioned snapshots,
per-user PostgreSQL locks) without:

- touching another user's data;
- interleaving with turn commits of the same user;
- incrementing `revision` more than once on retries;
- allowing divergent replays of the same `operation_id`;
- leaving deletions partially applied;
- deleting the anti-abuse ledger and enabling quota bypass.

## Binding decisions

### 1. PostgreSQL is the coordinator, reusing the commit_turn boundary

Every operation acquires the exact same per-user advisory transaction lock as
`commit_turn`:

```sql
pg_advisory_xact_lock(hashtextextended(authenticated_user_id, 0))
```

Deletions/resets of one user are therefore serialized against that user's
turn commits and other privacy operations, while different users never
contend on a global lock. The profile row is additionally locked `FOR UPDATE`.

### 2. Persistent idempotency via a durable ledger

`public.privacy_operations` records one row per APPLIED operation, keyed by
`(user_id, operation_id)`, storing:

- the operation name;
- the SHA-256 fingerprint of the operation payload;
- the sanitized public result.

Replay semantics:

- Same `operation_id` + same operation + same payload fingerprint → return
  the stored result verbatim, with NO mutation and NO revision increment.
- Same `operation_id` + divergent operation or payload → sanitized
  `operation_conflict`.
- The ledger survives restarts and different processes: no process-local
  cache is involved.

Keying by `(user_id, operation_id)` (not `operation_id` alone) means one user
can never observe or collide with another user's ledger rows.

### 3. Revision invalidation exactly once

Every applied operation that has a profile increments `profiles.revision`
exactly once, invalidating prior turn computations (a later `commit_turn`
with the old `expected_revision` fails with `revision_mismatch`). Replays
never reach the increment path.

### 4. Identity comes only from the server-side boundary

`p_authenticated_user_id` is the ONLY identity input. `user_id` / `bond_label`
inside payloads and snapshots are rejected at any depth by the existing
`jsonb_snapshot_contract` validation. No body/snapshot-controlled identity is
ever trusted. The identity is validated exactly as received (never normalized)
against the persistent ledger contract — `1 <= char_length(user_id) <= 128`
and `btrim(user_id) <> ''` — in BOTH the Python adapter (before the RPC) and
the SQL helper (`privacy_op_validation_error`): empty, whitespace-only and
oversized identities fail with a predictable `validation_failed` envelope
instead of a generic persistence error. The adapter also binds the RPC result
to the expected identity: a result whose `user_id` differs from the
`authenticated_user_id` requested fails closed and never echoes the divergent
value.

### 5. Server-owned RPCs, minimal grants

- The four RPCs are `SECURITY DEFINER` with fixed `search_path = public`.
- `EXECUTE` granted to `service_role` ONLY; revoked from
  `PUBLIC`, `anon` and `authenticated`.
- The internal core (`privacy_apply_operation`) and helpers
  (`privacy_operation_payload_sha256`, `privacy_op_validation_error`,
  `privacy_is_neutral_snapshot`) have NO grants at all (owner `postgres`
  only).
- `privacy_operations` is `RLS + FORCE RLS` with no policies and NO table
  grants (not even `service_role`): the RPCs are the only access path.
- Direct access to the internal structures follows the existing minimum
  grants/RLS model.

### 6. Sanitized errors and results

Results contain only `status`, `operation`, `operation_id`, `user_id`,
`revision` and aggregate safe counts. Errors use constant sanitized
envelopes (`validation_failed`, `operation_conflict`) or a constant
`persistence error` (P0001) raised by `WHEN OTHERS`. Message/memory content,
internal IDs, prompts, HMACs and raw SQL are never returned nor logged.

## Data matrix

| Data | `delete_history` | `delete_memories` | reset emotional | reset relational |
|---|---|---|---|---|
| `chat_logs` | deleted | preserved | preserved | preserved |
| `turn_requests` | deleted | preserved | preserved | preserved |
| `outbox_events` (derived) | deleted | preserved | preserved | preserved |
| `archival_extractions` (history-derived) | deleted | deleted (ungoverned memory candidates) | preserved | preserved |
| `memories` | preserved | deleted | preserved | preserved |
| `persona_config` / `user_profile` | preserved | preserved | preserved | preserved |
| `emotional_state` snapshot | preserved | preserved | replaced (validated v1 neutral) | preserved |
| `relationship_state` snapshot | preserved | preserved | preserved | replaced (validated v1 neutral) |
| `admission_reservations` (anti-abuse ledger) | preserved | preserved | preserved | preserved |
| `profiles.revision` | +1 (applied, profile exists) | +1 (applied, profile exists) | +1 (applied, profile exists) | +1 (applied, profile exists) |
| `privacy_operations` ledger | +1 row | +1 row | +1 row | +1 row |

`delete_history` deliberately NEVER touches `admission_reservations`:
history deletion cannot be used to bypass quota. `delete_history` also never
touches `memories` or the profile/persona/snapshots.

## Concurrency

- All steps of one operation run in one transaction; any failure rolls back
  everything.
- Same-user operations (and turn commits) serialize on the per-user advisory
  lock; different users proceed in parallel (no global lock).
- Two identical concurrent operations with the same `operation_id`: the
  first applies, the second observes the committed ledger row and replays.
- Two different concurrent operations of the same user: both apply in some
  serialized order; `revision` increments once per applied operation and the
  final state is fully consistent (never partially interleaved).

## Snapshots (resets)

No second emotional/relationship model is created in SQL. The resets reuse
the EXISTING v1 contract validation (`jsonb_snapshot_contract`, migration
#271) with the exact version and allowlist already used by `commit_turn`,
AND additionally require the canonical NEUTRAL state (issue #314 review):

- Neutrality is PRODUCED by the domain: `EmotionalStateV1.neutral(...)` /
  `RelationshipStateV1.neutral(...)`. The Python adapter reconstructs the
  canonical neutral snapshot through those constructors (using the payload's
  own timestamp) and requires an exact match, so a structurally valid v1
  snapshot with non-neutral values (for example `pleasure = 0.9`,
  `coping_mode = 'MANIC'`) is rejected before the RPC.
- Neutrality is VERIFIED at the privileged SQL boundary by
  `privacy_is_neutral_snapshot(payload, kind)`: it first enforces the full v1
  contract (`jsonb_snapshot_contract`) and then compares every state field
  against the canonical neutral constants that mirror the domain
  constructors. Cross-boundary tests (pgTAP and the real-database suites)
  pin the SQL constants to the exact output of the Python constructors, so
  the two boundaries cannot silently diverge.
- The RPC requires `schema_version == 1`, the exact v1 field set/ranges,
  positive timestamp, no identity/internal keys, size bound AND the canonical
  neutral values.
- Malformed, non-v1 or valid-but-non-neutral snapshots are rejected with
  `validation_failed` before any mutation or ledger record.

## Preflight (fail closed on any missing dependency)

The migration preflight checks EVERY mandatory dependency individually with
`pg_catalog.to_regclass(...) IS NULL` (NULL-safe) and raises SQLSTATE 23514
before creating any object of the migration when any of the six required
tables (`chat_logs`, `memories`, `archival_extractions`, `turn_requests`,
`outbox_events`, `admission_reservations`), `profiles.revision`,
`jsonb_snapshot_contract`, `pgcrypto` — or any drift of this migration's own
objects — is missing. A partially incomplete schema can never pass because
at least one table of the set exists. The CI preflight suite
(`backend/tests/test_privacy_operations_preflight.py`) drops exactly one
required table on a compatible baseline and proves the migration fails with
23514 before installing the ledger or any function.

## Error envelopes

| Case | Envelope |
|---|---|
| Invalid input (identity/operation/payload/snapshot) | `{"error":{"code":"validation_failed",...}}` |
| Reused operation_id with divergent operation/payload | `{"error":{"code":"operation_conflict",...}}` |
| Unexpected PostgreSQL failure | raised constant `persistence error` (P0001), rollback |

## HTTP API frontier (#315)

The four primitives are exposed over authenticated HTTP as four explicit
actions, each receiving ONLY `{"operation_id": "<uuid>"}`:

| Endpoint | Operation |
|---|---|
| `POST /privacy/delete-history` | `delete_history` |
| `POST /privacy/delete-memories` | `delete_memories` |
| `POST /privacy/reset-emotional-state` | `reset_emotional_state` |
| `POST /privacy/reset-relationship` | `reset_relationship_state` |

Binding decisions of the HTTP layer:

1. **Identity is never client-supplied.** The body is `extra="forbid"` and
   accepts only `operation_id`; any extra key (including `user_id`) returns
   `422`. The only identity is `current_user.id` from the existing
   `get_current_user` auth dependency. The UUID is normalized to lowercase
   canonical form before reaching the privacy layer; any canonical UUID
   version is accepted (no v4 restriction), matching the #314 contract.
2. **`backend/privacy_service.py` is a stateless application service.** It
   holds no per-user state: identity and `operation_id` are per-call
   arguments. The repository (Supabase sync RPC adapter) and the clock are
   injectable; reset snapshots are built with the injected clock through the
   #314 neutral helpers, so the timestamp is never a hidden `time.time()`.
3. **Writes use the existing bounded-write infrastructure.** The sync
   `client.rpc(name, params).execute()` call runs through
   `run_blocking_write` (per-action budget from the operational turn
   configuration, real PostgREST transport timeout, drain-on-cancellation).
   No `BackgroundTasks`, no fire-and-forget threads, no orphaned tasks.
4. **Public response projection.** The response is deliberately smaller than
   `PrivacyOperationResult`:

   ```json
   {
     "operation": "delete_history",
     "status": "applied",
     "counts": { "chat_logs": 2, "turn_requests": 1, "outbox_events": 1,
                 "archival_extractions": 1, "memories": 0, "profiles": 0 }
   }
   ```

   `user_id`, `revision`, `operation_id`, internal IDs, content, snapshots
   and secrets are never exposed. Fresh execution and idempotent replay
   produce the same projection (there is no `replayed` field).
5. **Stable sanitized HTTP errors.** Missing/invalid credentials → `401`;
   invalid input / extra keys / invalid UUID → `422`; `operation_conflict`
   → `409`; persistence failure → `503`; any unexpected failure or internal
   contract violation (`invalid_rpc_result`, divergent identity, malformed
   envelope) fails closed as a constant `500` and is never presented as a
   client `422`.
6. **Observability** uses only the existing sanitized events with
   low-cardinality codes (`http_result`, `request_conflict`). Raw
   `user_id`, `operation_id`, bearer tokens, content, snapshots, RPC payloads
   and upstream exception text never reach logs or responses.

## Files

- `supabase/migrations/20260808220000_privacy_data_operations.sql`
- `supabase/tests/database/06_privacy_data_operations.test.sql` (pgTAP)
- `backend/privacy_operations.py` (pure Python adapter, #314)
- `backend/privacy_service.py` (stateless application service + Supabase
  repository adapter + public projection, #315)
- `backend/main.py` (DTO, four authenticated routes, error mapping)
- `backend/dependencies.py` (container wiring of the default service)
- `backend/tests/test_privacy_operations.py` (unit, no DB)
- `backend/tests/test_privacy_operations_integration.py` (real Supabase)
- `backend/tests/test_privacy_operations_legacy.py` (legacy upgrade)
- `backend/tests/test_privacy_api.py` (unit API/service, no DB)
- `backend/tests/test_privacy_api_integration.py` (real Supabase, #315)

## Rollback

The migration is purely additive: dropping `privacy_operations` and the four
RPCs restores the previous schema without touching any other object. Applied
privacy operations themselves are destructive by design and are NOT
reversible; idempotency guarantees they are never applied twice.

## Out of scope (future leaves)

- UI / settings screens exposing these primitives.
- Account/Auth user deletion with tombstone (#317), durable workers (#276)
  and export policies.
- Emotional/relationship formula, decay, appraisal or personality changes.

Operational retention/cleanup (#316) is implemented by its own leaf:
`supabase/migrations/20260809030000_operational_data_retention.sql` plus
`backend/retention_policy.py`, `backend/retention.py` and the
`python -m backend.retention_cli --once` command. It covers ONLY
operational data (`admission_reservations`, the privacy operation ledger,
final outbox events) and deliberately never introduces an automatic TTL
for user-controlled content. The SQL boundary enforces the binding
minimum retention horizons with authoritative PostgreSQL time: purge
cutoffs supplied by the process are clamped, so a fast/misconfigured
process clock can never advance deletion of rows inside the horizons.

## Account deletion foundation (#324)

The durable PostgreSQL/Supabase foundation for account deletion is its own
leaf: `supabase/migrations/20260810120000_account_deletion_ledger.sql`
plus `backend/account_deletion.py` (pure Python contract, no HTTP, no Auth
Admin, no worker yet). Deleting an account crosses PostgreSQL and Supabase
Auth; there is no distributed transaction that makes both atomically
equivalent, so the architecture is: register a durable tombstone, purge the
PostgreSQL data in one transaction, and only later (#325) delete the Auth
user after the commit is confirmed. The tombstone survives the
`profiles` DELETE (the job table has NO FK to `profiles`).

### State machine

`pending -> processing -> (failed | pending retry) -> processing -> completed`

- `pending`: registered, not claimed. Always has a scheduled
  `next_attempt_at`.
- `processing`: claimed by exactly one worker (lease owner + expiry). The
  DB allows only `processing` to carry a lease.
- `failed`: a worker reported a sanitized `error_code`; retry-eligible
  after `next_attempt_at` (backoff derived from `attempts`, 30s..1h).
  A `failed` job whose `attempts` reached the deterministic ceiling (100)
  is TERMINAL: `next_attempt_at` becomes NULL, it is never claimed
  automatically again, and the tombstone stays blocking and operationally
  recoverable by a privileged operator. `record_retry` at the ceiling also
  becomes a terminal `failed` job with `error_code = attempts_exhausted`.
- `completed`: DB purge committed (`db_purged_at`) AND the future Auth
  deletion confirmed (#325). Constraints reject every incoherent
  combination: `completed` requires `db_purged_at` + `completed_at` and
  NULL `user_id`; `failed` requires `error_code`; attempts are never
  negative; lease owner and error codes use strict allowlists;
  `pending` always has `next_attempt_at`.

### DB/Auth frontier and `db_purged_at`

`db_purged_at` is the authoritative marker that the PostgreSQL purge
committed. It becomes non-NULL ONLY in the same transaction that completed
every delete; any failure rolls the whole attempt back and the marker
stays empty. It is set while the job is `processing` (purge done, Auth
deletion pending) and is required for `completed`. Once set it is NEVER
cleared: retry/failure after the DB purge (e.g. the future Auth deletion
failed) transition the job to `pending`/`failed` while PRESERVING the
marker, and a reacquired job's purge returns `already_purged` without
repeating deletes. A repeated purge after a crash (`DB commit` done,
process died before Auth) is a safe replay for the same reason: no delete
depends on the row existing.

### Purge order and locking

The purge deletes the user's rows in one transaction, in FK-safe order
analyzed from the migrations (no assumed cascade):

1. `outbox_events` (FK profiles/turn_requests CASCADE)
2. `turn_requests` (FK profiles CASCADE; chat_logs refs SET NULL)
3. `archival_extractions` (FK profiles/chat_logs CASCADE)
4. `memories` (FK profiles NO ACTION)
5. `chat_logs` (FK profiles NO ACTION)
6. `admission_reservations` (no FK)
7. `privacy_operations` (no FK)
8. `profiles` (last: snapshots, persona, `user_profile`)

Before mutating data the purge acquires the SAME per-user advisory
transaction lock as `commit_turn` and the privacy operations
(`pg_advisory_xact_lock(hashtextextended(user_id, 0))`), so a concurrent
turn commit of the same user can never recreate or modify data while the
account is being deleted. Distinct users are never globally serialized.

### Leases and retries

Claims use `SELECT ... FOR UPDATE SKIP LOCKED`: two workers can never own
the same job simultaneously. Leases expire, making a dead worker's job
eligible again; every job-scoped RPC revalidates ownership (worker id +
unexpired lease), so an old worker cannot purge, fail, retry or finalize
after losing the lease. Retries are represented in the database:
`attempts`, `next_attempt_at`, `record_failure` (status `failed`) and
`record_retry` (status back to `pending`).

### Identity minimization and HMAC policy

While the job still needs to run the purge/Auth deletion, the raw
`user_id` stays stored. On completion it is removed (NULL): only the
sanitized reference remains for bounded audit and idempotency. The
reference is an HMAC-SHA256 (lowercase hex, 64 chars) generated
server-side with a DEDICATED `account-deletion` HMAC domain, distinct from
the message/network/correlation/user-reference domains, so a persisted
reference can never be correlated with `USER_REFERENCE_DOMAIN` values.
The future HTTP client can never supply this reference; it is produced by
the trusted backend.

### Idempotency

`(user_ref_hmac_sha256, operation_id)` is UNIQUE. The same pair with the
same intent fingerprint is an exact replay (no second job); a divergent
fingerprint is a sanitized `operation_conflict`. Different users never
collide because the reference is a domain-separated HMAC of the identity.
No process-local cache.

### Retention of tombstones

Completed tombstones are aged out by `account_deletion_purge_completed`
(30-day horizon, batch-limited, idempotent, `completed` only, cutoff
clamped by the DB against `clock_timestamp()` like #316).
`pending`/`processing`/`failed` jobs are never removed by age. No new
scheduler: the RPC reuses the operational cleanup model and the future
worker may call it.

### ACLs

`account_deletion_jobs` is RLS + FORCE RLS with zero policies and no table
grants (not even `service_role`). All runtime RPCs are `SECURITY DEFINER`
with `SET search_path = ''`, EXECUTE granted to `service_role` only and
revoked from `PUBLIC`/`anon`/`authenticated`; internal helpers
(`account_deletion_validation_error`, `account_deletion_assert_owner`,
`account_deletion_intent_fingerprint_sha256`) have no runtime grants. No
dynamic SQL, no raw exception text, no parameters in logs.

### Future leaves

- #325: worker that claims jobs, runs the purge RPC, then deletes the Auth
  user via Supabase Auth Admin and finalizes the job.
- #326: HTTP tombstone gate that consults
  `account_deletion_has_tombstone` before serving operations.
- #318: tracking of the full deletion pipeline.
