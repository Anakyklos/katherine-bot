# Operational Data Retention — Runbook (#316)

## Purpose

The retention command applies the versioned operational retention policy
(`backend/retention_policy.py`, schema version 1) to **operational /
transitory** data only:

| Category | Eligible for purge | Horizon |
|---|---|---|
| `admission_reservations` | `reserved_at` older than the cutoff | 24h |
| `privacy_operations` (ledger) | `applied_at` older than the cutoff | 30 days |
| `outbox_events` | ONLY `completed` / `dead_letter` with `retention_until` past the cutoff | `retention_until` (no age horizon) |

User-controlled content is **never** eligible:

- `chat_logs`, `memories`, `archival_extractions`, `profiles` snapshots and
  `turn_requests` get **no automatic TTL**. They remain until an explicit
  user action or future account deletion (#317).
- `pending`, `processing` and `failed` outbox events are never purged by
  age.
- `delete_history` and the anti-abuse ledger are unchanged: cleanup removes
  expired `admission_reservations` rows only, so current quota windows are
  never affected and quota cannot be bypassed through cleanup.

## Who schedules the command

The command is a **one-shot, explicit operational entrypoint**. There is no
in-process scheduler, no `BackgroundTasks`, no hidden worker and no
long-running server.

A production operator (or a CI cron job in the production environment, not
the application process) schedules:

```
python -m backend.retention_cli --once
```

Recommended cadence: daily. The command is idempotent and safe to run
concurrently, so an overlapping run is harmless; the batch contract bounds
each round.

## Environment

The command reads the environment directly (it does not require the full
application settings; no Groq keys or CORS config are needed):

| Variable | Required | Purpose |
|---|---|---|
| `SUPABASE_URL` | yes | PostgREST base URL |
| `SUPABASE_SERVICE_ROLE_KEY` | yes | service-role key (never logged) |
| `TURN_SUPABASE_TIMEOUT` | no | Supabase transport timeout (seconds, default 5) |

Missing `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` fails closed with a
sanitized error and exit code 1.

## Running one round

```
SUPABASE_URL=https://<project>.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=<service-role-key> \
python -m backend.retention_cli --once
```

Optional parameters:

- `--batch-size N` — rows purged per statement (default 500, max 1000).

The round:

1. Computes cutoffs from the injected clock: admission `now - 24h`,
   privacy ledger `now - 30d`, outbox `now`.
2. For each category, runs bounded batches (each batch is one transactional
   `DELETE ... LIMIT batch_size` purge RPC) until a partial batch or the
   per-category cap (10 000 rows) is reached.
3. Prints a sanitized aggregate summary and exits 0.

## Verifying success

Exit code 0 and a sanitized summary:

```
retention_round schema_version=1
retention_round category=admission_reservations purged=42 batches=1
retention_round category=privacy_operations purged=7 batches=1
retention_round category=outbox_events purged=3 batches=1
retention_round total_purged=52
```

Only the policy schema version and aggregate counts are printed/logged:
no `user_id`, no HMAC, no content, no SQL, no tokens.

Optional SQL verification (as database owner, not in logs):

```sql
SELECT count(*) FROM public.admission_reservations
WHERE reserved_at < now() - interval '24 hours';
-- expect 0 after a round with no new expired rows
```

## Interpreting failure

A failure exits 1 and emits a sanitized event
(`retention_failed`, low-cardinality `code`). The process never prints the
upstream exception text, identifiers or content.

| Symptom | Likely cause | Action |
|---|---|---|
| exit 1, `code=round_failed` | runtime config incomplete (missing URL/key) | fix environment, retry |
| exit 1, `code=persistence_error` | purge RPC unreachable / malformed response | check Supabase availability and PostgREST, retry |
| exit 1, `code=purge_failed` | transport timeout or budget exhausted mid-category | retry; the round is idempotent |

A failed batch does not corrupt anything: each purge RPC is transactional,
and the whole round is idempotent (re-running only processes rows still
eligible).

## Retrying safely

- Re-running the command is always safe: rows already purged are gone;
  rows still eligible are processed again; rows inside the horizon are
  untouched.
- Two overlapping executions are safe by design: every statement deletes a
  bounded, primary-key-selected set; the loser's DELETE simply finds the
  rows already gone. No global advisory lock is used and no writer is
  blocked beyond a single row-level delete.
- Increasing `--batch-size` above the default is bounded to 1000 by the
  SQL boundary (fail closed otherwise).

## Backups and restores (honest limits)

- This routine **removes rows from the primary database**. It is
  destructive by design for the three operational categories and is NOT
  reversible: there is no tombstone and no recycle bin.
- Provider-managed snapshots/backups are **not** erased instantaneously by
  this routine. A snapshot taken before a purge may still contain purged
  rows until it ages out of the provider retention window.
- After a production restore from a snapshot/backup, the restored data may
  contain rows that were purged before the snapshot. Before serving traffic
  from a restored database, the operator MUST reconcile any outstanding
  deletion requests (user-initiated deletes, privacy operations and the
  retention policy) against the restored state, exactly as the privacy
  chain requires: a restore must never resurrect data the user asked to
  delete, and must never re-introduce quota bypass rows.
- If a restore reintroduces operational rows that violate the policy,
  simply run the retention command again after restore.

## Design invariants (what this runbook relies on)

- Policy is versioned: `RETENTION_POLICY_SCHEMA_VERSION = 1`; the SQL
  boundary (migration `20260809030000_operational_data_retention.sql`)
  enforces the same eligibility contracts fail-closed.
- Batch size is explicit and limited; a round never loads a whole table
  into memory.
- No global locks; concurrency-safe by idempotent bounded deletes.
- Grants: the three purge RPCs are `SECURITY DEFINER`,
  `SET search_path = ''`, executable by `service_role` only.
- No automatic TTL for user-controlled content.
