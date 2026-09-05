# Katherine Companion Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recompose the Linux desktop companion so KatherineFace is the visual center while the existing conversation, privacy operations, state ownership, and web mode remain intact.

**Architecture:** Keep `ChatWindow` as the only chat controller and add an optional `renderLayout(chatModel)` seam. Add a presentational `CompanionLayout` that receives that model and composes the existing header, face, history, composer, and collapsed secondary utilities. `AppDesktop` selects this layout while `AppWeb` uses the unchanged default layout.

**Tech Stack:** React 18, Vite, Tailwind utilities already in the repository, local CSS, Vitest, Testing Library, browser validation through the desktop entry.

**Spec:** `docs/superpowers/specs/2026-09-05-katherine-companion-layout-design.md`

## Global Constraints

- Work only on issue #343 in `feat/katherine-companion-layout`, based on the latest `origin/main`.
- Preserve the existing `KatherineFace` presentation boundary and do not edit face mapper, bfce, emotional domain, backend, bridge, storage, or persistence.
- Keep exactly one `useChat()` call per mounted conversation and do not add network requests, stores, polling, timers, workers, or dependencies.
- `AppWeb` must retain the default `ChatWindow` composition and existing authentication behavior.
- Keep the existing message, loading, error, clear-screen, privacy confirmation, keyboard, and reduced-motion contracts.
- Do not create fake future sidebar content, placeholder metrics, settings, floating mode, or mobile redesign.

---

### Task 1: Add the layout seam and a presentational companion composition

**Files:**
- Create: `frontend/src/features/chat/components/CompanionLayout.jsx`
- Create: `frontend/src/features/chat/components/CompanionLayout.css`
- Modify: `frontend/src/features/chat/components/ChatWindow.jsx`
- Modify: `frontend/src/AppDesktop.jsx`

**Interfaces:**
- `ChatWindow` consumes an optional `renderLayout` prop with signature `(chatModel) => ReactNode`, calls `useChat()` exactly once, and otherwise renders its current default layout.
- `CompanionLayout` consumes the existing `useChat()` return shape as props: `messages`, `input`, `setInput`, `isLoading`, `emotionState`, `messagesEndRef`, `inputRef`, `handleSend`, `clearScreen`, `transport`, plus optional `auxiliarySlot`.
- `CompanionLayout` produces a labeled desktop companion DOM with `data-testid="companion-layout"`, `data-testid="companion-presence"`, `data-testid="companion-history"`, and `data-testid="companion-utilities"`. It renders `KatherineFace` with only `emotionState` and `isLoading`, keeps `MessageList` and `ChatInput` in the conversation rail, and renders the optional auxiliary slot only when provided.
- `AppDesktop` produces `<ChatWindow renderLayout={renderCompanionLayout} />`; `AppWeb` continues to render `<ChatWindow />` without the prop.

- [ ] **Step 1: Add the failing composition tests**

Create `frontend/tests/CompanionLayout.test.jsx` with a local `makeChatModel()` fixture that contains only public chat-model values and a desktop transport. Mock `KatherineFace` to expose its received `emotionState` and `isLoading`, while using the real `MessageList`, `ChatInput`, `ChatHeader`, `EmotionPanel`, and `PrivacyPanel` where practical. Cover:

```jsx
it('keeps the face in the dominant presence region and the composer in the conversation rail', () => {
    render(<CompanionLayout {...makeChatModel()} />);

    const presence = screen.getByTestId('companion-presence');
    const history = screen.getByTestId('companion-history');
    const composer = screen.getByRole('textbox', { name: /sua mensagem/i });

    expect(within(presence).getByTestId('katherine-face')).toBeInTheDocument();
    expect(within(history).getByText(/comece uma conversa/i)).toBeInTheDocument();
    expect(composer).toBeInTheDocument();
    expect(presence).toContainElement(screen.getByTestId('katherine-face'));
    expect(screen.getByTestId('companion-layout')).not.toContainElement(
        screen.getByTestId('katherine-face').closest('header'),
    );
});
```

