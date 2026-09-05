# Katherine Face Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, pinned, safe bfce-backed Katherine face that presents only validated public emotion state and ephemeral loading state through a pure allowlisted mapper.

**Architecture:** Keep `useChat()` as the only owner of `emotionState` and `isLoading`. `selectKatherineFaceState({ emotionState, isLoading })` converts validated structured state to a small approved expression vocabulary, and `KatherineFace` passes only that result to a hardened local bfce adapter. `AppDesktop` composes the face through a narrow `ChatWindow` render slot; `AppWeb` and the chat transport remain unchanged.

**Tech Stack:** React 18, Vite, Vitest, Testing Library, Node test runner, plain vendored bfce source, CSS custom properties and `prefers-reduced-motion`.

**Spec:** Maintainer-provided issue #341 in the task prompt.

## Global Constraints

- Branch must be `feat/katherine-face-foundation`, based on refreshed `main` at `8192603803fc4747d751a4a8f6fdb93145bfdbd9`.
- The PR title is `feat(frontend): add safe Katherine face foundation` and the body contains `Closes #341`, never #331.
- Only validated public `EmotionStateResponse` fields and ephemeral `isLoading` may reach the mapper or face.
- Do not read emotional core, memory, relationship, prompts, responses, persistence, backend, or Ouroboros/Runstead state from UI code.
- Preserve the 13 canonical emotions and reject duplicates, unknown schema, non-finite values, PAD outside `[-1, 1]`, intensity outside `[0, 1]`, and timestamp `<= 0`.
- The only expression threshold is `STRONG_EXPRESSION_THRESHOLD = 0.70`; values below it are moderate and values at or above it are strong.
- Allowed expressions are `idle`, `thinking`, `happy`, `joy`, `sad`, `annoyed`, `angry`, `worried`, `scared`, `surprised`, `content`, and `curious`; no unsupported bfce expression may leave the mapper.
- `mood_label` never controls the face. Technical errors, timeout, reconnect, inactivity, absence, message count, and time never create emotions.
- The face is decorative with `aria-hidden="true"`, no focus or keyboard capture, no own network requests, and complete listener/timer/RAF cleanup on unmount.
- Vendor only required bfce source from canonical `https://github.com/bwndapp/bbot` at commit `814e199c2045b3be057f59f8dc4ed395a4d2bbd6`, preserving MIT attribution.
- Vendor project-local skills only under `.jcode/skills/impeccable/` and `.jcode/skills/emil-design-eng/`, with exact upstream revisions and licenses recorded.
- Run frontend commands through the existing npm workflow and use the repository `uv` workflow only if a shared backend contract file is changed.

---

### Task 1: Pin project-local design skills and upstream provenance

**Files:**
- Create: `.jcode/skills/impeccable/SKILL.md`, its required `reference/` and `scripts/` auxiliaries, `.jcode/skills/impeccable/LICENSE`, `.jcode/skills/impeccable/UPSTREAM.md`
- Create: `.jcode/skills/emil-design-eng/SKILL.md`, `.jcode/skills/emil-design-eng/LICENSE`, `.jcode/skills/emil-design-eng/UPSTREAM.md`
- Test/evidence: command output from `jcode --cwd "$PWD" --no-update --no-selfdev --provider jcode --tools read run --json ...`

**Interfaces:**
- Produces project-local skills discoverable by JCode from `.jcode/skills/`.
- Impeccable source revision: `8dac6ae7e020c43ab10ce9b41939f6fd42627b96` from `pbakaus/impeccable`, Apache 2.0.
- Emil source revision: `d23d7f88a2e21c9e4b1418c7abe420f5c1052ba7` from `emilkowalski/skills`, MIT.

- [x] **Step 1: Resolve and record the exact upstream revisions.**

```bash
git ls-remote https://github.com/pbakaus/impeccable.git HEAD
git ls-remote https://github.com/emilkowalski/skills.git HEAD
```

- [x] **Step 2: Vendor only the skill directories and licenses.**

```bash
git -C "$IMPECCABLE" archive HEAD .agent/skills/impeccable \
  | tar -x -C .jcode/skills/impeccable --strip-components=3
git -C "$EMIL" archive HEAD skills/emil-design-eng \
  | tar -x -C .jcode/skills/emil-design-eng --strip-components=2
```

Do not copy `.claude`, `.cursor`, `.agents`, caches, builds, or unrelated upstream files.

