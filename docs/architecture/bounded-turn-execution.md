# Bounded Turn Execution

## Problem

The `/chat` endpoint had no hard deadline, unlimited retries, and consumed
cancellations until the turn completed. Failures in appraisal or generation
were silently replaced with neutral fallbacks or hardcoded text that was
then persisted as a valid Katherine response.

## Solution

A monotonic deadline (`time.monotonic`) governs every turn. Stages before
persistence are fully cancellable. Only the commit section
(`save_turn` + `sync_state`) is shielded against cancellation.

## Turn Stage Sequence

```
load_state → load_context → appraisal → transition → generation → commit
    │              │             │            │            │          │
    └── bounded   └── bounded  └── async    └── pure     └── async  └── shielded
        thread        thread       LLM          domain       LLM        bounded
        helper        helper                                              thread
```

1. **lock acquisition** — Time-bounded by `remaining_before_reserve` via
   `asyncio.wait_for` wrapping only `ctx.__aenter__()`. Once acquired,
   the turn runs under budget checks (not an outer `wait_for`).
2. **load_state** — Bounded via `run_blocking_with_deadline()` helper.
3. **load_context** — Bounded via `run_blocking_with_deadline()` helper.
4. **appraisal** — Async LLM call via `AsyncGroq` with deadline budget.
5. **transition** — Pure domain: emotional + relationship transition.
6. **generation** — Async LLM call via `AsyncGroq` with deadline budget.
7. **commit** — `save_turn()` + `sync_state()` within a named task,
   shielded with explicit deadline tracking for post-cancel wait.

## Bounded Blocking Operations (`run_blocking_with_deadline`)

All thread-bound operations (Supabase calls: `load_user_state`, `get_context`,
`save_turn`, `sync_state`) are wrapped by `run_blocking_with_deadline()`:

```python
async def run_blocking_with_deadline(
    stage_label: str,
    budget: TurnBudget,
    supabase_timeout: float,
    func: Callable[..., T],
    *args, **kwargs,
) -> T:
```

The helper:
- Accepts the remaining monotonic budget via `TurnBudget`.
- Computes `timeout = min(budget.remaining, supabase_timeout)`.
- Uses `asyncio.wait_for(to_thread(func, ...), timeout=timeout)` to bound
  the wait.
- On **timeout**: raises `DeadlineExceeded` (``turn_timeout``).
- On **cancellation**: propagates ``asyncio.CancelledError``.
- On **operation failure**: wraps as ``TurnExecutionError(persistence_unavailable)``.
- Never waits indefinitely for a thread.

## Deadline & Budget

- **Deadline starts** when `process_turn()` is called, using `time.monotonic`.
- **Lock acquisition** is bounded by `remaining_before_reserve` so a blocked
  lock does not consume the commit reserve.
- **Budget checks** at each stage prevent pre-commit stages from exceeding
  `remaining_before_reserve`. This replaces the earlier approach of wrapping
  the entire `_run_turn_locked()` with `wait_for`, which could fire during
  the commit section and release the lock while `commit_task` was still
  executing.
- **Commit reserve** is a fixed time budget reserved exclusively for
  persistence (`save_turn` + `sync_state`). If `budget.has_reserve` is
  false, the turn fails with `turn_timeout` before any persistence.
- **No persistence happens** if the reserve is insufficient.

### Defaults

| Parameter | Default | Env Variable |
|-----------|---------|--------------|
| total_deadline | 45.0s | `TURN_TOTAL_DEADLINE` |
| connect_timeout | 3.0s | `TURN_CONNECT_TIMEOUT` |
| provider_attempt_timeout | 15.0s | `TURN_PROVIDER_ATTEMPT_TIMEOUT` |
| supabase_timeout | 5.0s | `TURN_SUPABASE_TIMEOUT` |
| commit_reserve | 10.0s | `TURN_COMMIT_RESERVE` |
| max_attempts | 2 | `TURN_MAX_ATTEMPTS` |
| base_backoff | 0.25s | `TURN_BASE_BACKOFF` |
| max_backoff | 0.75s | `TURN_MAX_BACKOFF` |
| max_jitter | 10% (0-100%) | `TURN_MAX_JITTER` |
| frontend_timeout_ms | 50_000ms | `TURN_FRONTEND_TIMEOUT_MS` |

