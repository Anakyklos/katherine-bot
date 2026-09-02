/**
 * Minimal, explicit desktop bridge client (#334).
 *
 * The desktop shell (pywebview) injects `window.pywebview.api` with an
 * allowlisted surface (currently: `health()`). This module is the only
 * place the frontend is allowed to touch it, so the contract stays
 * auditable in one file.
 *
 * Rules:
 * - Never trust the bridge blindly: presence and payload shape are
 *   validated before callers see anything.
 * - Returns `null` when not running inside the desktop shell (normal
 *   web mode is unaffected and never waits for the bridge).
 * - Structured errors from the bridge are passed through as-is; the
 *   bridge guarantees they are sanitized (no tracebacks/paths).
 */

const BRIDGE_READY_TIMEOUT_MS = 5000;

/**
 * Wait (bounded) for the pywebview bridge, then call `health()`.
 *
 * `targetWindow` is injectable for tests; production callers omit it and
 * the real `window` is used.
 *
 * @returns {Promise<{ok: boolean, api_version: number} | null>}
 *   The sanitized health payload, or null when the desktop bridge is
 *   unavailable (web mode, timeout, invalid payload).
 */
export async function checkDesktopHealth(targetWindow) {
    const scope = targetWindow ?? (typeof window !== 'undefined' ? window : undefined);
    if (!scope || !scope.pywebview) {
        return null;
    }

    const pywebview = scope.pywebview;
    try {
        await waitForBridgeReady(scope, pywebview);

        if (typeof pywebview.api?.health !== 'function') {
            return null;
        }

        const payload = await pywebview.api.health();
        if (
            payload &&
            typeof payload === 'object' &&
            payload.ok === true &&
            typeof payload.api_version === 'number'
        ) {
            return { ok: true, api_version: payload.api_version };
        }
        return null;
    } catch {
        // Bridge failures must never break the web experience.
        return null;
    }
}

function waitForBridgeReady(scope, pywebview) {
    if (pywebview.api) {
        return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('timeout')), BRIDGE_READY_TIMEOUT_MS);
        scope.addEventListener(
            'pywebviewready',
            () => {
                clearTimeout(timer);
                resolve();
            },
            { once: true },
        );
    });
}

// =========================================================================
// #336: companion op callers (T012)
// =========================================================================
//
// The same audit rules as checkDesktopHealth: this file is the only place
// the frontend touches window.pywebview; every payload is validated
// before callers see anything; bridge failures collapse into ChatError
// with stable types (never raw errors, never bridge internals).

import { ChatError } from '../features/chat/services/chatError.js';

/**
 * Stable mapping from bridge error codes to frontend ChatError types.
 * `storage` shares the web 'service_unavailable' user message class;
 * `request_replay` maps like the web 409 replay admission code.
 */
const BRIDGE_CODE_TO_CHAT_ERROR = Object.freeze({
    timeout: 'timeout',
    rate_limited: 'rate_limited',
    service_unavailable: 'service_unavailable',
    storage: 'service_unavailable',
    validation: 'validation',
    request_replay: 'request_replay',
    request_conflict: 'request_conflict',
    configuration: 'configuration',
});

const CHAT_ERROR_MESSAGES = Object.freeze({
    timeout: 'A requisição excedeu o tempo limite.',
    rate_limited: 'Muitas requisições. Aguarde um momento e tente novamente.',
    service_unavailable: 'Serviço temporariamente indisponível. Tente novamente mais tarde.',
    validation: 'Dados inválidos enviados.',
    request_replay: 'Este envio já foi recebido, mas a resposta não pode ser recuperada.',
    request_conflict: 'Este envio não pôde ser reconciliado. Envie a mensagem novamente.',
    configuration: 'O provedor remoto não está configurado neste ambiente.',
    unknown: 'Erro ao falar com a Katherine. Tente novamente.',
});

function bridgeCodeToChatError(payload) {
    // Bridge transport failures carry `code` (invalid_input,
    // internal_error, bridge_unavailable); runtime domain failures carry
    // `error_code` (the LocalErrorCode vocabulary). Read both.
    const code = (
        typeof payload?.error_code === 'string' ? payload.error_code :
        typeof payload?.code === 'string' ? payload.code : 'unknown'
    );
    const type = BRIDGE_CODE_TO_CHAT_ERROR[code] ?? 'unknown';
    return new ChatError(type, CHAT_ERROR_MESSAGES[type]);
}

/** Resolve the pywebview api object, or null outside the shell/timeout. */
async function resolveBridge(targetWindow) {
    const scope = targetWindow ?? (typeof window !== 'undefined' ? window : undefined);
    if (!scope || !scope.pywebview) {
        return null;
    }
    try {
        await waitForBridgeReady(scope, scope.pywebview);
    } catch {
        return null;
    }
    return scope.pywebview.api ?? null;
}

