/* global process */
process.env.NODE_ENV = 'test';
import { test } from 'node:test';
import assert from 'node:assert';

const { ChatError, classifyHttpError, createChatError } = await import(
    '../src/features/chat/services/chatService.js'
);

// ─── ChatError class ────────────────────────────────────────────────────────

test('ChatError has correct name', () => {
    const err = new ChatError('timeout', 'test');
    assert.strictEqual(err.name, 'ChatError');
    assert.strictEqual(err.type, 'timeout');
    assert.strictEqual(err.message, 'test');
});

// ─── classifyHttpError ───────────────────────────────────────────────────────

test('classifyHttpError: 504 is timeout', () => {
    assert.strictEqual(classifyHttpError(504), 'timeout');
});

test('classifyHttpError: 0 is timeout', () => {
    assert.strictEqual(classifyHttpError(0), 'timeout');
});

test('classifyHttpError: 429 is rate_limited', () => {
    assert.strictEqual(classifyHttpError(429), 'rate_limited');
});

test('classifyHttpError: 503 is service_unavailable', () => {
    assert.strictEqual(classifyHttpError(503), 'service_unavailable');
});

test('classifyHttpError: 422 is validation', () => {
    assert.strictEqual(classifyHttpError(422), 'validation');
});

test('classifyHttpError: 500 is unknown', () => {
    assert.strictEqual(classifyHttpError(500), 'unknown');
});

test('classifyHttpError: 418 is unknown', () => {
    assert.strictEqual(classifyHttpError(418), 'unknown');
});

// ─── createChatError ────────────────────────────────────────────────────────

function makeAxiosError(code, status, hasResponse = true) {
    const err = new Error('fake');
    err.code = code || '';
    err.response = hasResponse ? { status: status || 500 } : undefined;
    return err;
}

test('createChatError: ECONNABORTED is timeout', () => {
    const err = makeAxiosError('ECONNABORTED');
    const chatErr = createChatError(err);
    assert(chatErr instanceof ChatError);
    assert.strictEqual(chatErr.type, 'timeout');
});

test('createChatError: ERR_CANCELED is timeout', () => {
    const err = makeAxiosError('ERR_CANCELED');
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'timeout');
});

test('createChatError: no response is timeout', () => {
    const err = makeAxiosError('', 0, false);
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'timeout');
});

test('createChatError: HTTP 429 is rate_limited', () => {
    const err = makeAxiosError('', 429);
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'rate_limited');
});

test('createChatError: HTTP 503 is service_unavailable', () => {
    const err = makeAxiosError('', 503);
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'service_unavailable');
});

test('createChatError: HTTP 422 is validation', () => {
    const err = makeAxiosError('', 422);
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'validation');
});

test('createChatError: unknown error produces generic message', () => {
    const err = makeAxiosError('', 500);
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'unknown');
    assert.ok(chatErr.message.includes('Erro'));
});

test('createChatError: raw axios object not exposed in message', () => {
    const err = makeAxiosError('', 500);
    err.config = {
        url: '/chat',
        method: 'post',
        headers: { Authorization: 'Bearer fake-token' },
    };
    const chatErr = createChatError(err);
    assert.ok(!chatErr.message.includes('Bearer'));
    assert.ok(!chatErr.message.includes('Authorization'));
    assert.ok(!chatErr.message.includes('config'));
    assert.ok(!chatErr.message.includes('/chat'));
});

// ─── Abort/timeout error classification ─────────────────────────────────────

test('ERR_CANCELED produces timeout error message', () => {
    const err = makeAxiosError('ERR_CANCELED');
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'timeout');
    assert.ok(chatErr.message.includes('tempo limite') || chatErr.message.includes('limite'));
});

test('ECONNABORTED produces timeout error message', () => {
    const err = makeAxiosError('ECONNABORTED');
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'timeout');
});

// ─── HTTP 504 (Gateway Timeout) ─────────────────────────────────────────────

test('HTTP 504 produces timeout type', () => {
    const err = makeAxiosError('', 504);
    const chatErr = createChatError(err);
    assert.strictEqual(chatErr.type, 'timeout');
});