- [x] **Step 3: Run the JCode recognition probe from the repository workspace.**

```bash
jcode --cwd "$PWD" --no-update --no-selfdev --provider jcode --tools read run --json \
  'Read both .jcode/skills/*/SKILL.md and UPSTREAM.md files and report the two recognized project-local skills as JSON.'
```

Expected evidence names both paths and revisions, with no global installation.

- [ ] **Step 4: Commit the isolated tooling gate.**

```bash
git add .jcode/skills
git commit -m "chore(design): vendor project-local visual skills"
```

---

### Task 2: Harden the public emotion validator with regression tests

**Files:**
- Modify: `frontend/src/shared/utils/formatters.js:49-101`
- Test: create `frontend/tests/formatters.test.js` using Node's built-in test runner, or extend the existing formatter test location if present

**Interfaces:**
- `validateEmotionState(payload)` continues returning a deep-copied validated payload or `null`.
- Public limits are exact: PAD fields `[-1, 1]`, intensity `[0, 1]`, finite positive timestamp, schema `1`, 13 canonical names, no duplicates, maximum three emotions.

- [ ] **Step 1: Write failing boundary tests.**

```js
test('rejects PAD values outside the public range', () => {
  assert.equal(validateEmotionState(withPad({ pleasure: 1.01 })), null);
  assert.equal(validateEmotionState(withPad({ arousal: -1.01 })), null);
  assert.equal(validateEmotionState(withPad({ dominance: 2 })), null);
});

test('rejects intensity outside the public range', () => {
  assert.equal(validateEmotionState(withEmotion({ intensity: -0.01 })), null);
  assert.equal(validateEmotionState(withEmotion({ intensity: 1.01 })), null);
});

test('rejects non-positive timestamps', () => {
  assert.equal(validateEmotionState(withTimestamp(0)), null);
  assert.equal(validateEmotionState(withTimestamp(-1)), null);
});
```

Include existing regression coverage for NaN, infinity, duplicate names, unknown schema, and all 13 canonical names.

- [ ] **Step 2: Run the targeted test and verify it fails for missing range checks.**

```bash
npm run test:node -- tests/formatters.test.js
```

Expected: failures show out-of-range payloads currently being accepted.

- [ ] **Step 3: Implement only the minimum range checks.**

```js
const isInRange = (value, min, max) => value >= min && value <= max;
if (![pleasure, arousal, dominance].every(value => isInRange(value, -1, 1))) return null;
if (!isInRange(item.intensity, 0, 1)) return null;
if (payload.timestamp <= 0) return null;
```

Do not change the DTO shape, schema version, labels, or emotional core.

- [ ] **Step 4: Run the targeted test and then the complete node suite.**

```bash
npm run test:node -- tests/formatters.test.js
npm run test:node
```

Expected: the new tests and all existing node tests pass.

- [ ] **Step 5: Commit the validator change.**

```bash
git add frontend/src/shared/utils/formatters.js frontend/tests/formatters.test.js
git commit -m "fix(frontend): align emotion validation with public ranges"
```

---

### Task 3: Add the deterministic, pure face-state mapper with tests first

**Files:**
- Create: `frontend/src/features/katherine-face/faceState.js`
- Test: `frontend/tests/faceState.test.js` using Node's built-in test runner

**Interfaces:**
- Export `STRONG_EXPRESSION_THRESHOLD` equal to `0.70`.
- Export `selectKatherineFaceState({ emotionState, isLoading })` returning `{ expression, reaction? }`.
- The mapper reads only `emotionState.dominant_emotions[].name/intensity` and `isLoading`; it never reads `mood_label` or PAD to choose expression.
- Dominance is the first item in the validated `dominant_emotions` array. Empty, malformed, or invalid input returns `{ expression: 'idle' }`, except `isLoading === true`, which returns `{ expression: 'thinking' }`.

- [ ] **Step 1: Write failing mapper tests.**

