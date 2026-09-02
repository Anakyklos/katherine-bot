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

// =========================================================================
// #336: companion op callers (T012)
// =========================================================================

import {
    getRuntimeState,
    loadHistory,
    sendMessageViaBridge,
    runPrivacyOpViaBridge,
} from '../src/lib/desktopBridge.js';

function apiWindow(api) {
    return {
        addEventListener: () => {},
        pywebview: { api },
    };
}

test('getRuntimeState returns the validated state payload', async () => {
    const window = apiWindow({
        runtime_state: async () => ({
            ok: true, storage: true, provider_configured: true, revision: 3,
        }),
    });
    assert.deepEqual(await getRuntimeState(window), {
        ok: true, storage: true, provider_configured: true, revision: 3,
    });
});

test('getRuntimeState returns null outside the shell', async () => {
    assert.equal(await getRuntimeState(apiWindow({})), null);
    assert.equal(await getRuntimeState({ addEventListener: () => {} }), null);
});

test('getRuntimeState rejects wrong shapes (ok !== true)', async () => {
    const window = apiWindow({
        runtime_state: async () => ({ ok: false, storage: true }),
    });
    assert.equal(await getRuntimeState(window), null);
});

test('loadHistory returns {data: [...]} for a valid payload', async () => {
    const window = apiWindow({
        load_history: async (limit) => {
            assert.equal(limit, 50);
            return { ok: true, messages: [{ id: '1', role: 'user', content: 'hi', created_at: 1 }] };
        },
    });
    const result = await loadHistory(window, 50);
    assert.deepEqual(result, {
        data: [{ id: '1', role: 'user', content: 'hi', created_at: 1 }],
    });
});

test('loadHistory throws ChatError-shaped error when bridge reports failure', async () => {
    const window = apiWindow({
        load_history: async () => ({
            ok: false, code: 'storage', message: 'O armazenamento local não está disponível.',
        }),
    });
    await assert.rejects(
        () => loadHistory(window, 50),
        (err) => err.name === 'ChatError' && err.type === 'service_unavailable',
    );
});

test('loadHistory returns null outside the shell', async () => {
    assert.equal(await loadHistory({ addEventListener: () => {} }, 50), null);
});

test('sendMessageViaBridge returns the consumed shape on success', async () => {
    const window = apiWindow({
        send_message: async (requestId, message) => {
            assert.equal(requestId, 'req-1');
            assert.equal(message, 'olá');
            return {
                ok: true, success: true, response: 'tudo bem',
                emotion_state: { schema_version: 1 }, replayed: false,
            };
        },
    });
    const result = await sendMessageViaBridge('req-1', 'olá', window);
    assert.equal(result.response, 'tudo bem');
    assert.deepEqual(result.emotion_state, { schema_version: 1 });
});

test('sendMessageViaBridge throws ChatError with the stable bridge code', async () => {
    const cases = [
        ['configuration', 'configuration', 'O provedor remoto não está configurado neste ambiente.'],
        ['request_conflict', 'request_conflict', 'Este envio não pôde ser reconciliado. Envie a mensagem novamente.'],
        ['rate_limited', 'rate_limited', 'Muitas requisições. Aguarde um momento e tente novamente.'],
        ['timeout', 'timeout', 'A requisição excedeu o tempo limite.'],
        ['validation', 'validation', 'Dados inválidos enviados.'],
        ['storage', 'service_unavailable', 'Serviço temporariamente indisponível. Tente novamente mais tarde.'],
        ['service_unavailable', 'service_unavailable', 'Serviço temporariamente indisponível. Tente novamente mais tarde.'],
        ['something_odd', 'unknown', 'Erro ao falar com a Katherine. Tente novamente.'],
    ];
    for (const [bridgeCode, expectedType, expectedMessage] of cases) {
        const window = apiWindow({
            send_message: async () => ({
                ok: false, success: false,
                error_code: bridgeCode, error_message: expectedMessage,
            }),
        });
        await assert.rejects(
            () => sendMessageViaBridge('req-1', 'olá', window),
            (err) => err.name === 'ChatError' && err.type === expectedType && err.message === expectedMessage,
            `bridge code ${bridgeCode} must map to ${expectedType}`,
        );
    }
});

