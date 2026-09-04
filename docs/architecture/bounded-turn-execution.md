# Bounded Turn Execution

## Problem

The `/chat` endpoint had no hard deadline, unlimited retries, and consumed
cancellations until the turn completed. Failures in appraisal or generation
were silently replaced with neutral fallbacks or hardcoded text that was
then persisted as a valid Katherine response.

Additionally:
- `asyncio.wait_for(asyncio.to_thread(...))` was used for both reads and
  writes. The timeout only bounds the **await**, not the thread. For read
  operations this is acceptable. For writes (`save_turn`, `sync_state`), the
  thread can continue executing past the timeout, corrupting state or running
  without the user lock.
- Archival extraction used a blocking `Groq` client inside a coroutine,
  blocking the event loop.
- The commit section had no protection against repeated cancellations
  unshielding `commit_task`.
- Provider error codes lacked `provider_invalid_request` for non-retryable
  4xx errors.

## Solution

A monotonic deadline (`time.monotonic`) governs every turn. Stages before
persistence are fully cancellable. Only the commit section
(`save_turn` + `sync_state`) is shielded against cancellation.

Two semantically distinct helpers replace the single
`run_blocking_with_deadline`:

- **`run_blocking_read`** — for read-only operations. Uses
  `asyncio.wait_for(to_thread(...))` with a timeout derived from
  `remaining_before_reserve` (so reads never consume the commit reserve).
  Thread continuation after timeout is acceptable because there is no state
  to corrupt.
- **`run_blocking_write`** — for write operations. Does **not** use
  `wait_for(to_thread(...))`. Instead the real timeout comes from the
  PostgREST transport configuration (`ClientOptions(postgrest_client_timeout=...)`).
  The coroutine awaits the thread to completion, so writes are never
  abandoned.

## Turn Stage Sequence

```
lock acquisition → load_state → load_context → appraisal → transition → generation → commit
     │                │              │             │           │            │          │
     └── bounded    └── read       └── read      └── async   └── pure     └── async  └── shielded
         wait_for      helper        helper         LLM         domain       LLM        write
                                                                                       helpers
```

1. **lock acquisition** — Time-bounded by `remaining_before_reserve` via
   `asyncio.wait_for` wrapping only `ctx.__aenter__()`. Once acquired,
   the turn runs under budget checks (not an outer `wait_for`).
2. **load_state** — Bounded via `run_blocking_read()` helper.
3. **load_context** — Bounded via `run_blocking_read()` helper.
4. **appraisal** — Async LLM call via `AsyncGroq` with deadline budget.
5. **transition** — Pure domain: emotional + relationship transition.
6. **generation** — Async LLM call via `AsyncGroq` with deadline budget.
7. **commit** — `save_turn()` + `sync_state()` via `run_blocking_write()`
   within a named task, shielded with explicit deadline tracking for
   post-cancel wait.

## Read vs Write Blocking Helpers

### `run_blocking_read`

```python
async def run_blocking_read(
    stage_label: str,
    budget: TurnBudget,
    supabase_timeout: float,
    func: Callable[..., T],
    *args, **kwargs,
) -> T:
```

- Uses `budget.remaining_before_reserve` (never consumes the commit reserve).
- `timeout = min(remaining_before_reserve, supabase_timeout)`.
- Uses `asyncio.wait_for(asyncio.to_thread(func, ...), timeout=timeout)`.
- On **timeout**: raises `DeadlineExceeded` (`turn_timeout`).
- Thread may continue after timeout — acceptable for read-only operations.

### `run_blocking_write`

```python
async def run_blocking_write(
    stage_label: str,
    budget: TurnBudget,
    supabase_timeout: float,
    func: Callable[..., T],
    *args, **kwargs,
) -> T:
```

- Does **NOT** use `asyncio.wait_for`.
- The real timeout comes from the PostgREST transport configuration
  (`postgrest_client_timeout` set to `supabase_timeout`).
- Uses bare `asyncio.to_thread(func, ...)` — the coroutine awaits the
  thread to completion.