Also add tests for an empty emotion state, a valid emotion state, loading status, an existing system error message, history messages, collapsed privacy/emotion utilities, and an auxiliary slot that renders only when explicitly supplied. Use `aria-label` and DOM placement assertions rather than implementation-only class snapshots.

- [ ] **Step 2: Run the new test file and verify the expected red failure**

Run: `cd frontend && npx vitest run --config vitest.config.mjs tests/CompanionLayout.test.jsx`

Expected: FAIL because `CompanionLayout` and the companion renderer seam do not exist yet.

- [ ] **Step 3: Add the minimal `renderLayout` seam**

In `ChatWindow`, retain the existing `const chatModel = useChat()` call and add:

```jsx
const ChatWindow = ({ faceSlot = null, renderLayout = null }) => {
    const chatModel = useChat();
    if (renderLayout) return renderLayout(chatModel);
    const {
        messages,
        input,
        setInput,
        isLoading,
        emotionState,
        messagesEndRef,
        inputRef,
        handleSend,
        clearScreen,
        transport,
    } = chatModel;
    // existing default JSX remains unchanged
};
```

Do not change the default JSX beyond using the destructured values.

- [ ] **Step 4: Implement `CompanionLayout` with the approved composition**

Compose the model without calling any hook:

```jsx
export default function CompanionLayout({
    messages,
    input,
    setInput,
    isLoading,
    emotionState,
    messagesEndRef,
    inputRef,
    handleSend,
    clearScreen,
    transport,
    auxiliarySlot = null,
}) {
    return (
        <div className="companion-layout" data-testid="companion-layout">
            <ChatHeader clearScreen={clearScreen} />
            <main className="companion-layout__body" aria-label="Companion Katherine">
                <section className="companion-layout__presence" aria-label="Presença da Katherine" data-testid="companion-presence">
                    <KatherineFace emotionState={emotionState} isLoading={isLoading} className="katherine-face--companion" />
                </section>
                <section className="companion-layout__conversation" aria-label="Conversa com Katherine">
                    <div className="companion-layout__history" data-testid="companion-history" aria-label="Histórico da conversa">
                        <MessageList messages={messages} isLoading={isLoading} messagesEndRef={messagesEndRef} />
                    </div>
                    <ChatInput input={input} setInput={setInput} handleSend={handleSend} isLoading={isLoading} inputRef={inputRef} />
                    <div className="companion-layout__utilities" data-testid="companion-utilities">
                        {emotionState && <details>...</details>}
                        <details>...</details>
                    </div>
                </section>
            </main>
            {auxiliarySlot ? <aside className="companion-layout__auxiliary" aria-label="Área auxiliar">{auxiliarySlot}</aside> : null}
        </div>
    );
}
```

The disclosures must use native `<details>` and `<summary>` labels. The privacy disclosure always exists for desktop transport and passes the original `transport`; it must not alter operation copy or confirmation behavior. The emotion disclosure is present only when `emotionState` exists. No future placeholder text is rendered.

- [ ] **Step 5: Implement local CSS for hierarchy and responsive behavior**

In `CompanionLayout.css`, import no new assets or dependencies. Establish:

```css
.companion-layout { height: 100vh; min-height: 0; display: flex; flex-direction: column; background: #0b0f14; color: #f3f4f6; }
.companion-layout__body { flex: 1 1 auto; min-height: 0; display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(20rem, 31rem); }
.companion-layout__presence { min-width: 0; min-height: 0; display: grid; place-items: center; padding: clamp(2rem, 5vw, 6rem); border-right: 1px solid rgba(148, 163, 184, 0.14); }
.companion-layout__conversation { min-width: 0; min-height: 0; display: flex; flex-direction: column; background: rgba(17, 24, 39, 0.46); }
.companion-layout__history { min-height: 0; flex: 1 1 auto; display: flex; flex-direction: column; }
.companion-layout__utilities { flex: 0 0 auto; display: grid; gap: 0.5rem; padding: 0 1rem 1rem; }
```

Use `.katherine-face--companion` to give the face a dominant `width: clamp(12rem, 27vw, 24rem)` without adding a card or gradient. Add a medium-width rule that reduces pane padding and face size, and a narrow fallback below `48rem` that stacks presence over conversation while preserving `min-height: 0`, scroll behavior, and composer access. Add `prefers-reduced-motion: reduce` rules that disable incidental transitions only.