### Invariants

- `connect_timeout <= provider_attempt_timeout`
- `provider_attempt_timeout < total_deadline`
- `commit_reserve >= 2 × supabase_timeout`
- `commit_reserve < total_deadline`
- `max_attempts` is a real integer >= 1 and <= 5 (safety limit)
- `max_jitter` in `[0.0, 1.0]` (0.0 = no jitter, allowed)
- `total_deadline <= 300`
- `supabase_timeout <= 30`
- `connect_timeout <= 30`
- `provider_attempt_timeout <= 120`
- `commit_reserve <= 120`
- `frontend_timeout_ms <= 300_000`

## Lock Separation (critical)

The `wait_for` timeout in `process_turn` previously wrapped the entire
`_run_turn_locked()`, including the commit section. This could fire while
`asyncio.shield(commit_task)` was executing, releasing the user lock while
the commit task continued as an orphaned thread — a race condition.

**Fix**: Only the lock acquisition (`ctx.__aenter__()`) is bounded by
`remaining_before_reserve`. Once acquired, the turn runs directly under
budget checks. The commit section is protected by a named task with
shield.

## Groq Client Management

### Async Clients (Request-Scoped)

Groq async clients are **request-scoped** rather than shared:
- A new `AsyncGroq` client is created for each provider attempt.
- The client is always closed in a `finally` block after the attempt.
- No shared mutable state → no thread-safety issues.
- No risk of closing a client while another call is using it.
- No `asyncio.create_task()` from a non-event-loop thread.
- `max_retries=0` (SDK retries disabled).
- `httpx.Timeout` configured with connect/read timeouts from config.

### Sync Clients (Archival Extraction)

The sync `chat_completion` path also:
- Uses `max_retries=0` and bounded `httpx.Timeout`.
- Has finite `max_attempts` (same as async path).
- Creates a fresh `Groq()` client per attempt.

## Retry Policy

- **SDK retries disabled**: `max_retries=0` on both `AsyncGroq` and `Groq`.
- **Application retries** bounded by `min(max_attempts, eligible_key_count)`.
- Each key is tried **at most once** per logical call.
- `asyncio.wait_for(client.chat.completions.create(...), timeout=effective_timeout)`
  is the primary timeout mechanism. `effective_timeout` =
  `min(provider_attempt_timeout, remaining_before_reserve)`.
- `APITimeoutError` and `asyncio.TimeoutError` both produce
  `ProviderFailure.timeout` → `TurnErrorCode.turn_timeout` (HTTP 504).
- Generic exceptions are logged and treated as transient (try next key).
- 401 errors deactivate the key idempotently and try the next key.
- 429 errors mark cooldown and try the next key.
- Connection/5xx errors try the next key.
- Backoff: exponential with jitter, total capped at `max_backoff`.
- `_acquire_next_key()` distinguishes pool states with `ProviderFailure` codes:
  `auth_failed` (all deactivated), `rate_limited` (all cooldown),
  `connection_failed` (all tried).

## Cancellation Semantics

- **Before commit**: Cancellation (`asyncio.CancelledError`) propagates
  immediately through the `try/finally` in `_run_turn_locked`, which calls
  `ctx.__aexit__()` to release the lock. No persistence occurs.
- **During commit**: A named commit task is created and `asyncio.shield()`-ed.
  If a cancel arrives during commit, the `CancelledError` is caught, the
  original exception is saved (`as original_cancel`), and a fixed deadline
  is computed from `budget.remaining`. The commit task is re-shielded and
  awaited with `wait_for` using `remaining = max(0.0, deadline - now())`.
  Additional cancellations during this wait are consumed harmlessly:
  shield prevents cancellation of `commit_task`, and the decreasing
  deadline ensures progress (no infinite loop risk).
  When the commit finishes or the deadline expires, the original
  cancellation is re-raised via `raise original_cancel`.