function isPlainObject(value) {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Read the runtime readiness + configuration state (#336).
 *
 * @returns {Promise<{ok: boolean, storage: boolean,
 *   provider_configured: boolean, revision: number} | null>}
 */
export async function getRuntimeState(targetWindow) {
    const api = await resolveBridge(targetWindow);
    if (!api || typeof api.runtime_state !== 'function') {
        return null;
    }
    let payload;
    try {
        payload = await api.runtime_state();
    } catch {
        return null;
    }
    if (
        isPlainObject(payload) && payload.ok === true &&
        typeof payload.storage === 'boolean' &&
        typeof payload.provider_configured === 'boolean' &&
        typeof payload.revision === 'number'
    ) {
        return {
            ok: true,
            storage: payload.storage,
            provider_configured: payload.provider_configured,
            revision: payload.revision,
        };
    }
    return null;
}

/**
 * Load the local history window (#336). Returns the axios-like consumed
 * shape `{ data: [...] }` so the transport boundary can treat both
 * branches uniformly; throws a ChatError when the bridge reports a
 * sanitized failure payload.
 *
 * @returns {Promise<{data: Array<{id, role, content, created_at}>} | null>}
 */
export async function loadHistory(targetWindow, limit = 50) {
    const api = await resolveBridge(targetWindow);
    if (!api || typeof api.load_history !== 'function') {
        return null;
    }
    let payload;
    try {
        payload = await api.load_history(limit);
    } catch {
        throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
    }
    if (isPlainObject(payload) && payload.ok === true && Array.isArray(payload.messages)) {
        for (const row of payload.messages) {
            if (!isPlainObject(row) || typeof row.role !== 'string' ||
                typeof row.content !== 'string') {
                throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
            }
        }
        return { data: payload.messages };
    }
    if (isPlainObject(payload) && payload.ok === false) {
        throw bridgeCodeToChatError(payload);
    }
    throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
}

/**
 * Send one conversation turn through the bridge (#336).
 *
 * `options.signal` (AbortSignal) gives the call REAL bounded-time
 * semantics (#336, review blocker 2): an aborted signal races the
 * bridge promise and settles it as a timeout ChatError. The bridge
 * promise itself is never cancelled — the runtime owns the provider
 * deadline and the atomic commit — but the CALLER is never left
 * waiting on a hung bridge.
 *
 * @returns {Promise<{response: string, emotion_state: object,
 *   message_id?: string, revision?: number, replayed?: boolean}>}
 * @throws {ChatError} with the stable bridge code mapping.
 */
export async function sendMessageViaBridge(requestId, message, targetWindow, options = {}) {
    const api = await resolveBridge(targetWindow);
    if (!api || typeof api.send_message !== 'function') {
        throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
    }
    const signal = options.signal ?? null;
    if (signal?.aborted) {
        throw new ChatError('timeout', CHAT_ERROR_MESSAGES.timeout);
    }
    let payload;
    try {
        payload = await raceAbort(api.send_message(requestId, message), signal);
    } catch (error) {
        // raceAbort rejects as ChatError('timeout'): pass it through so
        // the timeout type survives. Anything else from the bridge
        // stays the sanitized unknown mapping.
        if (error instanceof ChatError) {
            throw error;
        }
        throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
    }
    if (
        isPlainObject(payload) && payload.ok === true && payload.success === true &&
        typeof payload.response === 'string' &&
        isPlainObject(payload.emotion_state)
    ) {
        return {
            response: payload.response,
            emotion_state: payload.emotion_state,
            message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
            revision: typeof payload.revision === 'number' ? payload.revision : undefined,
            replayed: payload.replayed === true,
        };
    }
    if (isPlainObject(payload) && (payload.ok === false || payload.success === false)) {
        throw bridgeCodeToChatError(payload);
    }
    throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
}

/**
 * Race a promise against an AbortSignal (#336, review blocker 2).
 *
 * When the signal aborts first, the returned promise rejects with
 * `ChatError('timeout')` and the original promise is left to settle on
 * its own (never cancelled: the runtime may still commit the turn; a
 * replay with the same request id reconciles to it). When the promise
 * settles first, the abort listener is removed and the result is
 * passed through unchanged.
 */
function raceAbort(promise, signal) {
    if (!signal) {
        return promise;
    }
    return new Promise((resolve, reject) => {
        const onAbort = () => {
            cleanup();
            reject(new ChatError('timeout', CHAT_ERROR_MESSAGES.timeout));
        };
        const cleanup = () => signal.removeEventListener('abort', onAbort);
        signal.addEventListener('abort', onAbort, { once: true });
        promise.then(
            (value) => {
                cleanup();
                resolve(value);
            },
            (error) => {
                cleanup();
                reject(error);
            },
        );
    });
}

/** The allowlisted privacy ops reachable through this helper. */
const PRIVACY_OPS = Object.freeze(new Set([
    'delete_history',
    'delete_memories',
    'reset_emotional_state',
    'reset_relationship_state',
]));

/**
 * Run one privacy operation through the bridge (#336). Desktop only;
 * unknown op names are rejected *before* the bridge is touched
 * (fail-closed allowlist on the client side too).
 *
 * @returns {Promise<{status: 'applied', [key: string]: unknown}>}
 * @throws {ChatError} when the op is unknown or the bridge reports failure.
 */
export async function runPrivacyOpViaBridge(op, targetWindow) {
    if (!PRIVACY_OPS.has(op)) {
        throw new ChatError('validation', CHAT_ERROR_MESSAGES.validation);
    }
    const api = await resolveBridge(targetWindow);
    if (!api || typeof api[op] !== 'function') {
        throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
    }
    let payload;
    try {
        payload = await api[op]();
    } catch {
        throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
    }
    if (isPlainObject(payload) && payload.ok === true && payload.success === true &&
        isPlainObject(payload.result) && payload.result.status === 'applied') {
        return payload.result;
    }
    if (isPlainObject(payload) && (payload.ok === false || payload.success === false)) {
        throw bridgeCodeToChatError(payload);
    }
    throw new ChatError('unknown', CHAT_ERROR_MESSAGES.unknown);
}
