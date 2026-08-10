# Account Deletion Worker — Runbook (#325)

## Purpose

The account deletion worker is the **durable, DB-first** executor that
finishes a user's account deletion after the #324 ledger registered the
tombstone. For each claimed job it runs, in this strict order:

```text
acquire lease
    ↓
PostgreSQL purge (transactional, db_purged_at committed)
    ↓
Supabase Auth Admin hard delete (should_soft_delete = false)
    ↓
finalize (completed, user_id minimized to NULL)
```

**There is no Auth-first path.** Auth is called only after the PostgreSQL
purge is confirmed (either freshly purged or replayed as
`already_purged` with `db_purged_at` present). `completed` is written only
after Auth deletion is confirmed or the user is proven absent through a
structured SDK property (`user_not_found`). There is no soft delete and no
undo.

## How to run one round

```bash
# Environment (values are never echoed or logged)
export SUPABASE_URL=...
export SUPABASE_SERVICE_ROLE_KEY=...
export ACCOUNT_DELETION_LEASE_SECONDS=300   # optional, SQL bound 1..3600
export ACCOUNT_DELETION_MAX_BATCH=10        # optional, SQL bound 1..1000
export ACCOUNT_DELETION_AUTH_TIMEOUT_SECONDS=10.0  # optional, <= lease

python -m backend.account_deletion_cli --once
```

`--once` processes at most `max_batch` jobs and exits deterministically:

* exit `0` with `no_work=True` when there is no eligible job (empty queue
  is nominal, never an incident);
* exit `0` after a round even when individual jobs were scheduled for
  retry, failed, or had a lost lease (those are DB-governed outcomes);
* exit `1` only for an operational failure that prevents the round from
  running (missing configuration, invalid bounds, database unreachable at
  acquisition).

The worker id defaults to a per-process random `cli-worker-<hex>` that
matches the SQL allowlist and never contains PII. Set
`ACCOUNT_DELETION_WORKER_ID` for a stable operator identity.

## Scheduling externally

There is deliberately **no scheduler inside the process** (no daemon, no
hidden loop, no `BackgroundTasks`). Schedule the one-shot command from
your existing scheduler, e.g. a systemd timer or cron:

```cron
*/5 * * * *  cd /srv/katherine && python -m backend.account_deletion_cli --once
```

Concurrent invocations are safe: the lease is acquired with
`FOR UPDATE SKIP LOCKED` in PostgreSQL, so two workers can never own the
same job at the same time. A job whose lease expired is reclaimed by the
next worker.

## DB-first ordering and the authoritative marker

`db_purged_at` is set in the **same transaction** that completed the
PostgreSQL purge. The worker treats it as the single authoritative
marker:

* `db_purged_at IS NULL` → run the purge (`account_deletion_purge`), then
  continue only on a valid `purged`/`already_purged` result.
* `db_purged_at IS NOT NULL` → skip all destructive DB mutations and go
  straight to Auth (crash-after-commit recovery).
* A failed purge (transaction rolled back) **never** leads to an Auth
  call; the lease expires and another worker retries the purge.

## Leases

* Jobs are claimed only through `account_deletion_acquire_lease`
  (`FOR UPDATE SKIP LOCKED`); the Python never implements its own lock.
* Every job-scoped RPC re-validates ownership and a live lease
  (`account_deletion_assert_owner`). A worker that lost its lease (or
  whose lease expired during the external Auth call) can no longer purge,
  fail, retry or finalize: the RPC fails closed and the worker emits
  `account_deletion_lease_lost`.
* The Auth call runs under a bounded transport timeout
  (`ACCOUNT_DELETION_AUTH_TIMEOUT_SECONDS`, default 10s, never larger
  than the lease) so a legitimate lease loss is never overridden by an
  external call that runs longer than the lease.
* If the DB purge already committed before the lease loss, the next
  worker skips the destructive purge via `db_purged_at` and the Auth
  delete stays idempotent.

## Retries and attempts (governed by the database)

There is **no Python backoff**. `next_attempt_at` and `attempts` are the
authority and live in `account_deletion_jobs`:

* **Transient Auth unavailability** (`auth_unavailable`, e.g. 5xx,
  rate-limit, timeouts, connect errors) → `account_deletion_record_retry`
  (the SQL RPC whose semantics are exactly "voluntarily defer a job": back
  to `pending`, `error_code` cleared, DB backoff).
* **Deterministic Auth failures** (`auth_forbidden` for 401/403,
  `auth_failed` for other structured SDK errors) →
  `account_deletion_record_failure` (job to `failed` with the sanitized
  `error_code`, DB backoff).
* Backoff is `LEAST(3600, 30 * attempts)` seconds, computed in the DB.
* The ceiling is 100 attempts. When reached, the job becomes **terminal**
  (`failed`, `error_code attempts_exhausted`, `next_attempt_at NULL`) and
  is never claimed automatically again; the tombstone stays blocking and
  is recoverable only by a privileged operator.
* `db_purged_at` is preserved across failures/retries, so retried jobs
  never repeat destructive deletes.

## Auth failures and already-absent users

Auth errors are classified into a small constant taxonomy using **only
structured SDK properties** (`code`, `status`, exception type). Parsing
exception text is forbidden:

* `user_not_found` structured code → idempotent success
  (`account_deletion_auth_already_absent`), then finalize.
* 401/403 → `auth_forbidden`.
* 5xx / rate-limit / timeouts / transport → `auth_unavailable` (retry).
* everything else structured → `auth_failed`.

Upstream exception text, ids, tokens and URLs are never propagated or
logged.

## Storage blocking Auth deletion

We do **not** clean Supabase Storage automatically. Deleting an Auth user
can be rejected by ownership of external objects (Storage objects,
bucket ownership). In that case:

* the Auth delete fails with a sanitized error (`auth_failed` or
  `auth_forbidden`);
* the failure is recorded and the tombstone stays blocking;
* the job remains operationally recoverable and is retried per
  `next_attempt_at`;
* the system **never** concludes the account was removed when it was not.

Operators must resolve the external object ownership out-of-band; the
worker retries the Auth delete afterwards.

## Tombstone semantics

The job row is a durable tombstone: it survives the `profiles` delete, has
no FK to `profiles`, and is never removed by the purge. On finalize the
raw `user_id` is minimized to `NULL`; the domain-separated HMAC reference
persists for bounded audit/idempotency. Completed tombstones are aged out
by the retention command (30 days, via `account_deletion_purge_completed`);
`pending`/`processing`/`failed` jobs are never aged out.

## Investigating without leaking identity

Never put `user_id`, HMACs, `operation_id`, job ids, emails, tokens,
payloads, SQL or upstream exception text in logs, metrics, alerts or
tickets. All worker observability uses constant events
(`account_deletion_worker_started`, `account_deletion_no_work`,
`account_deletion_db_purged`, `account_deletion_auth_deleted`,
`account_deletion_auth_already_absent`,
`account_deletion_retry_scheduled`, `account_deletion_failed`,
`account_deletion_completed`, `account_deletion_lease_lost`) plus
aggregate counts and the job's `attempts` number.

To investigate a specific failed job as an operator, use only the
minimized, server-side reference and the sanitized `error_code`:

```sql
-- Search by the HMAC reference computed server-side (never a raw id)
SELECT status, error_code, attempts, next_attempt_at IS NULL AS terminal,
       db_purged_at IS NOT NULL AS purged, lease_expires_at
FROM account_deletion_jobs
WHERE user_ref_hmac_sha256 = '<server-derived-ref>';
```

## No undo; provider snapshots

There is **no undo** for a completed account deletion, and this worker
does not erase backups/snapshots maintained by the provider (Supabase
Auth user snapshots, database backups, object storage versions). Those
age out per provider policy and are out of scope for this worker.

## Related files

* `backend/account_deletion_worker.py` — worker + Auth Admin adapter.
* `backend/account_deletion_cli.py` — one-shot CLI (`--once`).
* `backend/account_deletion.py` — #324 repository contract.
* `supabase/migrations/20260810120000_account_deletion_ledger.sql` — SQL
  authority (eligibility, lease, attempts, retry, exhaustion, purge,
  finalize, minimization).
* `docs/operations/operational-data-retention.md` — tombstone retention.
