# Katherine Companion Layout Design

## Context

Issue #343 changes only the desktop companion composition. `main` already contains the safe `KatherineFace` boundary from #341, the mode-aware transport, and the single `useChat()` chat controller. The current desktop root renders the same chat-first composition as the web root and overlays a small face in the header.

## Goals

- Make the decorative KatherineFace the dominant visual element in `AppDesktop`.
- Keep the conversation immediately usable through an always-available history rail and composer.
- Keep real local privacy operations available without making them visual protagonists.
- Keep the legacy EmotionPanel available but subordinate and collapsed by default.
- Preserve one `useChat()` instance per mounted conversation and the existing transport, loading, error, history, clear-screen, and focus behavior.
- Preserve `AppWeb` and the existing face, emotion, backend, bridge, and persistence contracts.
- Provide a named optional auxiliary slot for future desktop surfaces without fake data or placeholder copy.

## Non-goals

- No changes to `KatherineFace`, the face mapper, bfce, emotional state, backend, bridge, persistence, safety, memory, relationship, provider, web redesign, floating mode, settings workspace, or future sidebar content.
- No new dependency, store, network request, polling loop, or animation system.

## Chosen composition

`ChatWindow` remains the single state owner. It accepts an optional `renderLayout` function. After calling `useChat()` once, it delegates the model to that function when present. The default path stays unchanged for `AppWeb` and existing `ChatWindow` consumers.

`AppDesktop` passes a module-level renderer for a new presentational `CompanionLayout`. `CompanionLayout` receives only the existing chat model values and composes the existing `ChatHeader`, `MessageList`, `ChatInput`, `KatherineFace`, `EmotionPanel`, and `PrivacyPanel` components. It does not call `useChat()` and does not know transport details beyond passing the existing transport to `PrivacyPanel`.

The desktop surface is:

1. A compact existing header with title, bridge status, and clear-screen action. The face is not placed in the header.
2. A two-column body. The borderless left presence pane has generous negative space and a large `KatherineFace`. The right conversation rail contains the scrollable history and the composer.
3. A quiet secondary utility strip below the composer. The existing EmotionPanel is inside a closed native `details` disclosure when an emotion state exists. The existing PrivacyPanel is inside a closed native `details` disclosure and keeps every confirmation flow unchanged.
4. An optional `auxiliarySlot` renders only when supplied. The default desktop companion has no placeholder, fake data, or empty chrome for future work.

## Responsive behavior

- At wide desktop widths, the body uses `minmax(0, 1.45fr) minmax(20rem, 31rem)`, giving the presence pane most of the window.
- At medium desktop widths, the conversation rail stays at least 19rem wide and the face uses a smaller but still dominant `clamp()` size.
- At the minimum supported desktop width, the two columns remain usable with reduced padding. Below the desktop floor, the layout falls back to a single column with the presence pane above the conversation rail rather than allowing horizontal overflow.
- The history rail owns vertical scrolling. The root and body use `min-height: 0` so the composer remains reachable.

## Accessibility and performance

- `KatherineFace` remains `aria-hidden="true"` and has no focusable controls.
- The body uses labeled `main` and `section` regions for presence and conversation history. Existing textarea labeling, Enter behavior, loading status, system error text, copy controls, clear-screen confirmation, and privacy confirmations remain unchanged.
- Native `details` elements provide keyboard access to subordinate utilities without focus traps.
- No new motion is added. Existing face reduced-motion behavior remains authoritative, and CSS disables any incidental transitions in `prefers-reduced-motion`.
- The layout uses CSS only, with no new listeners, timers, stores, requests, or runtime work while idle.

## Validation

Component tests will prove face-first DOM composition, one supplied chat model, empty and valid emotion states, loading status, system error visibility, history access, privacy disclosure access, no fake auxiliary content, and web-root regression. The real desktop entry will be built and viewed at 1440px, 1024px, and 800px widths. Visual review will check hierarchy, spacing, overflow, focus order, zoom/font scaling, contrast, reduced motion, and absence of unintended desktop/web graph changes.

## Alternatives rejected

- Modifying `AppWeb` or making the shared chat markup branch on a desktop flag would risk web regression and spread desktop conditions through the shared component.
- A second component-owned `useChat()` would duplicate the conversation controller boundary. The render-layout seam keeps one hook call in `ChatWindow`.
- A permanent sidebar full of future cards or metrics would introduce fake product surface and make the legacy emotion panel too prominent.