test('sendMessageViaBridge rejects a non-dict success payload', async () => {
    const window = apiWindow({
        send_message: async () => 'not a dict',
    });
    await assert.rejects(
        () => sendMessageViaBridge('req-1', 'olá', window),
        (err) => err.name === 'ChatError' && err.type === 'unknown',
    );
});

// =========================================================================
// #336 review blocker 2: real timeout/cancel semantics on the bridge path
// =========================================================================

test('sendMessageViaBridge settles as timeout when the signal aborts (bridge never resolves)', async () => {
    // Deterministic hung bridge: the underlying promise NEVER settles.
    // The aborted signal must race it and reject with ChatError timeout.
    const window = apiWindow({
        send_message: () => new Promise(() => {}), // hangs forever
    });
    const controller = new AbortController();
    const pending = sendMessageViaBridge('req-1', 'olá', window, {
        signal: controller.signal,
    });
    controller.abort();
    await assert.rejects(
        () => pending,
        (err) => err.name === 'ChatError' && err.type === 'timeout',
    );
});

test('sendMessageViaBridge timeout rejects immediately on an already-aborted signal', async () => {
    let bridgeCalled = false;
    const window = apiWindow({
        send_message: async () => {
            bridgeCalled = true;
            return { ok: true, success: true, response: 'x', emotion_state: {} };
        },
    });
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(
        () => sendMessageViaBridge('req-1', 'olá', window, { signal: controller.signal }),
        (err) => err.name === 'ChatError' && err.type === 'timeout',
    );
    assert.equal(bridgeCalled, false, 'aborted-before-call must not hit the bridge');
});

test('sendMessageViaBridge passes through when the bridge settles before the signal aborts', async () => {
    const window = apiWindow({
        send_message: async () => ({
            ok: true, success: true, response: 'pronto',
            emotion_state: { mood: 'ok' },
        }),
    });
    const controller = new AbortController();
    const result = await sendMessageViaBridge('req-1', 'olá', window, {
        signal: controller.signal,
    });
    assert.equal(result.response, 'pronto');
    // A late abort must NOT change the already-settled result.
    controller.abort();
    assert.equal(result.response, 'pronto');
});

test('runPrivacyOpViaBridge calls the right op and returns the result', async () => {
    const calls = [];
    const api = {};
    for (const op of ['delete_history', 'delete_memories', 'reset_emotional_state', 'reset_relationship_state']) {
        api[op] = async (...args) => {
            calls.push([op, args]);
            return { ok: true, success: true, result: { status: 'applied', rows: 2 } };
        };
    }
    const window = apiWindow(api);

    const result = await runPrivacyOpViaBridge('delete_history', window);
    assert.deepEqual(result, { status: 'applied', rows: 2 });
    assert.deepEqual(calls, [['delete_history', []]]);

    await runPrivacyOpViaBridge('reset_emotional_state', window);
    assert.deepEqual(calls[1], ['reset_emotional_state', []]);
});

test('runPrivacyOpViaBridge rejects unknown op names before touching the bridge', async () => {
    let touched = false;
    const window = apiWindow(new Proxy({}, { get: () => { touched = true; } }));
    await assert.rejects(
        () => runPrivacyOpViaBridge('eval_sql', window),
        (err) => err.name === 'ChatError' && err.type === 'validation',
    );
    assert.equal(touched, false);
});

test('runPrivacyOpViaBridge maps a failed op to a ChatError', async () => {
    const window = apiWindow({
        delete_history: async () => ({
            ok: false, success: false, error_code: 'storage',
            error_message: 'O armazenamento local não está disponível.',
        }),
    });
    await assert.rejects(
        () => runPrivacyOpViaBridge('delete_history', window),
        (err) => err.name === 'ChatError' && err.type === 'service_unavailable',
    );
});
