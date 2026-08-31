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
