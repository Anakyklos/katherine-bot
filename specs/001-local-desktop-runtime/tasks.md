# Tasks: Local Desktop Companion Runtime (no Supabase, no login)

**Input**: Design documents from `/specs/001-local-desktop-runtime/` (plan.md, spec.md)

**Prerequisites**: plan.md (required), spec.md (required)

**Tests**: Required by constitution VI and spec SC-004/SC-005 — every implementation task is paired with tests, written first (TDD where practical).

**Organization**: Tasks are grouped by user story (US1 talk P1, US2 privacy P2, US3 honest failure P3) with a shared foundational phase.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to

## Phase 1: Foundational (Blocking Prerequisites)

**Purpose**: Desktop runtime core that every story depends on.

- [ ] T001 [US1] Write `backend/tests/test_desktop_runtime.py` skeleton: fixtures for real temp SQLite (`tmp_path`), fake provider port (deterministic appraise/generate), and `CompanionRuntime` construction with `supabase_factory=lambda: None`; assert runtime module is import-pure (no FastAPI, no supabase imports) — FR-020.
- [ ] T002 [US1] Implement `backend/desktop/runtime.py` core: `CompanionRuntime` owning `LocalStorage` (via `open_local_storage`), `ConversationEngine(clock, turn_config, groq_keys=..., supabase_factory=lambda: None)`, fixed local lock key, `TurnExecutionConfig.from_env` reuse, budget creation, and terminal `close()` (idempotent, closes store) — FR-002/FR-007/FR-012.
- [ ] T003 [US1] Implement desktop turn flow (`LocalTurnFlow` stage order from plan: load state → migrate snapshots → local context loader producing `LoadedContextData(history_rows, (), profile, persona)` → appraisal → transitions → trusted policy + envelope → generation → public emotion → `commit_turn` with `expected_revision`, bounded single revision retry, idempotency passthrough) — FR-008/FR-009.
- [ ] T004 [US1] Implement sanitized error mapping table in `runtime.py` (bridge codes: `invalid_input`, `provider_not_configured`, `rate_limited`, `provider_unavailable`, `turn_timeout`, `request_in_progress`, `request_conflict`, `storage_error`, `internal_error`; constant messages; no content echo) — FR-005.
- [ ] T005 [US1] `runtime_state()` support in runtime: storage status probe + provider-configured flag, no env echo — FR-016 groundwork.

**Checkpoint**: runtime core usable from Python; integrated tests green (turn committed to real temp SQLite).

## Phase 2: User Story 1 — Open and talk, no login, no cloud (P1) 🎯 MVP

**Independent Test**: launch desktop app with no Supabase env vars; chat UI opens directly; send one message; reply persisted in temp SQLite; relaunch recovers history.

### Tests for User Story 1 (write first)

- [ ] T006 [P] [US1] `backend/tests/test_desktop_api.py` extension: allowlist equality (`DESKTOP_API_METHODS == ("health","runtime_state","load_history","send_message","delete_history","delete_memories","reset_emotional_state","reset_relationship_state")`), facade exposes exactly allowlist, no method raises for any input (fuzz wrong types/extra args/oversized strings) — FR-004/FR-005.
- [ ] T007 [P] [US1] `backend/tests/test_desktop_navigation.py` extension: every new op fails closed for remote URL and mid-revert (parameterized over the full allowlist) — FR-006.
- [ ] T008 [US1] `backend/tests/test_desktop_runtime.py` integrated tests (real temp SQLite, fake provider): first turn in empty DB creates profile+messages+ledger atomically; replay same request_id+payload returns committed result without writing; divergent payload → `request_conflict` and nothing written; restart (close, reopen same path) recovers history + snapshots; concurrent CAS bump between load and commit → exactly one bounded retry then success or `request_conflict`; unconfigured provider → `runtime_state` ready but `send_message` → `provider_not_configured` and history still loads — FR-001/FR-008/FR-009/FR-010/FR-016.
- [ ] T009 [P] [US1] Frontend tests: `chatTransport.test.js` (desktop branch calls bridge ops with right args, maps errors to `ChatError` with stable types; web branch still axios), `App.desktop.test.jsx` (desktop mode renders ChatWindow without AuthPage), `desktopChat.test.js` (useChat via bridge: single-flight, loading, failure message, history load) — FR-017/FR-018.

### Implementation for User Story 1

- [ ] T010 [US1] `backend/desktop/api.py`: extend `DESKTOP_API_METHODS` + `DesktopApi` ops (`runtime_state`, `load_history(limit)` validated 1..500 default 50, `send_message(request_id, message)` validated UUID + message bounds) delegating to an injected runtime; facade wrappers generated over the fixed allowlist (no generic dispatch) — FR-003/FR-004.
- [ ] T011 [US1] `backend/desktop/app.py`: generalize `LocalBuildBridge` trust wrapper to all allowlisted ops; wire runtime lifecycle in `run_desktop_shell` (open runtime before window; `runtime.close()` in `finally` after `webview.start()` returns; startup storage failure → sanitized message, exit code 2, no window) — FR-006/FR-011/FR-012.
- [ ] T012 [P] [US1] `frontend/src/lib/desktopBridge.js`: add op callers `getRuntimeState`, `loadHistory(limit)`, `sendMessageViaBridge(requestId, message)` (validate payload shapes before returning; keep `checkDesktopHealth` untouched) — FR-003.
- [ ] T013 [P] [US1] `frontend/src/features/chat/services/chatTransport.js`: mode-aware boundary (`fetchHistory`, `sendMessage`, plus desktop-only privacy pass-through), reusing `chatService.js` for web so web behavior is unchanged — FR-017/FR-019.
- [ ] T014 [US1] `frontend/src/features/chat/hooks/useChat.js`: route history+send through injected transport; keep single-flight/token/unmount/timeout invariants; desktop send uses 50s guard timer clearing on resolve — FR-018.
- [ ] T015 [US1] `frontend/src/App.jsx`: desktop branch renders ChatWindow directly; web path untouched — FR-001.
- [ ] T016 [US1] `backend/tests/test_desktop_app_runtime.py`: shell wiring tests (fake webview) — runtime opened before window creation, closed after start returns, close idempotent, no window on storage failure — FR-011/FR-012.

