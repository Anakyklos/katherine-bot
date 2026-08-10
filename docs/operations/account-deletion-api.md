# Account Deletion API and Tombstone Gate (#326)

## Purpose

`POST /privacy/delete-account` exposes the durable #324 account deletion
ledger to an authenticated user and registers the deletion job the #325
worker executes (PostgreSQL purge, then Supabase Auth Admin hard delete).
From the moment a tombstone exists, **every normal account action is
blocked** by a single fail-closed gate, even when an already-issued JWT is
still accepted by the Auth service.

```text
authenticated request
    ↓
identity = current_user.id  (ONLY source of identity)
    ↓
server derives user_ref (HMAC-SHA256, dedicated account-deletion domain)
    ↓
server derives deterministic delete_account intent fingerprint
    ↓
account_deletion_request RPC -> durable job (tombstone) [idempotent replay]
    ↓
public response: {"status": "accepted"}  (or "completed" only when the
                                             ledger confirms it)
```

## Endpoint contract

### `POST /privacy/delete-account`

Authenticated with a Bearer token (`get_current_user`). The request body
accepts **only** `operation_id` (any canonical UUID, normalized to lowercase):

```json
{"operation_id": "11111111-1111-1111-1111-111111111111"}
```

Responses:

| HTTP | Body | Meaning |
| --- | --- | --- |
| 200 | `{"status": "accepted"}` | Job created or exact replay; deletion pending/in progress. |
| 200 | `{"status": "completed"}` | Only when the ledger confirms completion (finalized with `db_purged_at`). |
| 401 | — | Missing/invalid credentials. |
| 409 | `{"detail": {"code": "operation_conflict", ...}}` | Same `operation_id` reused with a divergent intent. |
| 422 | `{"detail": {"code": "invalid_request", ...}}` | Extra keys (`user_id`, `user_ref`, `job_id`, HMACs, ...) or invalid `operation_id`. No RPC runs. |
| 503 | `{"detail": {"code": "service_unavailable", ...}}` | Persistence/store unavailable. |

The public response never contains `user_id`, `user_ref`, `operation_id`,
`job_id`, HMACs, internal timestamps, upstream errors or SQL. The same
user + `operation_id` + intent is an exact ledger replay and never creates
a second job.

## Tombstone gate (fail-closed)

Every normal route applies the gate through **one** helper in
`backend/main.py` (`_enforce_account_deletion_gate`) immediately after
authentication and **before any useful work**:

* `/chat` — before `reserve_admission_sync`, state/history reads,
  embeddings, provider/Groq calls, `engine.process_turn` and any turn
  write.
* `/history` — before any `chat_logs` query.
* `POST /privacy/delete-history`, `POST /privacy/delete-memories`,
  `POST /privacy/reset-emotional-state`,
  `POST /privacy/reset-relationship` — before any mutation.

A present tombstone (status `pending`, `processing`, `failed`, or
`completed` while the row still exists within the retention horizon)
returns **423**:

```json
{"detail": {"code": "account_deletion_pending", "message": "Account deletion is pending."}}
```

If the tombstone store cannot be consulted (persistence failure, timeout,
malformed payload, unavailable store, missing service), the route returns
a sanitized **503** and never continues. A tombstone-store failure is
never interpreted as "user active".

The gate relies on the **server-side reference**: it recomputes the HMAC
from `current_user.id` with the same admission secret under the dedicated
`account-deletion` domain, so an old JWT cannot bypass the block by
presenting a different identity or reference.

### Exception

`POST /privacy/delete-account` itself is **not** gated: it must stay
reachable with base authentication so an idempotent replay keeps working
while the token is still accepted.

## Idempotency and honesty

* The #324 SQL RPC `account_deletion_request` returns `created` on the
  first call and `replay` for the same `(user_ref, operation_id,
  fingerprint)`; a divergent fingerprint returns a sanitized
  `operation_conflict`.
* The API returns `accepted` for created/replayed non-completed jobs and
  `completed` **only** when the ledger reports `completed` with a
  persisted `db_purged_at`. The API never promises completion before the
  #325 worker finishes.
* `operation_conflict` maps to HTTP 409 with a constant public message.

## Security properties

* Identity comes **only** from `current_user.id` (authenticated). No
  `user_id`, `user_ref`, HMAC, `job_id` or extra key is accepted from the
  body/query/path/headers (422 before any RPC).
* The HMAC reference uses the existing server-side admission secret through
  `compute_account_deletion_user_ref` (dedicated `account-deletion` HMAC
  domain). No new secret and no new client are introduced.
* The intent fingerprint is deterministic: a constant `delete_account`
  payload with no user data.
* The default composition reuses the existing Supabase client
  (`ApplicationDependencies.account_deletion_service`); no per-request or
  second Supabase client is created, and no user state lives in the
  container.
* Observability uses constant, low-cardinality events
  (`account_deletion_requested`, `account_deletion_blocked`,
  `account_deletion_gate_unavailable`) and never logs ids, HMACs, tokens,
  payloads, SQL or upstream exception text.
* `/live` and `/ready` are unchanged: the gate reuses the existing
  Supabase/persistence component, so no new readiness dependency exists.

## Related files

* `backend/account_deletion_service.py` — stateless service, public
  projection and the single `assert_active` gate boundary.
* `backend/main.py` — `POST /privacy/delete-account`, the centralized
  `_enforce_account_deletion_gate` helper, and gate integration in
  `/chat`, `/history` and the four privacy actions.
* `backend/dependencies.py` — default composition
  (`SupabaseAccountDeletionRepository` over the existing client).
* `backend/account_deletion.py` — #324 ledger contract.
* `backend/account_deletion_worker.py` / `backend/account_deletion_cli.py`
  — #325 durable executor (unchanged).
* `docs/operations/account-deletion-worker.md` — worker runbook.
