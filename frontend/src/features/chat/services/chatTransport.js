/**
 * Mode-aware chat transport boundary (#336, T013).
 *
 * This is the single seam between the chat feature and the outside
 * world. The chat UI talks to the transport; the transport decides
 * where the request goes:
 *
 * - **web** — the existing `chatService` (Axios against the web API).
 *   Behavior is delegated unchanged; no web code path is touched.
 * - **desktop** — the pywebview bridge callers in
 *   `lib/desktopBridge.js` (the only file allowed to touch
 *   `window.pywebview`). No HTTP is involved on this branch.
 *
 * Rules:
 * - The consumed shapes are identical on both branches:
 *   `fetchHistory()` → `{ data: [...] }`,
 *   `sendMessage()` → `{ response, emotion_state }`,
 *   failures → thrown `ChatError` with stable types.
 * - Desktop-only surface (`runPrivacyOp`, `getRuntimeState`) exists on
 *   the transport but throws in web mode: callers gate on
 *   `isDesktopTransport` first.
 * - The mode is explicit (`createTransport({ mode })`), never sniffed
 *   from "credentials are missing".
 *
 * Graph isolation (#336, review blocker 1): the web branch's
 * `chatService` (which statically imports Axios + apiClient +
 * supabaseClient) is behind a DYNAMIC import that only the web
 * branch awaits. The desktop bundle's module graph therefore never
 * contains chatService or anything it pulls in — mechanically proven
 * by `tests/desktopGraph.test.js` against the built bundle.
 */

import { ChatError } from './chatError.js';
import {
    getRuntimeState,
    loadHistory,
    sendMessageViaBridge,
    runPrivacyOpViaBridge,
} from '../../../lib/desktopBridge.js';
import { isDesktopShell } from '../../../lib/runtimeMode.js';

const PRIVACY_OP_NAMES = Object.freeze([
    'delete_history',
    'delete_memories',
    'reset_emotional_state',
    'reset_relationship_state',
]);

/**
 * Create a transport for one runtime mode.
 *
 * @param {object} options
 * @param {'web'|'desktop'} options.mode — explicit, detected once at
 *   the app boundary via `detectRuntimeMode`, not guessed here.
 * @param {object} [options.chatService] — web branch implementation
 *   (injectable for tests; the production web branch loads the real
 *   module lazily — see `_loadChatService`).
 * @param {Window} [options.targetWindow] — desktop branch (injectable
 *   for tests; defaults to the global window).
 * @param {number} [options.historyLimit=50]
 */
export function createTransport({
    mode,
    chatService: webService,
    targetWindow,
    historyLimit = 50,
} = {}) {
    if (mode !== 'web' && mode !== 'desktop') {
        throw new Error('createTransport requires mode "web" or "desktop"');
    }

    if (mode === 'web') {
        return createWebTransport(webService ?? _loadChatService());
    }
    return createDesktopTransport(targetWindow, historyLimit);
}

/**
 * Lazily load the web chatService.
 *
 * Static-import-free on purpose (review blocker 1): a static import
 * would place chatService — and its Axios/apiClient/supabaseClient
 * graph — inside every bundle that includes this module, including
 * the desktop bundle. A dynamic import keeps it in the web chunk
 * only. It is awaited at each web call site, so a module that is
 * still initializing is handled correctly (the web interface is
 * async regardless).
 */
function _loadChatService() {
    return _chatServicePromise ?? (_chatServicePromise = import('./chatService.js'));
}

let _chatServicePromise = null;

/** Web branch: pure delegation to chatService (Axios), unchanged. */
function createWebTransport(webService) {
    // If a live module was injected (tests), use it directly; otherwise
    // the lazy module (possibly still initializing) is awaited per call
    // — the web branch is async either way.
    const service = _isChatServiceModule(webService) ? webService : null;
    return Object.freeze({
        mode: 'web',

        /** @param {{signal?: AbortSignal}} [options] */
        async fetchHistory(options = {}) {
            const impl = service ?? (await _loadChatService());
            return impl.fetchHistory(options);
        },

        async sendMessage(message, options, requestId) {
            const impl = service ?? (await _loadChatService());
            return impl.sendMessage(message, options, requestId);
        },

        async runPrivacyOp() {
            // Privacy ops are a desktop-only surface; the web app has
            // its own account-data flows. Failing closed keeps the
            // boundary honest instead of silently no-op'ing.
            throw new ChatError(
                'validation',
                'Dados inválidos enviados.',
            );
        },

        async getRuntimeState() {
            return null;
        },
    });
}

/**
 * True when `candidate` is a live chatService module (or facsimile):
 * an object exposing the async operations the web branch delegates to.
 * A dynamic-import Promise must NOT take this path: awaiting per call
 * keeps a still-initializing module correct.
 */
function _isChatServiceModule(candidate) {
    return Boolean(
        candidate
        && typeof candidate.fetchHistory === 'function'
        && typeof candidate.sendMessage === 'function',
    );
}

/** Desktop branch: bridge callers only, never Axios. */
function createDesktopTransport(targetWindow, historyLimit) {
    return Object.freeze({
        mode: 'desktop',

        /**
         * History via the bridge. The `signal` argument is accepted for
         * interface parity but unused on this branch (no HTTP request
         * exists to abort); the desktop guard is the caller's timeout.
         */
        async fetchHistory(options = {}) {
            // Accepted for interface parity with the web branch (the
            // AbortSignal applies to HTTP only); deliberately unused.
            void options;
            const result = await loadHistory(targetWindow, historyLimit);
            if (result === null) {
                throw new ChatError(
                    'unknown',
                    'Erro ao falar com a Katherine. Tente novamente.',
                );
            }
            return result;
        },

        async sendMessage(message, _options, requestId) {
            // The 50s guard timer lives in useChat; the bridge call has
            // its own provider-side deadline. `signal`/`timeout` are
            // web-contract arguments, ignored here deliberately.
            return sendMessageViaBridge(requestId, message, targetWindow);
        },

        async runPrivacyOp(op) {
            if (!PRIVACY_OP_NAMES.includes(op)) {
                throw new ChatError('validation', 'Dados inválidos enviados.');
            }
            return runPrivacyOpViaBridge(op, targetWindow);
        },

        async getRuntimeState() {
            return getRuntimeState(targetWindow);
        },
    });
}

// Re-exported for backward compatibility; the dependency-free home is
// transportMode.js (desktop-gated UI must not pull the web graph).
export { isDesktopTransport } from './transportMode.js';

/**
 * Build the default transport for the current runtime.
 *
 * Mode detection happens exactly once, here, at the app boundary —
 * consumers receive an explicit branch instead of sprinkling
 * `if (desktop)` checks.
 */
export function createDefaultTransport(targetWindow) {
    const scope = targetWindow ?? (typeof window !== 'undefined' ? window : undefined);
    return createTransport({
        mode: isDesktopShell(scope) ? 'desktop' : 'web',
        targetWindow: scope,
        // chatService intentionally NOT passed: the web branch loads it
        // lazily so the desktop graph stays free of the web modules.
    });
}