- [ ] **Step 6: Wire the desktop root without changing the web root**

In `AppDesktop.jsx`, import `CompanionLayout` and pass a module-level renderer:

```jsx
const renderCompanionLayout = (chatModel) => <CompanionLayout {...chatModel} />;

export default function AppDesktop() {
    return (
        <div className="min-h-screen bg-gray-900 text-gray-100 font-sans antialiased">
            <ChatWindow renderLayout={renderCompanionLayout} />
        </div>
    );
}
```

Keep `AppWeb.jsx` unchanged. Run the focused tests from Step 2 and the existing App desktop/web tests.

- [ ] **Step 7: Commit the self-contained composition change**

```bash
git add docs/superpowers/specs/2026-09-05-katherine-companion-layout-design.md docs/superpowers/plans/2026-09-05-katherine-companion-layout.md frontend/src/AppDesktop.jsx frontend/src/features/chat/components/ChatWindow.jsx frontend/src/features/chat/components/CompanionLayout.jsx frontend/src/features/chat/components/CompanionLayout.css frontend/tests/CompanionLayout.test.jsx
git commit -m "feat(frontend): center companion mode on Katherine presence"
```

---

### Task 2: Verify behavior, accessibility, and preserved boundaries

**Files:**
- Modify: `frontend/tests/App.desktop.test.jsx`
- Modify: `frontend/tests/chatWindowPrivacy.test.jsx` only if the existing desktop privacy assertion needs a selector update
- Modify: `frontend/tests/CompanionLayout.test.jsx`

**Interfaces:**
- Desktop root continues to render one ChatWindow/useChat model and the new companion layout.
- Web root continues to render the existing auth gate and default ChatWindow without KatherineFace.
- Existing transport and privacy operations remain the same objects and calls.

- [ ] **Step 1: Add failing desktop integration assertions**

Extend `App.desktop.test.jsx` to assert the real desktop root contains `companion-layout`, `companion-presence`, `companion-history`, the labeled composer, and the collapsed `Privacidade local` disclosure. Assert the mocked face receives `data-has-emotion="false"` and `data-loading="false"` for the empty initial model. Add a valid emotion-state case through the existing controlled transport or model seam and assert the face receives that state without a second hook or second history request.

Add assertions that `AppWeb` remains on the auth branch, never renders `companion-layout` or `katherine-face`, and does not alter its existing session behavior.

- [ ] **Step 2: Run the focused integration tests and verify red before implementation adjustments**

Run: `cd frontend && npx vitest run --config vitest.config.mjs tests/CompanionLayout.test.jsx tests/App.desktop.test.jsx tests/chatWindowPrivacy.test.jsx`

Expected: the new assertions fail only where the test seam or selectors are not yet complete. Fix production behavior rather than weakening existing assertions.

- [ ] **Step 3: Cover real states and keyboard behavior**

Use the existing `MessageList` and `ChatInput` contracts to verify:

```jsx
expect(screen.getByRole('status')).toHaveTextContent(/katherine está digitando/i);
expect(screen.getByText(/erro ao falar com a katherine/i)).toBeInTheDocument();
expect(screen.getByText('old message')).toBeInTheDocument();
expect(screen.getByRole('textbox', { name: /sua mensagem/i })).toBeInTheDocument();
```

Open the native privacy disclosure with `fireEvent.click(screen.getByText('Privacidade local'))`, verify all four existing operation buttons remain reachable, and confirm that no operation runs until its existing confirmation button is activated. Verify `details` and the composer are reachable in keyboard order without adding focus to the decorative face.

- [ ] **Step 4: Run all frontend component and Node tests**

Run: `cd frontend && npm test`

Expected: exit code 0 with all existing and new suites passing.

- [ ] **Step 5: Commit test-only adjustments**

```bash
git add frontend/tests/App.desktop.test.jsx frontend/tests/CompanionLayout.test.jsx frontend/tests/chatWindowPrivacy.test.jsx
git commit -m "test(frontend): cover companion layout boundaries"
```