```js
test('maps every canonical emotion through the approved vocabulary', () => {
  const expected = {
    joy: 'happy', sadness: 'sad', anger: 'annoyed', fear: 'worried',
    disgust: 'annoyed', surprise: 'surprised', trust: 'content',
    anticipation: 'curious', tenderness: 'content', guilt: 'worried',
    pride: 'content', jealousy: 'worried', gratitude: 'content',
  };
  for (const [name, expression] of Object.entries(expected)) {
    assert.equal(selectKatherineFaceState({ emotionState: state(name, 0.2), isLoading: false }).expression, expression);
  }
});

test('uses the strong expression only at and above 0.70', () => {
  assert.equal(selectKatherineFaceState({ emotionState: state('joy', 0.699), isLoading: false }).expression, 'happy');
  assert.equal(selectKatherineFaceState({ emotionState: state('joy', 0.70), isLoading: false }).expression, 'joy');
  assert.equal(selectKatherineFaceState({ emotionState: state('joy', 0.701), isLoading: false }).expression, 'joy');
});

test('loading projects thinking without changing the emotion object', () => {
  const emotionState = state('sadness', 0.9);
  const snapshot = structuredClone(emotionState);
  assert.deepEqual(selectKatherineFaceState({ emotionState, isLoading: true }), { expression: 'thinking' });
  assert.deepEqual(emotionState, snapshot);
});
```

Also test deterministic ties, absent/invalid state, `mood_label` changes, forbidden expressions (`love`, `smug`, `sly`, `bored`, `sleepy`, `sleep`), negative technical outcomes, inactivity-like inputs, and no mutation of arguments.

- [ ] **Step 2: Run the targeted test and verify it fails because the module is absent.**

```bash
npm run test:node -- tests/faceState.test.js
```

Expected: module import failure or missing export, not a passing test.

- [ ] **Step 3: Implement the allowlisted table and one threshold.**

```js
export const STRONG_EXPRESSION_THRESHOLD = 0.70;
const EXPRESSION_MAP = Object.freeze({
  joy: ['happy', 'joy'], sadness: ['sad'], anger: ['annoyed', 'angry'],
  fear: ['worried', 'scared'], disgust: ['annoyed'], surprise: ['surprised'],
  trust: ['content'], anticipation: ['curious'], tenderness: ['content', 'happy'],
  guilt: ['worried'], pride: ['content', 'happy'], jealousy: ['worried'],
  gratitude: ['content', 'happy'],
});

export function selectKatherineFaceState({ emotionState, isLoading } = {}) {
  if (isLoading === true) return { expression: 'thinking' };
  const dominant = emotionState?.dominant_emotions?.[0];
  const variants = EXPRESSION_MAP[dominant?.name];
  if (!variants) return { expression: 'idle' };
  const strong = dominant.intensity >= STRONG_EXPRESSION_THRESHOLD;
  return { expression: strong && variants[1] ? variants[1] : variants[0] };
}
```

The actual implementation must guard types without assuming unvalidated input, preserve no references, and export only approved expression values.

- [ ] **Step 4: Run targeted and complete node tests.**

```bash
npm run test:node -- tests/faceState.test.js
npm run test:node
```

Expected: all mapper cases pass and existing node tests remain green.

- [ ] **Step 5: Commit the mapper.**

```bash
git add frontend/src/features/katherine-face/faceState.js frontend/tests/faceState.test.js
git commit -m "feat(frontend): add allowlisted Katherine face mapper"
```

---

### Task 4: Vendor the approved bfce source and harden its lifecycle boundary

**Files:**
- Create under `frontend/src/vendor/bfce/`: `Face.jsx`, `core.js`, `expressions.js`, `face.css`, `index.js`, `LICENSE`, `UPSTREAM.md`
- Create: `frontend/src/features/katherine-face/KatherineFace.jsx`
- Create: `frontend/src/features/katherine-face/KatherineFace.css`
- Test: `frontend/tests/KatherineFace.test.jsx`

**Interfaces:**
- `KatherineFace` accepts only `{ emotionState, isLoading, className }` and passes the mapper result to the local vendor. It does not accept messages, response, prompt, token, user ID, memory, relationship, transport, API, or bridge props.
- The vendor receives only `expression`, fixed safe render options, and a local class/style. Use `mouth={true}`, `pupils={false}`, `track={false}`, `blink={false}`, and `idle={false}` so this face has no pointer tracking or idle randomness.
- The vendor component is decorative and must render with `aria-hidden="true"`.
- The local vendor adaptation must retain the approved commit in `UPSTREAM.md` and document any lifecycle-only hardening. Do not expose unsupported expressions through the adapter.

- [ ] **Step 1: Write failing component and lifecycle tests.**

