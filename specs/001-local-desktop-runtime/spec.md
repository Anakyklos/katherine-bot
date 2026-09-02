# Feature Specification: Local Desktop Companion Runtime (no Supabase, no login)

**Feature Branch**: `refactor/local-desktop-runtime`

**Created**: 2026-09-01

**Status**: Approved for planning (issue #336; tracking #333)

**Input**: User description: "P1: migrar companion mode para runtime desktop sem Supabase ou login — transformar o shell Linux pywebview (#334) e a persistência SQLite local (#335) no runtime real do modo independente da Katherine: iniciar como app desktop Linux, abrir direto no companion mode, carregar estado/histórico, conversar e persistir novos turnos sem login, Supabase Auth, Supabase Database/PostgREST, PostgreSQL remoto ou servidor HTTP interno; apenas o provider LLM remoto configurado pode exigir rede."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Open Katherine and talk, with no login and no cloud (Priority: P1)

A person launches the Katherine desktop application on Linux. There is no
login screen: the companion conversation interface opens directly. The
person types a message and receives Katherine's reply. The message and the
reply are persisted on the user's own disk. No account, email, password or
remote database participates in this flow.

**Why this priority**: This is the product change itself — the desktop
companion experience being real, local and immediate. Everything else
exists to make this story safe and durable.

**Independent Test**: Launch the desktop app with no Supabase environment
variables set; confirm the chat UI opens directly (no auth page); send one
message through the real UI; confirm the reply comes from the core; confirm
the message pair is persisted in the local SQLite database; close and
relaunch and confirm the conversation is recovered.

**Acceptance Scenarios**:

1. **Given** a Linux machine with the app built and no Supabase credentials
   anywhere, **When** the user launches the desktop app, **Then** the
   companion mode opens directly and no login screen is ever shown.
2. **Given** the app is open, **When** the user sends a first message into
   an empty local database, **Then** Katherine generates a reply via the
   configured remote LLM provider and the turn is committed atomically to
   local storage.
3. **Given** a first turn has been committed, **When** the app is fully
   closed and reopened, **Then** the history and emotional/relationship
   state are recovered from local storage and the UI shows the previous
   conversation.
4. **Given** the app is running, **When** the user watches the process
   during startup and conversation, **Then** no request to any Supabase
   or PostgREST endpoint is made and no HTTP server is needed for
   UI/core communication.

---

### User Story 2 - Local privacy operations stay real (Priority: P2)

The user wants their local data respected: clearing the persisted history
really deletes the persisted history; clearing memories really deletes the
local memories; emotional and relationship resets return Katherine to the
canonical neutral states. In the local product there is no "account", so no
fake account-deletion semantics are invented.

**Why this priority**: Privacy invariants must not silently degrade when the
runtime changes; the user's control over their own local data is a
non-negotiable property of the product.

**Independent Test**: With real local data committed, invoke each local
privacy operation through the desktop path and verify with direct SQLite
inspection (temporary database) that the corresponding persisted rows are
gone and the operation was transactional.

**Acceptance Scenarios**:

1. **Given** a local database with persisted turns, **When** the user
   deletes the history, **Then** the persisted history (messages, turn
   ledger, derived events) is really removed in one transaction and the
   aggregate-only audit record remains.
2. **Given** local memories exist, **When** the user deletes memories,
   **Then** the local memories are really removed.
3. **Given** non-neutral emotional and relationship states, **When** the
   user resets them, **Then** both snapshots become exactly the canonical
   neutral domain states and the profile revision is bumped coherently.

---

### User Story 3 - The app fails honestly (Priority: P3)

When something is wrong — the local database is corrupt, the storage is
unavailable, or the LLM provider is not configured — the application
reports an explicit, sanitized error. It never silently resets the
database, never creates a replacement database, and still lets the user
open their local data when the model is not configured (the error appears
only when an operation actually needs the model).

**Why this priority**: Reliability means failures are visible and
non-destructive; silent recovery would destroy user trust and user data.

**Independent Test**: Point the runtime at a corrupt SQLite file and
confirm an explicit corruption error surfaces; run with no provider key
and confirm data can be opened and the configuration error appears only
at send time.

**Acceptance Scenarios**:

1. **Given** a corrupt local database, **When** the app starts or loads
   history, **Then** an explicit sanitized corruption error is reported
   and the original file is untouched.
2. **Given** no LLM provider configured, **When** the app opens, **Then**
   local history and state load normally, and sending a message surfaces a
   clear provider-configuration error.
3. **Given** the window is closed, **When** the process exits, **Then**
   the local storage and all desktop-owned resources are closed cleanly.

### Edge Cases

- The first turn ever committed into a brand-new (empty) local database
  must work, including creating the initial profile row.
- Two rapid sends of the same request id (double-click / retry) must not
  duplicate the turn: idempotent replay returns the committed result, and
  a divergent payload for the same id is rejected as a conflict.
- A turn whose generation succeeded but whose commit hits a revision
  mismatch must retry in a bounded way (single retry), and never leave
  messages and snapshots divergent.
- An interrupted process (crash mid-turn) must leave no half-committed
  turn; on restart pending work is failed-closed and marked interrupted,
  never silently recomputed.
- History loading must tolerate an empty database and return an empty list
  (not an error).
- The bridge must reject every malformed input (wrong types, oversized
  strings, extra arguments) with a sanitized structured error; remote
  content must never gain bridge access (invariant from #334).
- Web mode must keep working exactly as before during the migration (no
  regression in the legacy path while it exists).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The desktop application MUST start with no Supabase
  environment variables, tokens or service credentials and open directly
  into the companion conversation interface without any login screen.
- **FR-002**: In desktop mode the application MUST NOT perform any
  Supabase Auth, Supabase Database, PostgREST or remote PostgreSQL
  operation for startup, state loading, history loading, turn commit or
  local privacy operations.
- **FR-003**: The desktop UI MUST communicate with the Python core
  exclusively through the allowlisted local bridge; no HTTP server may be
  required for UI/core communication in desktop mode.
- **FR-004**: The bridge surface MUST remain an explicit allowlist. New
  methods are limited to what this feature needs: a runtime/state
  initialization query, history loading, sending one turn, and the local
  privacy operations. Every method MUST validate input shape and bounds
  in Python and return plain JSON-serializable data with a stable shape.
- **FR-005**: Every bridge failure MUST surface to the UI as a sanitized
  code+message payload; no exception, traceback, local path, SQL, secret,
  internal prompt content or provider detail may cross the bridge.
- **FR-006**: The bridge MUST stay local-build-only: content that is not
  the local build page must never be served (fail closed during any
  navigation state).
- **FR-007**: The desktop runtime MUST use the approved local storage
  foundation as its authoritative persistence: no second SQLite store, ORM
  or parallel repository.
- **FR-008**: Each conversation turn MUST be committed atomically
  (messages, snapshots, ledger, events in one transaction with revision
  CAS), including the first turn in an empty database.
- **FR-009**: Request ids MUST keep their idempotency semantics locally:
  same id + same payload replays the committed result; same id with a
  divergent payload is a rejected conflict; nothing is written on the
  rejection paths.
- **FR-010**: A complete application restart MUST recover history and the
  persisted emotional/relationship state from local storage.
- **FR-011**: Local storage failure or corruption MUST produce an
  explicit, sanitized error; the runtime MUST NOT create a new database,
  delete or reset the existing one, or silently discard state.
- **FR-012**: Closing the window MUST terminate the local storage and
  every desktop-owned resource cleanly (no daemon, no worker, no
  polling, no auxiliary process).
- **FR-013**: Local privacy operations (delete history, delete memories,
  reset emotional state, reset relationship state) MUST be reachable from
  the desktop UI through explicit bridge operations and MUST preserve
  their transactional, real-deletion semantics.
- **FR-014**: The runtime MUST NOT introduce "account deletion" or
  tombstone semantics into the local product; no fake account exists.
  A future explicit "wipe all local data" operation is documented as a
  follow-up, not hidden inside another operation.
- **FR-015**: API keys and provider configuration MUST remain only on the
  Python side; the frontend bundle and the bridge MUST NOT carry secrets.
- **FR-016**: When the LLM provider is not configured, the application
  MUST still open and load local data, and MUST present a clear
  configuration error only when an operation actually requires the model.
- **FR-017**: The desktop frontend MUST detect the desktop environment
  through an explicit, testable mechanism and MUST NOT depend on
  `supabase.auth` or the auth page in that mode; the chat and history
  flows MUST use the local bridge instead of HTTP calls.
- **FR-018**: Existing frontend conversation guarantees MUST be preserved
  in desktop mode: single-flight send, loading and failure states,
  timeout/cancel where semantically possible, unmount protection, and
  idempotent reconciliation.
- **FR-019**: The legacy web mode (Supabase auth + HTTP API) MUST continue
  to function without undue regression while it exists; transport
  differences MUST be isolated in a small, explicit boundary rather than
  `if (desktop)` scattered across the frontend.
- **FR-020**: No cloud-heavy module may be imported or initialized in the
  desktop local path when it is not used; external clients must be
  lazy/on-demand.
- **FR-021**: The final state MUST provide a reproducible smoke test that
  exercises the real flow (UI -> bridge -> core -> local storage) plus a
  full restart, using real SQLite and, when determinism requires it, a
  fake only at the remote provider boundary.

### Key Entities *(include if feature involves data)*

- **Local installation**: the single-user desktop installation owning one
  local database. Identity in the local domain is deterministic and
  minimal; no email, Supabase UUID or token is required.
- **Turn**: one user message + Katherine's reply, persisted atomically
  with the new emotional/relationship snapshots, a replay payload and a
  request id (idempotency key).
- **Local profile**: the single local profile row carrying the persona,
  user profile, emotional and relationship snapshots plus the revision
  (CAS token).
- **Local history**: the persisted sequence of messages, loadable in
  bounded windows (most recent first, chronological presentation).
- **Bridge operation**: one allowlisted, validated, sanitized method on
  the desktop bridge (the only transport between UI and core).
- **Remote LLM provider**: the user-configured remote model provider; the
  only component allowed to require network; keys stay in Python.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: The desktop application starts and reaches the companion
  conversation interface with zero Supabase credentials present, in a
  fresh environment, with no login step.
- **SC-002**: In desktop mode, zero Supabase/PostgREST requests occur
  from process start through load/history/send/privacy flows (verifiable
  by test-level request interception and by no such client being
  constructed).
- **SC-003**: A first turn in an empty database is generated and
  persisted; a complete restart recovers the history and snapshots
  (proven by an integrated test with a real temporary SQLite database,
  and by the smoke test).
- **SC-004**: All pre-existing test suites of #334 (desktop shell/bridge)
  and #335 (local storage) remain green on the final HEAD, with no suite
  removed or weakened.
- **SC-005**: Backend, frontend build, frontend lint and frontend tests
  all pass on the final HEAD; the reproducible desktop smoke passes end
  to end (UI -> bridge -> core -> SQLite -> restart).
- **SC-006**: Closing the window leaves no listening socket, no extra
  process, and the store reaches its terminal closed state.
- **SC-007**: The legacy web flow still passes its existing tests with no
  undue regression.

## Assumptions

- The remote LLM provider remains Groq with the existing key-loading
  mechanism (`groq_keys`), read only by Python; desktop does not add any
  new provider abstraction (#337 is out of scope).
- The desktop uses the existing Vite build loaded via `file://` through
  pywebview (from #334); packaging (.deb), Windows/macOS, TTS, visual
  redesign and face/avatar are out of scope.
- Local conversation memory retrieval (vector/embedding search) is part of
  the legacy web path; for this migration the desktop context loader
  provides recent local history and profile/persona state through the
  same trusted-context boundary, without Supabase RPCs. Memory write
  through archival extraction stays disabled by default in the desktop
  runtime (as it is in the web default), so no silent behavior change is
  introduced.
- The legacy web architecture is preserved during this migration; global
  removal/reconciliation of cloud/Auth backlog belongs to the maintainer
  (#340). This feature only removes cloud dependencies from the *desktop
  runtime path*.
- Data migration/import from existing Supabase installs is a separate,
  explicit operation (never an automatic sync). Per issue #336's
  mandatory test 12 ("importação de fixture legado não duplica dados"),
  this feature SHIPS the idempotent legacy fixture import:
  `backend/local_storage/legacy_import.py` imports a validated
  structural Supabase export into the local SQLite store in ONE
  transaction — re-importing the same fixture never duplicates rows
  (stable legacy request ids skip as duplicates), invalid/forbidden
  fixtures fail closed with no partial writes, the source is never
  modified, and the desktop runtime reads the imported history and
  replays imported turns without recomputation
  (`backend/tests/test_legacy_import.py`). The full UI-facing
  export/import UX (file picker, progress) remains a maintainer
  decision (#340); the storage-level contract and its proofs are in
  scope and delivered here.