---

### Task 3: Run real desktop visual and technical acceptance

**Files:**
- No intended source changes. If a material visual defect is found, modify only the companion layout files and add a focused regression before rerunning this task.

**Interfaces:**
- The production desktop entry is `frontend/desktop.html`.
- The web entry is `frontend/index.html`.

- [ ] **Step 1: Run Impeccable's one-shot detector on changed UI targets**

Run once after implementation:

```bash
./.jcode/skills/impeccable/scripts/impeccable detect --json frontend/src/features/chat/components/CompanionLayout.jsx frontend/src/features/chat/components/CompanionLayout.css frontend/src/AppDesktop.jsx
```

Read the detector output and fix only material findings within #343. Do not rerun it in a loop.

- [ ] **Step 2: Build and serve the real desktop entry**

Run: `cd frontend && npm run build && npm run dev -- --host 127.0.0.1`

Open `http://127.0.0.1:3000/desktop.html` through the browser tool. Do not use a mock-only page for the visual acceptance path.

- [ ] **Step 3: Capture wide, medium, and minimum desktop observations**

At viewport sizes 1440x900, 1024x768, and 800x600, observe and record:

- the face occupies the largest visual area and is not inside the header;
- the history scrolls independently and remains readable;
- the composer remains visible and focusable;
- the privacy disclosure remains reachable and the emotion disclosure is subordinate;
- no horizontal overflow appears;
- header, face, conversation, and disclosure focus rings remain visible;
- body text and controls remain readable at 125% zoom or equivalent browser font scaling.

Take screenshots only if the environment permits and ensure they contain no private conversation data.

- [ ] **Step 4: Verify reduced motion and public boundaries**

Use browser emulation or a deterministic test to set `prefers-reduced-motion: reduce`. Confirm no new layout animation runs and the face's existing reduced-motion behavior remains active. Verify no additional `fetch`, XHR, transport call, store, or `useChat()` path is introduced by the layout. Run `node tests/desktopGraph.test.js` to confirm the desktop graph remains free of web-only modules.

- [ ] **Step 5: Run final local gates and diff checks**

Run:

```bash
cd frontend && npm test
npm run lint
npm run build
npm audit --audit-level=high
node tests/desktopGraph.test.js
git diff --check origin/main...HEAD
```

Expected: every command exits 0. Use the repository's existing audit/gate scripts if `npm audit` is not the canonical command, without changing CI.

---

### Task 4: Commit, push, open the single PR, and stop

**Files:**
- Modify only branch files created or changed for issue #343.

- [ ] **Step 1: Run the relevant backend/desktop gates if the final diff touches only frontend**

Confirm from the changed-file list that no backend, schema, bridge, or persistence file changed. If the existing CI requires desktop packaging for every branch, run its documented packaging and GUI smoke command from the current main. Do not add backend work.

- [ ] **Step 2: Record the final base and branch evidence**

Run:

```bash
git rev-parse origin/main
git rev-parse HEAD
git status --short --branch
git diff --stat origin/main...HEAD
```

The working tree may retain only the pre-existing protected stash/reference state outside the branch. No secrets, private messages, or personal paths belong in PR evidence.

- [ ] **Step 3: Push the existing issue branch**

```bash
git push -u origin feat/katherine-companion-layout
```

- [ ] **Step 4: Open exactly one PR against main**

Use title:

```text
feat(frontend): center companion mode on Katherine presence
```

The body must include:

```text
Closes #343
```

Also include the base SHA, layout decisions, files changed, history/composer treatment, privacy preservation, accessibility and visual observations at all three widths, reduced-motion evidence, tests, lint, build, audit, CI results, no added dependencies, known risks, rollback, and explicit out-of-scope issues #342, #344, #345, #346, #347, and #348.

- [ ] **Step 5: Wait for the same-HEAD CI checks**

Use `gh pr checks <number> --watch` or the repository's existing check command. Report each required check and stop if an external check is blocked rather than modifying CI to hide it.

- [ ] **Step 6: Stop after PR creation and review evidence**

Do not merge, close another issue, start a follow-on issue, or make changes outside #343.
