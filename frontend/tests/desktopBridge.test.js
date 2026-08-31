/**
 * Tests for the desktop bridge client (#334).
 *
 * Node-native tests (no DOM): validate the contract rules —
 * null outside the shell, payload validation inside it, bounded wait.
 * The window object is injected explicitly (no global mutation).
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { checkDesktopHealth } from '../src/lib/desktopBridge.js';

function makeWindow({ pywebview } = {}) {
    const window = {
        addEventListener: () => {},
    };
    if (pywebview !== undefined) {
        window.pywebview = pywebview;
    }
    return window;
}

test('returns null when window.pywebview is absent (web mode)', async () => {
    assert.equal(await checkDesktopHealth(makeWindow()), null);
});

test('returns null when window is undefined (SSR/edge)', async () => {
    assert.equal(await checkDesktopHealth(undefined), null);
});

test('returns null when api.health is missing', async () => {
    const window = makeWindow({ pywebview: { api: {} } });
    assert.equal(await checkDesktopHealth(window), null);
});

test('returns health payload for a valid bridge', async () => {
    const window = makeWindow({
        pywebview: {
            api: {
                health: async () => ({ ok: true, api_version: 1 }),
            },
        },
    });
    assert.deepEqual(await checkDesktopHealth(window), { ok: true, api_version: 1 });
});

test('rejects payload with wrong shape (ok !== true)', async () => {
    const window = makeWindow({
        pywebview: {
            api: {
                health: async () => ({ ok: false, api_version: 1 }),
            },
        },
    });
    assert.equal(await checkDesktopHealth(window), null);
});

test('rejects payload with non-number api_version', async () => {
    const window = makeWindow({
        pywebview: {
            api: {
                health: async () => ({ ok: true, api_version: '1' }),
            },
        },
    });
    assert.equal(await checkDesktopHealth(window), null);
});

test('bridge rejection (sanitized error) maps to null', async () => {
    const window = makeWindow({
        pywebview: {
            api: {
                health: async () => {
                    throw { ok: false, code: 'internal_error', message: 'x' };
                },
            },
        },
    });
    assert.equal(await checkDesktopHealth(window), null);
});

test('waits for pywebviewready when api is not yet exposed', async () => {
    let resolveReady;
    const window = {
        addEventListener: (name, fn) => {
            if (name === 'pywebviewready') {
                resolveReady = fn;
            }
        },
        pywebview: {
            api: undefined,
        },
    };
    const pending = checkDesktopHealth(window);
    // Simulate the shell finishing bridge setup.
    window.pywebview.api = {
        health: async () => ({ ok: true, api_version: 1 }),
    };
    resolveReady();
    assert.deepEqual(await pending, { ok: true, api_version: 1 });
});