- **Lock**: Per-user `asyncio.Lock` serializes requests for the same user.
  Lock is released on timeout (`DeadlineExceeded`), cancellation, or
  failure before commit. Lock is held during the entire commit wait.
- **Outer `wait_for` does NOT wrap the entire turn**. Only lock acquisition
  (`__aenter__()`) is time-bounded. Once acquired, budget checks prevent
  unbounded execution.

## HTTP Error Codes

| Code | HTTP Status | `detail.code` |
|------|-------------|---------------|
| Deadline exceeded / effective timeout | 504 | `turn_timeout` |
| Rate limited | 429 | `upstream_rate_limited` |
| Provider unavailable | 503 | `provider_unavailable` |
| Invalid provider request | 503 | `provider_invalid_request` |
| Invalid provider response | 500 | `provider_invalid_response` |
| Persistence unavailable | 503 | `persistence_unavailable` |
| Unexpected error | 500 | `internal_error` |

Never exposed: model name, provider detail, exception text, prompt, key,
token, or stack trace.

## Observability

Structured low-cardinality log events:

```
event=turn_stage_completed stage=generation outcome=success duration_ms=120
event=turn_stage_completed stage=appraisal outcome=failed code=provider_invalid_response
event=turn_stage_completed stage=generation outcome=cancelled
event=commit_timeout_after_cancel
event=emotional_appraisal_fallback code=...
```

Never logged: `user_id`, message content, prompt, response, key, token,
DB IDs, or exception text.

## Frontend

- `AbortController` created per request, stored in ref, cleaned up on
  success/error/cancel/unmount.
- `requestTokenRef` (monotonically increasing) prevents stale `finally`
  blocks from clearing the controller/timer of a newer request or changing
  `isLoading` of a request that already completed.
- Timeout timer at 50s (configurable) aborts the controller.
- `AbortSignal` forwarded to Axios via the `signal` option.
- Error responses classified by HTTP status: 504 → timeout, 429 →
  rate_limited, 503 → service_unavailable, 422 → validation.
- Axios error objects are never logged to console directly.

## ProviderFailure → TurnErrorCode Mapping

| ProviderFailure | TurnErrorCode | HTTP |
|----------------|---------------|------|
| `rate_limited` | `upstream_rate_limited` | 429 |
| `auth_failed` | `provider_invalid_request` | 503 |
| `connection_failed` | `provider_unavailable` | 503 |
| `server_error` | `provider_unavailable` | 503 |
| `timeout` | `turn_timeout` | 504 |
| `invalid_response` | `provider_invalid_response` | 500 |
| `cancelled` | `internal_error` (not used — propagated) | — |

## Risk: Partial Persistence (#271)

Until issue #271 is resolved, a failure between `save_turn()` and
`sync_state()` can leave the emotional/relationship state out of sync
with the conversation history. The commit section is non-atomic.

Additionally, if the commit's post-cancel wait times out, the
`commit_task` continues executing in the background without the user
lock. While the Supabase transport timeout will eventually terminate
the underlying thread, there is a brief window of orphaned execution.
This risk is accepted until #271 introduces proper transaction semantics.

## Configuration Safety Limits

To prevent misconfiguration from causing unbounded execution, the
following absolute limits are enforced:

| Parameter | Safety Limit |
|-----------|-------------|
| `total_deadline` | ≤ 300s |
| `supabase_timeout` | ≤ 30s |
| `connect_timeout` | ≤ 30s |
| `provider_attempt_timeout` | ≤ 120s |
| `commit_reserve` | ≤ 120s |
| `max_attempts` | ≤ 5 |
| `frontend_timeout_ms` | ≤ 300_000 |

Values above these limits raise `ValueError` at construction time.

## Out of Scope (this issue)

- Rate limiting, quotas, request IDs (#268)
- Transactions, CAS, outbox (#270–#272)
- Full frontend reconciliation (#277)
- Circuit breaker
- Streaming
- Auth, RLS, schema migrations
- Emotional core or relationship changes
- Prompt or personality changes
