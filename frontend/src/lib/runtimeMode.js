/**
 * Explicit runtime mode detection (#334, review blocker B4).
 *
 * Web mode must never be confused with the desktop shell merely because
 * Supabase credentials are absent. The mode is a *positive* detection:
 *
 * - `desktop`: running inside the pywebview shell — the bridge global
 *   `window.pywebview` exists AND the bundle was built without web
 *   credentials. The shell is the only place `window.pywebview` is
 *   injected, so this cannot trigger in a regular browser.
 * - `web-dev`: `import.meta.env.DEV` is true (Vite dev/preview server).
 * - `web`: a normal production web deployment (with credentials).
 *
 * Every consumer branches on this explicit mode instead of guessing from
 * "credentials are missing", which would silently treat a misconfigured
 * web deploy as a desktop build.
 */

/**
 * Detect the runtime mode from explicit signals only.
 *
 * `targetWindow` is injectable for tests; production callers omit it and
 * the real `window` is used.
 *
 * @returns {'desktop' | 'web-dev' | 'web'}
 */
export function detectRuntimeMode(targetWindow) {
    const scope = targetWindow ?? (typeof window !== 'undefined' ? window : undefined);

    // The pywebview bridge global is injected only by the desktop shell.
    // `window.pywebview` in a plain browser is not a thing (the shell's
    // injected script is the only creator), so presence here is a
    // positive shell signal, independent of credentials.
    const hasShellGlobal = Boolean(scope?.pywebview);

    if (hasShellGlobal) {
        return 'desktop';
    }

    // Vite dev/preview server: explicit build-time flag, not a guess
    // from missing credentials.
    if (import.meta.env?.DEV) {
        return 'web-dev';
    }

    return 'web';
}

/**
 * True only inside the pywebview desktop shell.
 *
 * Uses the same positive detection as `detectRuntimeMode`: a web page
 * without credentials is NOT "desktop" — it is still `web` (or
 * `web-dev`), and must not show shell-only behavior.
 */
export function isDesktopShell(targetWindow) {
    return detectRuntimeMode(targetWindow) === 'desktop';
}
