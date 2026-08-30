/**
 * Tests for explicit runtime mode detection (#334, review B4).
 *
 * Web mode must never be confused with the desktop shell merely because
 * credentials are absent. Mode is detected from explicit signals:
 * - `desktop`: `window.pywebview` present (only the shell injects it)
 * - `web-dev`: `import.meta.env.DEV` (build-time flag)
 * - `web`: everything else (production web)
 *
 * Node-native tests (no DOM): the window object is injected explicitly.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { detectRuntimeMode, isDesktopShell } from '../src/lib/runtimeMode.js';

test('desktop shell is detected positively via window.pywebview', () => {
    const shellWindow = { pywebview: { api: {} } };
    assert.equal(detectRuntimeMode(shellWindow), 'desktop');
    assert.equal(isDesktopShell(shellWindow), true);
});

test('web browser without the shell global is web, even without credentials', () => {
    // The core B4 regression: missing credentials alone must NOT imply
    // desktop. A plain window (no pywebview global) is web mode.
    assert.equal(detectRuntimeMode({}), 'web');
    assert.equal(isDesktopShell({}), false);
});

test('undefined window (SSR/edge) is web, not desktop', () => {
    assert.equal(detectRuntimeMode(undefined), 'web');
    assert.equal(isDesktopShell(undefined), false);
});

test('window with pywebview: null is not a shell signal', () => {
    // Falsy pywebview (null) must not count as the shell global.
    assert.equal(detectRuntimeMode({ pywebview: null }), 'web');
    assert.equal(detectRuntimeMode({ pywebview: undefined }), 'web');
});

test('injection: explicit windows drive detection (no global mutation)', () => {
    const plain = {};
    const shell = { pywebview: {} };
    assert.equal(isDesktopShell(plain), false);
    assert.equal(isDesktopShell(shell), true);
});