```jsx
it('renders only the mapper expression and hides the decorative face', () => {
  render(<KatherineFace emotionState={state('joy', 0.8)} isLoading={false} />);
  expect(screen.getByTestId('katherine-face')).toHaveAttribute('aria-hidden', 'true');
  expect(screen.getByTestId('bfce-face')).toHaveAttribute('data-expression', 'joy');
});

it('does not pass chat or identity props to the vendor', () => {
  render(<KatherineFace emotionState={state('trust', 0.2)} isLoading={false} />);
  const vendor = screen.getByTestId('bfce-face');
  expect(vendor).not.toHaveAttribute('data-message');
  expect(vendor).not.toHaveAttribute('data-token');
  expect(vendor).not.toHaveAttribute('data-user-id');
  expect(vendor).not.toHaveAttribute('data-memory');
  expect(vendor).not.toHaveAttribute('data-prompt');
});
```

Use real vendor behavior where possible. Spy only on browser lifecycle APIs (`addEventListener`, `removeEventListener`, `requestAnimationFrame`, `cancelAnimationFrame`, `setTimeout`, `clearTimeout`, `fetch`, `XMLHttpRequest`) to verify boundaries. Avoid mocks that merely assert calls to test code.

- [ ] **Step 2: Run the targeted component test and verify the missing component/vendor failure.**

```bash
npm run test:component -- tests/KatherineFace.test.jsx
```

Expected: import or render failure because the component is not implemented.

- [ ] **Step 3: Copy only the approved bfce source and MIT license.**

```bash
git -C "$BFCE" archive 814e199c2045b3be057f59f8dc4ed395a4d2bbd6 \
  src/Face.jsx src/core.js src/expressions.js src/face.css src/index.js LICENSE \
  | tar -x -C frontend/src/vendor/bfce --strip-components=1
```

Record canonical resolution from `https://github.com/bwndapp/bfce` only if the exact API URL redirect is represented as `https://github.com/bwndapp/bbot`; do not fetch latest and do not use CDN or runtime network.

- [ ] **Step 4: Apply only lifecycle-safe local changes required by the acceptance contract.**

The adapter must keep all cleanup handles local to a face instance: remove scroll/resize/pointer listeners, disconnect `IntersectionObserver`, remove the SVG on destroy, clear timers, and cancel the shared RAF when the last face is destroyed. Reduced motion must skip blinking/idle motion and any CSS transition must have a `@media (prefers-reduced-motion: reduce)` alternative. If the upstream implementation cannot guarantee cleanup, isolate the change in the vendored core and describe it in `UPSTREAM.md`.

- [ ] **Step 5: Implement the reusable `KatherineFace` wrapper.**

```jsx
export default function KatherineFace({ emotionState, isLoading, className = '' }) {
  const { expression } = selectKatherineFaceState({ emotionState, isLoading });
  return (
    <div className={`katherine-face ${className}`.trim()} data-testid="katherine-face" aria-hidden="true">
      <Face expression={expression} mouth track={false} blink={false} idle={false} />
    </div>
  );
}
```

Do not add a second hook, fetch, timer, or domain-state copy. Use CSS for the small integrated frame, not a new card/dashboard.

- [ ] **Step 6: Run the component tests and commit.**

```bash
npm run test:component -- tests/KatherineFace.test.jsx
npm run test:component

git add frontend/src/vendor/bfce frontend/src/features/katherine-face frontend/tests/KatherineFace.test.jsx
git commit -m "feat(frontend): add safe bfce Katherine face component"
```

---

### Task 5: Integrate the real desktop emotion state without touching web mode

**Files:**
- Modify: `frontend/src/AppDesktop.jsx`
- Modify: `frontend/src/features/chat/components/ChatWindow.jsx` only if a minimal slot/prop is required
- Test: update `frontend/tests/App.desktop.test.jsx` or add `frontend/tests/KatherineFace.integration.test.jsx`
- Test: `frontend/tests/desktopGraph.test.js` if the new import graph needs an explicit allowlist assertion

**Interfaces:**
- Exactly one `useChat()` remains in the current conversation.
- `AppDesktop` receives the real face data through a narrow render slot or composition boundary, while `AppWeb` remains behaviorally identical and does not import the face unless the shared component requires it.
- PrivacyPanel remains present.

- [ ] **Step 1: Add failing desktop integration assertions.**

