# Implementation Plan: Local Desktop Companion Runtime (no Supabase, no login)

**Branch**: `refactor/local-desktop-runtime` | **Date**: 2026-09-01 | **Spec**: `specs/001-local-desktop-runtime/spec.md`

**Input**: Feature specification from `specs/001-local-desktop-runtime/spec.md` (issue #336, tracking #333)

## Summary

Transform the approved #334 pywebview Linux shell and #335 LocalStorage SQLite
foundation into the real runtime of Katherine's independent desktop mode.
The desktop app must start with zero Supabase credentials, open directly
into the companion conversation (no AuthPage), load history and persisted
emotional/relationship state, process new turns (appraisal → transitions →
generation → atomic commit) and expose the four local privacy operations,
with the UI talking to Python exclusively through the allowlisted,
fail-closed bridge — no HTTP server, no Supabase anything; only the remote
LLM provider may touch the network.

Technical approach (from repository research):

1. **New module `backend/desktop/runtime.py`** — a `CompanionRuntime` that
   owns the whole desktop-side lifecycle: opens LocalStorage
   (`open_local_storage`), constructs a `ConversationEngine` with
   `supabase_factory=lambda: None` (env-pure, no Supabase client ever
   constructed), drives turns through the same domain path as the web
   (`ProcessTurn`-style flow reused, but repositories bound to LocalStorage
   instead of PostgREST), maps every domain/storage error to a sanitized
   stable `code`, and closes terminally on shutdown.
2. **Bridge evolution `backend/desktop/api.py`** — extend the explicit
   allowlist `DESKTOP_API_METHODS` from `("health",)` to the minimal set
   this feature needs: `runtime_state`, `load_history`, `send_message`,
   `delete_history`, `delete_memories`, `reset_emotional_state`,
   `reset_relationship_state`. Every method validates input shape/bounds
   in Python, never raises, and returns plain JSON-serializable payloads
   with stable shapes. `app.py`'s `LocalBuildBridge` gets one generic
   wrapper loop over the allowlist so each new method is covered by the
   same local-build-only trust gate (no generic dispatch: the wrapper set
   is still the fixed allowlist).
3. **Turn flow reuse** — `process_turn.py` stages are re-implemented as a
   desktop use case bound to LocalStorage (load state+revision via
   `LocalStorage.load_user_state`, load context via a small local adapter
   over `LocalStorage.load_recent_history` + profile snapshot producing
   `LoadedContextData`, provider stages via the engine's existing
   `appraise`/`build_trusted_policy`/`generate`, commit via
   `LocalStorage.commit_turn` with `expected_revision` CAS + bounded
   single retry + idempotent replay of the same request_id+payload).
   The web module `process_turn.py` is NOT modified (its repositories
   are Supabase-shaped); the desktop flow calls the same pure domain
   functions (`migrate_legacy_snapshot`, `transition`,
   `transition_relationship`, `build_context_bundle`, `build_envelope`,
   `project_public_emotion`) and mirrors the same stage order.
4. **Frontend** — `App.jsx` branches once on `isDesktopShell()`: desktop
   renders `ChatWindow` directly (no AuthPage, no supabase import reached
   at runtime in that mode is not possible statically since supabaseClient
   is imported by apiClient; instead a new `services/chatTransport.js`
   boundary selects bridge vs axios per call, keeping web mode byte-for-byte
   behavior and desktop mode bridge-only). `useChat` is refactored to call
   the transport boundary with explicit `requestId`, preserving
   single-flight, loading/failure states, unmount guards and timeout
   semantics. The desktop entry (`index.html`) stays the same bundle;
   mode detection already exists (`runtimeMode.js`).
5. **Tests** — integrated backend tests with real temporary SQLite
   databases (first turn in empty DB, restart recovery, idempotent
   replay/payload conflict, revision CAS retry, privacy ops transactional
   semantics, corrupt DB fail-closed, provider-unconfigured behavior,
   bridge input validation, allowlist surface equality, no-Supabase
   construction). Frontend tests for the transport boundary and desktop
   App entry. Reproducible smoke extended to run the real flow
   (UI → bridge → core → SQLite) plus a restart loop.

## Technical Context

**Language/Version**: Python 3.12 (backend, uv venv), JavaScript ES2022 +
React 18 + Vite 5 (frontend, node v22)

**Primary Dependencies**:
- Backend: pywebview 6.2.1 (GTK/WebKitGTK), stdlib `sqlite3`,
  `groq` SDK (only remote-network dependency), pydantic v2, FastAPI
  (web path only — NOT imported by desktop runtime), python-dotenv.
- Frontend: axios (web path only), @supabase/supabase-js (web path only),
  Vitest + Testing Library.

**Storage**: SQLite via `backend/local_storage/` (#335) — `LocalStorage`
class with WAL, `synchronous=FULL`, atomic `commit_turn` (BEGIN IMMEDIATE,
revision CAS, idempotency by canonical payload hash), replay, transactional
privacy deletes, neutral resets, migrations v1, terminal `close()`.

**Testing**: pytest (backend, with real temp SQLite files via `tmp_path`),
Vitest + jsdom (frontend), reproducible pywebview smoke under `xvfb-run`.

**Target Platform**: Linux desktop (WebKitGTK via pywebview); web mode
unchanged (FastAPI + Supabase).

**Project Type**: desktop-app (pywebview shell around Vite build) plus the
existing web-service.

**Performance Goals**: first interactive window < a few seconds (GTK
startup dominated); turn latency bounded by `TurnExecutionConfig`
(total_deadline 45s, provider attempt 15s, commit reserve 10s) reused
from the web path.

**Constraints**: no daemon/polling/extra process (constitution I); bridge
allowlist only, fail-closed, local-build-only (constitution III);
LocalStorage is the sole persistence (IV); login-free single-user (V);
no secrets in JS (VIII); no network except the LLM provider.

**Scale/Scope**: single-user single-process; history windows bounded
(1..500 rows per `load_recent_history` contract).

### Key research findings (with classification)

- **[NEVER DEVIATE]** `LocalStorage.commit_turn` signature:
  `commit_turn(*, request_id, user_message, assistant_message,
  emotional_state, relationship_state, public_response, replay_payload,
  outbox_events=None, expected_revision=None)` — validation, idempotency
  (completed same-hash replays without writing; divergent hash is
  `ConflictError("request_payload_conflict")`; pending is
  `request_in_progress`; failed same-hash proceeds as fresh attempt),
  then `BEGIN IMMEDIATE` … revision CAS … inserts … `COMMIT`.
- **[NEVER DEVIATE]** `LocalStorage.load_user_state()` returns
  `LoadedUserState(revision, persona_config, user_profile,
  emotional_state, relationship_state)` with neutral v1 defaults on a
  missing profile row (contract parity with the web
  `UserStateRepository.load`).
- **[NEVER DEVIATE]** Bridge surface rule (#334): pywebview exposes every
  public attribute of the `js_api` object; the facade must keep the public
  surface exactly `DESKTOP_API_METHODS`, no method may ever raise, and
  `LocalBuildBridge` must re-check trust on every call (URL + committed
  trust, revoke-before-revert state machine).
- **[NEVER DEVIATE]** `trusted_context.LoadedContextData` requires
  history rows as tuple of dicts with `role`/`content`/`id`/`created_at`
  (id positive int, created_at parseable), `retrieved_memories` tuple
  (empty tuple acceptable), `profile_snapshot` dict, `persona_snapshot`
  str; `build_context_bundle` + `build_envelope` then produce provider
  messages.
- **[NEVER DEVIATE]** Correlation: `ProcessTurnInput.correlation` must be
  a 64-char lowercase hex HMAC in the web flow; the desktop runtime has no
  admission HMAC secret, so the desktop turn input uses a *different*
  DTO (see below) rather than faking a correlation. Domain pure functions
  do not require correlation; logging in the desktop runtime uses only
  stable event names (never message content).
- **[RESEARCH DONE]** `ConversationEngine.__init__(clock,
  archival_extraction_enabled, embeddings_enabled, turn_config, *,
  groq_keys, supabase_factory)` builds `MemoryManager` with
  `supabase_factory`; passing `lambda: None` yields
  `memory_manager.supabase is None` and every MemoryManager method fails
  with its own sanitized errors — the desktop runtime therefore never
  uses MemoryManager for I/O; it uses the engine only for the provider
  port methods (`appraise`, `generate`, `build_trusted_policy`) plus its
  transition configs.
- **[RESEARCH DONE]** `build_process_turn(engine)` wires Supabase-shaped
  repositories (`client_provider = lambda: engine.memory_manager.supabase`)
  — unusable for desktop; the desktop flow constructs its own use case
  with LocalStorage-bound adapters, reusing `ProcessTurn._run_once`'s
  stage order (load state → load context → appraisal → transitions →
  envelope → generation → snapshots → commit) and the same public result
  parser semantics (`parse_public_result` requires `response` +
  `emotion_state` in the replay payload — desktop reuses this function
  from `process_turn.py`, which is import-pure w.r.t. HTTP).
- **[RESEARCH DONE]** `TurnExecutionConfig` validation invariants
  (connect ≤ attempt < total; commit_reserve ≥ 2*supabase_timeout;
  reserve < total) all hold for defaults; desktop reuses the same config
  and `TurnBudget`/`run_blocking_read`/`run_blocking_write` helpers
  (they take `supabase_timeout` only as a scalar per-call bound; nothing
  Supabase-specific).
- **[RESEARCH DONE]** Frontend mode detection `detectRuntimeMode()`/
  `isDesktopShell()` (positive `window.pywebview` signal) exists from
  #334; `useChat` currently does axios `api.get('/history')` and
  `sendMessage()` posts `/chat` with `{request_id, message}`; response
  shape consumed: `{response, emotion_state}` (+ optional fields).
  `validateEmotionState` from `shared/utils/formatters` sanitizes the
  emotion payload.
- **[RESEARCH DONE]** The web `/history` route returns `list[dict]` with
  `id`, `role`, `content`, `created_at` (matching `load_recent_history`
  output shape) — the desktop `load_history` bridge op can return the
  same row shape so the UI renders identically.
- **[RESEARCH DONE]** `ChatConversationEngine.process_turn` acquires
  `UserLockManager` per user id; desktop is single-process single-user
  with LocalStorage's own RLock + SQLite busy timeout, but reusing the
  engine's lock keeps the single-flight invariant defensively. Desktop
  user id is a fixed local constant (`"local"`) used only as a lock key;
  it never reaches storage or prompts.
- **[NEEDS RESEARCH → RESOLVED]** Memories retrieval: web path uses
  Supabase RPC retrieval via `MemoryManager.get_context`; desktop uses
  an empty `retrieved_memories=()` (spec assumption: no vector retrieval
  in the desktop context loader this iteration; archival extraction
  disabled by default both paths).
- **[NEEDS RESEARCH → RESOLVED]** `groq_keys.get_groq_api_keys()` reads
  `backend/.env` (dotenv) only on Python side; `GroqClientManager` raises
  `GroqConfigurationError` when keys are empty/whitespace — desktop maps
  this to `provider_not_configured` at send time only (FR-016).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I (Reliability ordering)**: PASS — reuse of the transactional
  LocalStorage and the bounded turn execution machinery; failures are
  explicit and sanitized; no new daemon/process/polling introduced.
- **II (Python domain authority)**: PASS — all turn logic, storage,
  privacy, keys stay in Python; the frontend only renders and calls the
  allowlisted bridge.
- **III (Allowlisted fail-closed bridge)**: PASS — allowlist grows only
  with the seven explicitly needed ops; every op validates and returns
  sanitized payloads; `LocalBuildBridge` trust gate extended
  mechanically to all ops (still no generic dispatch).
- **IV (LocalStorage sole persistence)**: PASS — desktop runtime uses
  `backend/local_storage` exclusively; no second store, no ORM; corrupt
  DB surfaces `StorageCorruptError` without reset.
- **V (Login-free single user)**: PASS — no AuthPage in desktop mode, no
  supabase client constructed (factory returns None; no env read),
  fixed local identity constant.
- **VI (Test-first)**: PASS — tasks below are test-first; integrated
  tests use real temp SQLite; provider mocked only where determinism
  requires; no existing suite removed/weakened (SC-004).
- **VII (Real transactional privacy)**: PASS — the four privacy ops are
  bridge-reachable and delegate to LocalStorage's transactional
  implementations; no account semantics added.
- **VIII (Provider decoupled)**: PASS — keys stay Python-side
  (`backend/.env`); unconfigured provider only errors at send.
- **Scope discipline**: PASS — one branch off `main` (`fd681df`), no
  stacked PRs; web path untouched functionally.

## Project Structure

### Documentation (this feature)

```text
specs/001-local-desktop-runtime/
├── plan.md              # This file
├── spec.md              # Approved specification
└── checklists/requirements.md
```

### Source Code (repository root)

```text
backend/
├── desktop/
│   ├── __init__.py
│   ├── api.py                  # extend: allowlist + 7 ops (validated, never raising)
│   ├── app.py                  # extend: LocalBuildBridge generic trust wrapper; runtime lifecycle wiring
│   ├── build_resolver.py       # unchanged (#334)
│   └── runtime.py              # NEW: CompanionRuntime (LocalStorage + engine + turn flow + error mapping)
├── local_storage/              # unchanged (#335) — sole persistence
├── process_turn.py             # unchanged web use case (desktop reuses parse_public_result)
├── tests/
│   ├── test_desktop_api.py            # extend: new ops validation/sanitization/allowlist equality
│   ├── test_desktop_navigation.py    # extend: trust gate for all ops
│   ├── test_desktop_runtime.py       # NEW: integrated runtime tests (real temp SQLite)
│   └── test_desktop_app_runtime.py   # NEW: shell wiring (runtime opened/closed with window)
├── conftest.py                 # unchanged (mock GROQ keys for unit tests)
scripts/
└── desktop_smoke.py            # extend: real send through UI → SQLite; restart recovery probe

frontend/
├── src/
│   ├── App.jsx                      # desktop mode renders ChatWindow directly
│   ├── features/chat/
│   │   ├── hooks/useChat.js         # transport boundary; explicit requestId; desktop ops
│   │   ├── services/chatService.js  # unchanged web axios service (kept)
│   │   └── services/chatTransport.js # NEW: mode-aware boundary (bridge vs axios)
│   ├── features/privacy/
│   │   └── PrivacyPanel.jsx         # NEW (desktop-only): 4 local privacy ops UI
│   └── lib/
│       ├── desktopBridge.js        # extend: op callers (health/loadHistory/sendMessage/...)
│       └── runtimeMode.js          # unchanged
└── tests/
    ├── chatTransport.test.js       # NEW
    ├── desktopChat.test.js         # NEW (desktop mode useChat behavior)
    └── App.desktop.test.jsx        # NEW
```

**Structure Decision**: existing two-tree `backend/` + `frontend/`
repository layout (matches Option 2 shape); all new backend code inside
`backend/desktop/`, all frontend changes inside the existing feature
folders. No new top-level directories.

## Milestone Plan and Implementation Model

### Data/process flow (final architecture)

```text
┌───────────── Vite build (file://) ─────────────┐
│ ChatWindow ── useChat ── chatTransport          │
│     │ (desktop mode: bridge only)               │
│     ▼                                           │
│ window.pywebview.api.<op>()  (JS)               │
└────────────────────┬────────────────────────────┘
                     │ pywebview js_api (GTK dispatch thread)
┌────────────────────▼────────────────────────────┐
│ LocalBuildBridge (trust gate: URL + BuildTrust) │  #334 layer, unchanged semantics
│   └── DesktopBridge facade (allowlist wrappers) │
│         └── DesktopApi ops (validate, map)      │
│               └── CompanionRuntime               │ NEW
│                     ├─ LocalStorage (#335)       │ sole persistence
│                     ├─ ConversationEngine        │ provider port only
│                     │   (supabase_factory=None)  │ never used for I/O
│                     ├─ LocalTurnFlow            │ stage order of ProcessTurn,
│                     │                           │ repositories bound to SQLite
│                     └─ sanitized error mapping   │ stable codes
└────────────────────┬────────────────────────────┘
                      ▼
              Groq (remote LLM) — only network hop
```

### Bridge operation contracts (stable, JSON-serializable)

All ops return `{"ok": true, ...}` or `{"ok": false, "code": <stable>,
"message": <pt-BR, sanitized>}`. Payload bounds mirror LocalStorage
contracts (`MAX_MESSAGE_LENGTH`, request id charset/length).

| Op | Args | Success payload | Notable errors |
|----|------|------------------|----------------|
| `health()` | — | `{ok, api_version}` | (unchanged #334) |
| `runtime_state()` | — | `{ok, ready: bool, storage: "ok"\|"corrupt"\|"unavailable", provider_configured: bool, api_version}` | sanitized storage/provider status only |
| `load_history(limit?)` | int 1..500 (default 50) | `{ok, messages: [{id, role, content, created_at}]}` | `invalid_input`, `storage_*` codes |
| `send_message(request_id, message)` | canonical UUID string; message 1..MAX_MESSAGE_LENGTH | `{ok, response, emotion_state, revision}` | `invalid_input`, `provider_not_configured`, `rate_limited`, `provider_unavailable`, `turn_timeout`, `request_in_progress`, `request_conflict`, `storage_error`, `internal_error` |
| `delete_history()` | — | `{ok, result: {status, deleted_messages, ...}}` | `storage_*` |
| `delete_memories()` | — | `{ok, result: {status, deleted_memories}}` | `storage_*` |
| `reset_emotional_state()` | — | `{ok, result: {status, revision}}` | `storage_*` |
| `reset_relationship_state()` | — | `{ok, result: {status, revision}}` | `storage_*` |

Error-code mapping table (desktop runtime internal → bridge code):
`ValidationError→invalid_input` (bounds), `ConflictError(request_in_progress)
→request_in_progress`, `ConflictError(request_payload_conflict)→request_conflict`,
`ConflictError(revision_mismatch exhausted)→request_conflict` (bounded retry
happened first), `GroqConfigurationError→provider_not_configured`,
`ProviderFailure.rate_limited→rate_limited`,
`ProviderFailure.{connection_failed,server_error}→provider_unavailable`,
`ProviderFailure.timeout / DeadlineExceeded→turn_timeout`,
`PersistenceError/StorageCorruptError→storage_error` (constant message,
original file untouched), anything else→`internal_error`.

### Desktop turn flow (LocalTurnFlow stage order = web ProcessTurn)

1. validate request_id/message (bridge layer bounds; flow layer trusts types)
2. `LocalStorage.load_user_state()` → revision + snapshots
3. migrate snapshots via `migrate_legacy_snapshot` /
   `migrate_legacy_relationship_snapshot` (fail → `internal_error`)
4. build `LoadedContextData(history_rows=LocalStorage.load_recent_history(),
   retrieved_memories=(), profile_snapshot, persona_snapshot)`
   → `build_context_bundle` → `build_envelope`
5. `engine.appraise(message, budget)` → `transition` + `transition_relationship`
6. `engine.build_trusted_policy(new_state, relationship)`
   (envelope from step 4)
7. `engine.generate(messages, budget)`
8. `project_public_emotion(new_state, appraisal)` → replay payload
   `{response, emotion_state, message_id, duration_ms}`
9. `LocalStorage.commit_turn(..., expected_revision=state.revision)`;
   on `ConflictError(revision_mismatch)` → exactly one bounded retry
   (reload state, redo 5–9 with fresh budget-derived deadline checks);
   on `request_in_progress`/`request_payload_conflict` → surface code.
10. `parse_public_result(committed)` → `{response, emotion_state, revision}`.

Locking: reuse `engine.lock_manager.lock("local")` with
`budget.remaining_before_reserve` timeout exactly like
`ChatConversationEngine._run_turn_locked` (defensive single-flight).

### Frontend transport boundary

`chatTransport.js` exports:
- `fetchHistory({signal})` → web: axios `/history`; desktop:
  `bridge.loadHistory()` mapped to `{data: [...]}`.
- `sendMessage(message, {signal, timeout}, requestId)` → web: axios
  `/chat`; desktop: `bridge.sendMessage(requestId, message)` mapped to
  the same consumed shape `{response, emotion_state}` or a thrown
  `ChatError` classified from the stable bridge codes.
- `runPrivacyOp(name)` → desktop only; web mode callers never reach it.
Desktop bridge callers live in `desktopBridge.js` (single file touching
`window.pywebview`), preserving the #334 audit rule.

`useChat.js` changes:
- inject transport (default = mode-aware `chatTransport`);
- history fetch and send go through the boundary;
- AbortController/timeout retained for web; desktop uses the same
  50s timer to guard against a hung bridge call (clears on resolve);
- messages shape `{role, content}` for rendering; history rows already
  match; single-flight/token/unmount invariants untouched.
- desktop-only `privacy` ops exposed via a small `useDesktopPrivacy`
  hook consumed by `PrivacyPanel` (desktop mode only).

`App.jsx`:
```jsx
if (isDesktopShell()) return <ChatWindow />;  // no AuthPage, no supabase session read
// existing web flow unchanged below
```
`supabaseClient.js` is still imported (by apiClient) but in desktop mode
its client is null (no env credentials in the desktop build) and no
`supabase.auth` call is made on the desktop branch.

## Security Validation Checklist

*Threat model: the JS environment inside the webview is the untrusted
rendering surface; remote content must never reach Python; SQLite must
never be reset silently; secrets never cross to JS.*

- Bridge inputs validated in Python (types, lengths, charset) before any
  storage/provider call; oversized/incorrect types → `invalid_input`.
- No method ever raises across `js_api` (facade wraps everything).
- No path, SQL, traceback, key, prompt content, or provider message text
  in any bridge payload; messages shown to the UI are the user's own
  conversation content only (the response + history rows the user
  already sees).
- Local-build-only trust gate covers every op (extended mechanically);
  revert race window still closed (`BuildTrust` unchanged).
- No `eval`/`getattr`-driven dispatch: wrapper set is the literal
  allowlist tuple.
- LocalStorage failure modes fail closed; no reset/recreate of the DB
  file; `close()` terminal.
- No Supabase client is constructed in desktop runtime
  (`supabase_factory=lambda: None`); no `SUPABASE_*` env read on the
  desktop path; dotenv load limited to `groq_keys` (backend/.env).
- No new subprocesses/threads beyond pywebview's own GTK dispatch and
  `asyncio.to_thread` pool used by turn execution helpers.
- Frontend: no secrets in bundle (unchanged); bridge module is the only
  `window.pywebview` toucher; `PrivacyPanel` confirmations required for
  destructive ops (UX guard, storage stays transactional regardless).

## Milestones & Tasks Outline

*(task-level detail generated into `tasks.md` by `setup-tasks.sh`)*

1. **Backend runtime core** — `CompanionRuntime` + `LocalTurnFlow` +
   error mapping, unit-tested against real temp SQLite with a fake
   provider port (TDD).
2. **Bridge surface** — extend allowlist + validation + facade wrappers
   + trust-gate extension; tests for each op's validation/sanitization
   and allowlist equality; app wiring opens runtime before window and
   closes it after `webview.start()` returns.
3. **Frontend** — `chatTransport`, `desktopBridge` op callers, `useChat`
   refactor (tests first), desktop App branch, `PrivacyPanel`.
4. **Integrated tests** — restart recovery, first-turn-in-empty-DB,
   idempotency/conflict/CAS-retry, privacy transactionality, corrupt
   DB, provider-unconfigured, no-Supabase-construction proof.
5. **Smoke** — extend `scripts/desktop_smoke.py`: send through real UI
   → assert SQLite rows; close; relaunch against same temp DB → assert
   recovered history; keep all #334 probes green.
6. **Verification** — full backend pytest, frontend lint+test+build,
   smoke under xvfb; document numbers for the PR.

## Prepared Changesets

- **CS-1 (backend desktop runtime)**: new `backend/desktop/runtime.py`;
  no changes to `backend/local_storage/*`, `backend/process_turn.py`,
  `backend/engine.py`, `backend/chat_engine.py`, `backend/memory.py`.
- **CS-2 (bridge)**: `backend/desktop/api.py` allowlist/ops;
  `backend/desktop/app.py` trust wrapper generalization + runtime
  lifecycle (open before window; close in `finally` after start
  returns; runtime failures → sanitized startup error, window not
  opened, exit code 2 with actionable message).
- **CS-3 (frontend)**: `chatTransport.js`, `desktopBridge.js` callers,
  `useChat.js` transport injection, `App.jsx` desktop branch,
  `PrivacyPanel.jsx`, tests.
- **CS-4 (smoke)**: `scripts/desktop_smoke.py` flow + restart probes.
- **CS-5 (tests)**: `backend/tests/test_desktop_runtime.py`,
  `test_desktop_app_runtime.py`, extensions to `test_desktop_api.py`,
  `test_desktop_navigation.py`, frontend tests.

## Traceability

| Spec item | Plan element | Verification |
|-----------|---------------|--------------|
| FR-001 no login, direct companion | App.jsx branch; no supabase on desktop path | App.desktop.test.jsx; smoke "no auth page" probe |
| FR-002 no Supabase ops in desktop | runtime `supabase_factory=None`; no env reads | test_desktop_runtime.py::test_no_supabase_construction; smoke port probe |
| FR-003 bridge-only transport, no HTTP server | chatTransport desktop = bridge | chatTransport.test.js; smoke "no listening port" |
| FR-004 explicit allowlist, validated ops | api.py 7 ops | test_desktop_api.py allowlist equality + per-op validation |
| FR-005 sanitized failures | error mapping table | per-op sanitization tests (no traceback/paths) |
| FR-006 local-build-only bridge | LocalBuildBridge generic wrapper | test_desktop_navigation.py extension |
| FR-007 LocalStorage sole persistence | runtime uses LocalStorage only | code review + tests (no other store touched) |
| FR-008 atomic first turn | LocalTurnFlow commit_turn path | test_desktop_runtime.py empty-DB first turn |
| FR-009 idempotency/conflict semantics | commit_turn reuse | replay/payload-conflict tests |
| FR-010 restart recovery | runtime open → load history/state | restart test (temp SQLite), smoke stage 2 |
| FR-011 explicit storage failure | StorageCorruptError/PersistenceError mapping | corrupt-DB test (file untouched) |
| FR-012 clean shutdown | runtime.close() in finally | test_desktop_app_runtime.py lifecycle |
| FR-013 privacy ops via bridge | 4 ops + panel | bridge tests + transactional SQLite assertions |
| FR-014 no fake account semantics | none added; only 4 real ops | review |
| FR-015 keys Python-side only | groq_keys unchanged; no keys in payloads | bridge payload allowlist tests |
| FR-016 unconfigured provider | GroqConfigurationError → send-time code | provider-unconfigured test (open still works) |
| FR-017 explicit desktop detection + bridge flows | runtimeMode + chatTransport | unit tests |
| FR-018 frontend guarantees preserved | useChat refactor invariants | desktopChat.test.js (single-flight, loading, errors) |
| FR-019 web mode unchanged | boundary isolation; web tests stay | existing suites green (SC-007) |
| FR-020 lazy cloud imports | desktop path imports no supabase/FastAPI | import-purity test |
| FR-021 reproducible smoke | extended smoke | smoke run under xvfb |
| SC-001..SC-007 | milestones 1–6 | verification section of the PR |

## Complexity Tracking

> No constitution violations to justify. The only deliberate complexity is
> the parallel desktop turn flow (`LocalTurnFlow`) instead of refactoring
> `ProcessTurn` to be repository-agnostic mid-feature — rejected because
> touching the production web use case in the same PR violates scope
> discipline and risk isolation; the reuse happens at the pure-domain and
> parser level (`parse_public_result`, transitions, envelope builders),
> keeping a single source of truth for every domain rule.