**Checkpoint**: US1 complete end to end (UI → bridge → core → SQLite), tests green.

## Phase 3: User Story 2 — Real local privacy operations (P2)

**Independent Test**: with real data, invoke each privacy op through the desktop bridge and verify via direct SQLite inspection that rows are gone and operations were transactional.

### Tests for User Story 2

- [ ] T017 [P] [US2] `backend/tests/test_desktop_api.py`: privacy ops validation (no args accepted, wrong args rejected sanitized), payload shape `{ok, result}` — FR-013.
- [ ] T018 [US2] `backend/tests/test_desktop_runtime.py`: privacy ops delegation tests against real temp SQLite with seeded rows (delete_history removes chat_logs+turn_requests+outbox in one transaction leaving only aggregate audit row; delete_memories removes memories; resets produce canonical neutral snapshots and bump revision coherently; subsequent commit after reset must respect new revision CAS) — FR-013.

### Implementation for User Story 2

- [ ] T019 [US2] `backend/desktop/api.py` + `runtime.py`: add `delete_history`, `delete_memories`, `reset_emotional_state`, `reset_relationship_state` ops delegating to `LocalStorage` transactional methods with sanitized mapping — FR-013.
- [ ] T020 [P] [US2] `frontend/src/features/privacy/PrivacyPanel.jsx` + `useDesktopPrivacy` hook (desktop-only UI; each destructive op behind a confirmation; never shown in web mode) — FR-013/FR-014.
- [ ] T021 [P] [US2] Frontend test for PrivacyPanel desktop-only rendering + confirmation flow.

**Checkpoint**: privacy story independently functional.

## Phase 4: User Story 3 — The app fails honestly (P3)

**Independent Test**: corrupt SQLite file → explicit sanitized corruption error, file untouched; no provider key → app opens, send errors clearly; window close → clean exit, no sockets/processes.

### Tests for User Story 3

- [ ] T022 [P] [US3] `backend/tests/test_desktop_runtime.py`: corrupt-database test (write garbage file, open runtime → `storage_error`/corrupt code, file bytes unchanged, no new file created); storage-unavailable test (unwritable dir) — FR-011.
- [ ] T023 [P] [US3] `backend/tests/test_desktop_app_runtime.py`: startup failure path (runtime open fails → no window created, exit code 2, sanitized stderr); shutdown path (close called once even if window loop raises) — FR-011/FR-012.
- [ ] T024 [US3] No-supabase proof test: monkeypatch `supabase.create_client` to explode; run a full runtime turn; assert never called (also asserts no `SUPABASE_*` env read on desktop path) — FR-002/SC-002.

### Implementation for User Story 3

- [ ] T025 [US3] `runtime.py`/`app.py`: ensure corrupt/unavailable storage surfaces `storage_error` with actionable constant message at startup and via `runtime_state`; never recreate/reset — FR-011.
- [ ] T026 [US3] Verify clean shutdown integration: `close()` idempotent + store terminal state asserted; no listening sockets (smoke check) — FR-012/SC-006.

**Checkpoint**: all stories complete.

## Phase 5: Smoke, docs, verification

- [ ] T027 [US1] Extend `scripts/desktop_smoke.py`: (a) runtime_state probe; (b) type one message through the real UI (bridge send with temp XDG_DATA_HOME) → assert assistant reply rendered + SQLite rows exist; (c) close window, relaunch against same temp DB → assert history recovered; keep every #334 probe green; assert no listening ports — FR-021/SC-003/SC-005/SC-006.
- [ ] T028 [P] Docs: README desktop section update (no-login runtime, provider key setup, privacy ops, smoke instructions) — PR evidence.
- [ ] T029 Full verification run: backend pytest (all suites incl. #334/#335), frontend lint + test + build, xvfb smoke; record numbers — SC-004/SC-005/SC-007.
- [ ] T030 Open PR `refactor(desktop): move companion runtime off Supabase` with `Closes #336`, full evidence list (problem, architecture, boundaries, Supabase deps removed/retained, compatibility, test numbers, smoke, lifecycle, risks, rollback, out-of-scope) — then stop.

## Dependencies & Execution Order

- Phase 1 blocks everything (runtime core).
- Phase 2 after Phase 1; T010/T011 before T014 wiring is testable end to end; T006–T009 written before/with implementation (TDD).
- Phase 3 after Phase 1 (independent of Phase 2 UI work except bridge facade reuse).
- Phase 4 after Phase 1; T024 can run any time after T002.
- Phase 5 last; T027 needs frontend build + backend complete.

### Notes

- All backend integrated tests use real temporary SQLite files; provider faked only at the port boundary (constitution VI).
- No existing test may be deleted or weakened (SC-004); web suites must stay green (SC-007).
- Commit after each task or logical group.
