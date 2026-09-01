/**
 * Tests for the mode-aware chat transport boundary (#336, T013).
 *
 * The transport is the single seam between the chat feature and the
 * outside world: web mode reuses chatService (Axios, unchanged
 * behavior); desktop mode routes through the pywebview bridge
 * (desktopBridge.js callers, never Axios).
 *
 * Node-native tests (no DOM): windows and transports are injected.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
    createTransport,
    isDesktopTransport,
} from '../src/features/chat/services/chatTransport.js';
import { ChatError } from '../src/features/chat/services/chatService.js';

function desktopWindow(api) {
    return { addEventListener: () => {}, pywebview: { api } };
}

test('web transport delegates history to chatService unchanged', async () => {
    const calls = [];
    const webService = {
        fetchHistory: async ({ signal }) => {
            calls.push(['history', signal]);
            return { data: [{ role: 'user', content: 'a' }] };
        },
        sendMessage: async (message, options, requestId) => {
            calls.push(['send', message, options, requestId]);
            return { response: 'r', emotion_state: { schema_version: 1 } };
        },
    };
    const transport = createTransport({ mode: 'web', chatService: webService });

    const history = await transport.fetchHistory({ signal: 'sig' });
    assert.deepEqual(history, { data: [{ role: 'user', content: 'a' }] });
    assert.deepEqual(calls[0], ['history', 'sig']);

    const result = await transport.sendMessage('oi', { signal: 'sig', timeout: 50000 }, 'req-1');
    assert.deepEqual(result, { response: 'r', emotion_state: { schema_version: 1 } });
    assert.deepEqual(calls[1], ['send', 'oi', { signal: 'sig', timeout: 50000 }, 'req-1']);
});

test('web transport privacy ops throw (desktop-only surface)', async () => {
    const transport = createTransport({
        mode: 'web',
        chatService: { fetchHistory: async () => ({}), sendMessage: async () => ({}) },
    });
    await assert.rejects(
        () => transport.runPrivacyOp('delete_history'),
        (err) => err.name === 'ChatError' && err.type === 'validation',
    );
});

test('desktop transport history routes through the bridge', async () => {
    const window = desktopWindow({
        load_history: async (limit) => {
            assert.equal(limit, 50);
            return { ok: true, messages: [{ id: '1', role: 'user', content: 'oi', created_at: 5 }] };
        },
    });
    const transport = createTransport({ mode: 'desktop', targetWindow: window });

    const history = await transport.fetchHistory({ signal: 'irrelevant-web-only' });
    assert.deepEqual(history, {
        data: [{ id: '1', role: 'user', content: 'oi', created_at: 5 }],
    });
});

test('desktop transport send routes through the bridge with the requestId', async () => {
    const window = desktopWindow({
        send_message: async (requestId, message) => {
            assert.equal(requestId, 'req-9');
            assert.equal(message, 'tudo bem?');
            return {
                ok: true, success: true, response: 'sim',
                emotion_state: { schema_version: 1 }, replayed: false,
            };
        },
    });
    const transport = createTransport({ mode: 'desktop', targetWindow: window });

    const result = await transport.sendMessage('tudo bem?', { timeout: 50000 }, 'req-9');
    assert.equal(result.response, 'sim');
    assert.deepEqual(result.emotion_state, { schema_version: 1 });
});

test('desktop transport send maps bridge failures to ChatError', async () => {
    const window = desktopWindow({
        send_message: async () => ({
            ok: false, success: false, error_code: 'configuration',
            error_message: 'O provedor remoto não está configurado.',
        }),
    });
    const transport = createTransport({ mode: 'desktop', targetWindow: window });

    await assert.rejects(
        () => transport.sendMessage('oi', {}, 'req-1'),
        (err) => err instanceof ChatError && err.type === 'configuration',
    );
});

test('desktop transport privacy ops reach the bridge allowlist', async () => {
    const calls = [];
    const api = {};
    for (const op of ['delete_history', 'delete_memories', 'reset_emotional_state', 'reset_relationship_state']) {
        api[op] = async () => {
            calls.push(op);
            return { ok: true, success: true, result: { status: 'applied' } };
        };
    }
    const transport = createTransport({ mode: 'desktop', targetWindow: desktopWindow(api) });

    for (const op of ['delete_history', 'delete_memories', 'reset_emotional_state', 'reset_relationship_state']) {
        const result = await transport.runPrivacyOp(op);
        assert.deepEqual(result, { status: 'applied' });
    }
    assert.deepEqual(calls, [
        'delete_history', 'delete_memories', 'reset_emotional_state', 'reset_relationship_state',
    ]);
});

test('desktop transport rejects unknown privacy ops before the bridge', async () => {
    let touched = false;
    const window = desktopWindow(new Proxy({}, { get: () => { touched = true; } }));
    const transport = createTransport({ mode: 'desktop', targetWindow: window });

    await assert.rejects(
        () => transport.runPrivacyOp('read_file'),
        (err) => err.name === 'ChatError' && err.type === 'validation',
    );
    assert.equal(touched, false);
});

test('isDesktopTransport marks mode explicitly', () => {
    const web = createTransport({ mode: 'web', chatService: {} });
    const desktop = createTransport({ mode: 'desktop', targetWindow: desktopWindow({}) });
    assert.equal(isDesktopTransport(web), false);
    assert.equal(isDesktopTransport(desktop), true);
});

test('transport construction fails closed on unknown mode', () => {
    assert.throws(() => createTransport({ mode: 'weird' }), /mode/);
});