- Budget check uses `budget.remaining` (full remaining including reserve).
- On **cancellation**: the underlying thread continues (it's not cancellable)
  but the coroutine stops waiting. The caller must retain the user lock
  until the task completes.

### Why `wait_for(to_thread())` is Unsafe for Writes

The `asyncio.wait_for` timeout only releases the **await**, not the thread.
The function already running in the thread pool continues executing. For
writes (`save_turn`, `sync_state`), this means:

- The lock may be released before the write completes.
- A second request for the same user can begin while the first write is
  still running.
- The write may commit after the lock is released, causing out-of-order
  persistence.

`run_blocking_write` avoids this entirely by never timing out the await.
The real timeout is enforced by the HTTP transport.

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
- **Pre-commit stages** use `remaining_before_reserve` so they never
  consume the reserve. The commit section requires `has_reserve` (the
  full reserve must be available) before starting.

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

## Commit Cancellation Protocol

1. A named `commit_task` (`"turn-commit"`) runs `save_turn()` and
   `sync_state()` via `run_blocking_write()`.
2. If cancellation arrives, `asyncio.CancelledError` is caught and the
   original exception is saved as `original_cancel`.
3. A **fixed** deadline is computed once: `commit_deadline = now() + budget.remaining`
   (not recomputed on each iteration).
4. The post-cancel wait loops:
   - `asyncio.wait_for(asyncio.shield(commit_task), timeout=remaining)`
   - If `CancelledError` arrives again, the shield prevents it from
     reaching `commit_task`, and the loop continues.
   - If the commit completes, `break` and propagate `original_cancel`.
   - If the deadline expires, `break` (the underlying thread has PostgREST
     transport timeout).
5. The lock is held throughout (the entire sequence runs inside
   `_run_turn_locked`'s `try/finally` block with `ctx.__aexit__`).

Key points:
- `commit_task` is **never** cancelled — shield prevents it.
- No `max(...)` or grace period that silently extends the deadline.
- The task name (`"turn-commit"` or `"turn-commit"`) contains no user data.
- After the post-cancel wait ends, `original_cancel` is always re-raised.

## LanguageModel Boundary (issue #337)

The domain (engine, `process_turn`, companion runtime, HTTP layer)
speaks **only** to the canonical `LanguageModel` contract
(`backend/language_model.py`):

- `appraise(message, budget)` — emotional appraisal (JSON mode inside the adapter);
- `generate(messages, budget)` — response generation;
- `extract_archival(messages, budget)` — long-term-memory fact extraction
  (keeps its own contracted call shape: fast model, JSON mode,
  temperature 0, explicit token limit, `archival_extraction` stage);
- `describe()` — sanitized provider/model identification.

The contract module is provider-agnostic by construction: it carries
only the Protocol, the sanitized selection/failure types and the
failure → `TurnErrorCode` mapping. It deliberately contains **no
provider registry, no `SUPPORTED_PROVIDERS` set and no dynamic
`backend.{provider}_language_model` import convention** — the concrete
provider choice lives exclusively in the composition roots
(`backend/dependencies.py` on the web, `backend/desktop/app.py` on the
desktop), which close directly over the concrete adapter builder
(`build_groq_language_model_factory` in the Groq adapter itself).

**Cancellation semantics.** `asyncio.CancelledError` is control flow,
not a provider failure. The adapter propagates it **immediately and
natively** — no retry, no translation into `LanguageModel*Error`, no
second provider call after cancellation (pinned by a deterministic
adapter test). The `ModelFailure` taxonomy therefore has no `cancelled`
code: the dead API was removed rather than pretending to represent
task cancellation.

Groq is a first-class remote provider **behind an explicit adapter**
(`backend/groq_language_model.py`). No Groq symbol (SDK type, manager,
exception) crosses the adapter upward; failures surface as the canonical
`LanguageModel*Error` taxonomy with constant sanitized messages, and the
existing failure → `TurnErrorCode` mapping is preserved bit a bit
(minus the removed dead `cancelled` code).
Selection is explicit at composition time — there is **no auto-routing
and no fallback to another provider**: a provider failure is a turn
failure. The composition roots capture the provider keys and the
provider-call parameters from the application settings at composition
time (`Settings.provider_keys()` / `Settings.turn_config` on the web;
the desktop root reads its own key source), so the adapter never
falls back to the process environment. The ``/ready`` provider check
consumes a small injectable ``provider_configured_probe`` bound to the
same captured configuration — a factory's existence alone never
reports "configured"; the probe is presence-only (no SDK instantiation,
no inference call, no secret echo). The trusted system policy is a
Katherine-core responsibility (`build_trusted_policy` in
`backend/trusted_policy.py`), never a provider capability. Honest
disclosure: the prompt content still travels to the configured remote
provider; nothing else about the turn does.

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

### Sync Clients (Archival Extraction — Sync Path Kept for Backward Compat)

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
- Non-retryable 4xx errors (400, 403, 404, 405, etc.) produce
  `GroqRequestError` in sync path; in async path they produce
  `ProviderFailure.invalid_request`.
- Backoff: exponential with jitter, total capped at `max_backoff`.
- `_acquire_next_key()` distinguishes pool states with `ProviderFailure` codes:
  `auth_failed` (all deactivated), `rate_limited` (all cooldown),
  `connection_failed` (all tried).

## Cancellation Semantics

- **Before commit**: Cancellation (`asyncio.CancelledError`) propagates
  immediately through the `try/finally` in `_run_turn_locked`, which calls
  `ctx.__aexit__()` to release the lock. No persistence occurs.
- **During commit**: See [Commit Cancellation Protocol](#commit-cancellation-protocol)
  above.
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

## ProviderFailure → TurnErrorCode Mapping

| ProviderFailure | TurnErrorCode | HTTP |
|----------------|---------------|------|
| `rate_limited` | `upstream_rate_limited` | 429 |
| `auth_failed` | `provider_invalid_request` | 503 |
| `invalid_request` | `provider_invalid_request` | 503 |
| `connection_failed` | `provider_unavailable` | 503 |
| `server_error` | `provider_unavailable` | 503 |
| `timeout` | `turn_timeout` | 504 |
| `invalid_response` | `provider_invalid_response` | 500 |
| `cancelled` | `internal_error` (not used — propagated) | — |

## Provider Error Classification

The `classify_provider_error()` function classifies Groq SDK exceptions
deterministically:

| SDK Exception | ProviderFailure |
|---------------|-----------------|
| `RateLimitError` | `rate_limited` |
| `AuthenticationError` | `auth_failed` |
| `APITimeoutError` | `timeout` |
| `APIConnectionError` | `connection_failed` |
| `APIStatusError` (401) | `auth_failed` |
| `APIStatusError` (4xx) | `invalid_request` |
| `APIStatusError` (5xx) | `server_error` |
| `asyncio.CancelledError` | `cancelled` |
| `asyncio.TimeoutError` | `timeout` |
| Unknown exception | `invalid_response` |

This preserves exact codes: 4xx → `provider_invalid_request`, 429 →
`upstream_rate_limited`, timeout → `turn_timeout`, connection/5xx →
`provider_unavailable`, invalid response → `provider_invalid_response`.

## Observability

Structured low-cardinality log events:

```
event=turn_stage_completed stage=generation outcome=success duration_ms=120
event=turn_stage_completed stage=appraisal outcome=failed code=provider_invalid_response
event=turn_stage_completed stage=generation outcome=cancelled
event=commit_timeout_after_cancel
event=emotional_appraisal_fallback code=...
```

Stage events include `code` for `TurnExecutionError` and `GroqPoolExhaustedError`
failures. Events are emitted for:
- `load_state` — success, timeout (code=turn_timeout), failed (code=persistence_unavailable)
- `load_context` — success, timeout, failed
- `appraisal` — success, failed (with `exc.code` from `TurnExecutionError`)
- `transition` — success
- `generation` — success, failed (with `exc.code`)
- `commit` — success, cancelled, failed (code=persistence_unavailable)

Never logged: `user_id`, message content, prompt, response, key, token,
DB IDs, or exception text.

## Frontend

- `AbortController` created per request, stored in ref, cleaned up on
  success/error/cancel/unmount.
- `requestTokenRef` (monotonically increasing) prevents stale `finally`
  blocks from clearing the controller/timer of a newer request or changing
  `isLoading` of a request that already completed.
- **`inFlightRef`** (synchronous `useRef(false)`) prevents double submission
  before rerender. The check occurs at the start of `handleSend()` before
  any optimistic effects (message addition, input clear, controller creation).
  Only the owning request clears `inFlightRef` in the `finally` block.
- Timeout timer at 50s (configurable) aborts the controller.
- `AbortSignal` forwarded to Axios via the `signal` option.
- Error responses classified by HTTP status: 504 → timeout, 429 →
  rate_limited, 503 → service_unavailable, 422 → validation.
- Axios error objects are never logged to console directly.

## Archival Extraction (Async Path)

`run_archival_extraction()` is a background task scheduled after the
commit section completes. It:

1. Creates its own monotonic budget (15s deadline, isolated from the main
   turn budget).
2. Loads the persisted user message via `run_blocking_read()`.
3. Calls the LLM via `groq_manager.chat_completion_async()` (async, with
   its own budget) — does **not** block the event loop.
4. Parses and validates the extraction result.
5. Stores the extraction via `run_blocking_write()` — the write is
   awaited to completion (not abandoned by timeout).
6. `ArchivalDuplicateError` (from unique constraint violation) is handled
   structurally, not by parsing exception text.
7. On any failure in loading, LLM call, or storage, logs a structured
   event and returns — never modifies emotional or relationship state.
8. Disabled by default (`ARCHIVAL_EXTRACTION_ENABLED`).

## `TURN_MAX_JITTER=0` Acceptance

`TurnExecutionConfig.from_env()` uses `_parse_nonnegative_float()` for
`TURN_MAX_JITTER`, which accepts:
- `0` and `0.0` (zero jitter)
- Any finite non-negative float up to `1.0`

Rejected:
- Negative numbers
- Boolean strings (`true`, `false`)
- Empty string
- `NaN`, `inf`, `-inf`
- Invalid text
- Values greater than `1.0`

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
