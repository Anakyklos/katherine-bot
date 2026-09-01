# Katherine Bot Constitution

## Core Principles

### I. Reliability > Security > Simplicity > Resource Efficiency > Additional Functionality > Implementation Speed

Every decision follows the Anakyklos universal policy in that strict order.
No convenience (implementation speed) may weaken a reliability or security
invariant. Katherine is desktop-first and must run well on modest hardware:
no daemon, no polling, no always-on service, no extra process, no framework
added without proven need.

### II. Core Python is the domain authority

ConversationEngine, personality, emotional state, relationship state and
memory logic live in Python and never move to JavaScript. The frontend never
touches SQLite, SQL, filesystem paths, connection objects, API keys or
internal Python objects. All storage access flows through explicit,
sanitized boundaries.

### III. The desktop bridge is allowlisted, small and fail-closed

The pywebview bridge (`js_api`) exposes an explicit allowlist of methods.
Never turn it into generic RPC, arbitrary Python/filesystem/SQL access or
dynamic dispatch. Every method validates input shape and bounds on the
Python side, returns plain JSON-serializable data with a stable shape, and
converts every failure into a sanitized code+message payload. No
exception, traceback, local path, SQL text, secret, internal prompt
content or provider detail ever crosses to JS. Remote content must never
obtain the privileged bridge (local-build-only trust; fail closed during
any navigation).

### IV. LocalStorage is the single approved persistence foundation

The approved SQLite foundation (`backend/local_storage/`) owns atomic turn
commit, revision CAS, request idempotency/replay, payload validation, real
deletes, neutral resets, migrations, foreign keys, WAL + synchronous=FULL,
recovery, backup, metrics, sanitized errors and terminal lifecycle. No
second SQLite store, ORM or parallel repository may be created without
proven need. SQLite failure is explicit; silent reset, deleting the
original database or creating an empty replacement is forbidden.

### V. Desktop is single-user and login-free

The desktop companion mode has no login screen, no Supabase Auth, no
user/password system of its own, and needs no Supabase env vars, tokens or
service-role keys to start. If an internal contract needs an owner key, it
must be local, deterministic and minimal. Multiuser complexity must not
be transported into the desktop product. The legacy web path stays
untouched until the local path is proven equivalent (incremental
migration; compatibility/import of old installs is preserved; source data
is never deleted automatically).

### VI. Test-First and evidence

Every domain rule change carries tests. Integrated behavior must be proven
with real SQLite (temporary databases), not mocks as the only evidence.
Mocking is only legitimate at the remote LLM provider boundary when
determinism requires it. No test suite may be removed or weakened to get
green; adapt the boundary or keep the legacy test while the behavior
exists. The final HEAD must show: build, lint, and tests passing, plus a
reproducible smoke of the real desktop path.

### VII. Privacy is real, transactional and honest

Delete history really deletes persisted history; delete memories really
deletes local memories; emotional/relationship resets use the canonical
neutral domain states. No "account deletion" semantics exist in the local
product (no fake accounts). Never claim to delete backups or external
services Katherine does not control. Never log personal content, secrets
or internal prompts. Explicit "wipe all local data" is its own future
operation, never hidden inside another button.

### VIII. Remote LLM provider stays decoupled

API keys live only on the Python side and never enter the Vite bundle or
the bridge. The remote provider (Groq) remains usable by the desktop; the
future LanguageModel abstraction (#337) and local LLM are out of scope of
the storage/auth migration. When the provider is unconfigured, the app
still opens local data and reports a clear configuration error only when
an operation actually needs the model.

## Scope Discipline

One issue, one branch, one PR against `main`. No stacked PRs, no parallel
issues, no opportunistic refactors, no visual redesign, no removal of user
data, no resurrected cloud semantics (tombstone/account deletion) in the
local product. After the PR is opened, stop; the maintainer reviews.

## Governance

AGENTS.md defines agent authority and PR format; this constitution binds
all specs/plans/tasks to the architectural invariants above. ADRs under
`docs/adr/` record the approved direction. Amendments require maintainer
review.

**Version**: 1.0.0 | **Ratified**: 2026-09-01 | **Last Amended**: 2026-09-01