```jsx
it('shows the decorative face from the same desktop conversation state', async () => {
  render(<AppDesktop />);
  expect(screen.getByTestId('katherine-face')).toBeInTheDocument();
  expect(screen.getByPlaceholderText(/escreva aqui sua mensagem/i)).toBeInTheDocument();
  expect(screen.getByText(/privacidade/i)).toBeInTheDocument();
});

it('keeps web unauthenticated mode unchanged', async () => {
  render(<AppWeb />);
  await waitFor(() => expect(mockGetSession).toHaveBeenCalled());
  expect(screen.queryByTestId('katherine-face')).toBeNull();
});
```

- [ ] **Step 2: Run the targeted integration test and observe the missing face or wiring failure.**

```bash
npm run test:component -- tests/App.desktop.test.jsx
```

- [ ] **Step 3: Wire the face through the existing `ChatWindow` state owner.**

Use a `showFace`/`faceSlot` desktop-only prop if needed, but do not call `useChat()` from `AppDesktop` and do not create a parallel state. The face must receive the same `emotionState` and `isLoading` variables already returned by the one `useChat()` call.

- [ ] **Step 4: Add minimal responsive CSS.**

Place the face as a quiet desktop-only companion region that inherits the existing dark palette, has no extra chrome, stays hidden or compact on narrow widths, and leaves the chat and privacy surfaces intact. Include:

```css
.katherine-face {
  flex: 0 0 auto;
  width: clamp(7rem, 14vw, 10rem);
  aspect-ratio: 1;
}

@media (prefers-reduced-motion: reduce) {
  .katherine-face,
  .katherine-face * {
    animation: none !important;
    transition-duration: 0ms !important;
  }
}
```

- [ ] **Step 5: Run desktop integration, graph, and complete component tests.**

```bash
npm run test:component -- tests/App.desktop.test.jsx tests/desktopGraph.test.js
npm run test:component
```

- [ ] **Step 6: Commit the desktop-only integration.**

```bash
git add frontend/src/AppDesktop.jsx frontend/src/features/chat/components/ChatWindow.jsx frontend/src/index.css frontend/tests/App.desktop.test.jsx frontend/tests/desktopGraph.test.js
git commit -m "feat(frontend): integrate Katherine face into desktop shell"
```

---

### Task 6: Perform bounded visual review and complete verification

**Files:**
- Modify only implementation/test/provenance files if a verification finding requires a scoped correction.
- Create or update PR evidence documentation only if needed by repository conventions.

**Interfaces:**
- Final evidence must cover every explicit #341 acceptance item and include exact upstream provenance, JCode recognition, bfce canonical resolution, lifecycle cleanup, reduced motion, no network, build graph, and test commands.

- [ ] **Step 1: Run the local Impeccable context once and its one bounded detector pass.**

```bash
.jcode/skills/impeccable/scripts/impeccable context
.jcode/skills/impeccable/scripts/impeccable detect --json \
  frontend/src/AppDesktop.jsx \
  frontend/src/features/katherine-face/KatherineFace.jsx \
  frontend/src/features/katherine-face/KatherineFace.css \
  frontend/src/index.css
```

Review the findings with the vendored Impeccable and Emil guidance. Fix only issues inside the #341 face foundation. Use a Before/After/Why markdown table in the review notes if documenting UI changes.

- [ ] **Step 2: Run all mandatory frontend verification commands.**

```bash
cd frontend
npm test
npm run lint
npm run build
```

- [ ] **Step 3: Run relevant CI gates and backend tests only if a shared contract file was changed.**

```bash
uv lock --check
uv run --project backend python -m pytest backend/tests/test_emotion_presentation.py backend/tests/test_emotional_integration.py
```

Do not reintroduce `pip install -r requirements.txt` or alter CI.

- [ ] **Step 4: Inspect the final diff and requirement traceability.**

```bash
git status --short
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Confirm no secrets, prompts, chat content, identity data, global skill copies, CDN URLs, runtime fetches, unsupported expressions, or out-of-scope issue work are present.

- [ ] **Step 5: Commit only verified fixes, push the single required branch, and open one PR.**

```bash
git push -u origin feat/katherine-face-foundation
```

Use title `feat(frontend): add safe Katherine face foundation`. The body must include `Closes #341`, base SHA, all provenance and license data, mapper table and threshold, lifecycle/reduced-motion evidence, test/build results, network/resource notes, risks/migration/rollback, and explicit out-of-scope items. After the PR is opened, stop and do not start #343, #342, or another issue.
