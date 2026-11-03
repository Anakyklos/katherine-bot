import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { ChatError } from '../src/features/chat/services/chatError.js';
import { useChat } from '../src/features/chat/hooks/useChat.js';

function response(text, messageId, replayed = false) {
    return {
        response: text,
        message_id: messageId,
        replayed,
        emotion_state: {
            schema_version: 1,
            pad: { pleasure: 0, arousal: 0, dominance: 0 },
            dominant_emotions: [],
            mood_label: 'NEUTRA',
            timestamp: 1000,
        },
    };
}

function makeDesktopTransport({ history = [], sendMessage = vi.fn() } = {}) {
    return {
        mode: 'desktop',
        fetchHistory: vi.fn(async () => ({ data: history })),
        sendMessage,
        runPrivacyOp: vi.fn(),
    };
}

async function setAndSend(result, content) {
    let send;
    await act(async () => {
        result.current.setInput(content);
    });
    await waitFor(() => expect(result.current.input).toBe(content));
    await act(async () => {
        send = result.current.handleSend();
        await send;
    });
    return send;
}

describe('desktop chat intent reconciliation', () => {
    let uuid;

    beforeEach(() => {
        const ids = ['request-one', 'request-two', 'request-three'];
        uuid = vi.fn(() => ids.shift());
        vi.stubGlobal('crypto', { randomUUID: uuid });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    it('creates one request id per explicit send and a stable optimistic identity', async () => {
        let resolveFirst;
        const transport = makeDesktopTransport({
            sendMessage: vi.fn()
                .mockImplementationOnce(() => new Promise(resolve => { resolveFirst = resolve; }))
                .mockResolvedValueOnce(response('segunda resposta', 'assistant-2')),
        });
        const { result } = renderHook(() => useChat({ transport }));

        await act(async () => { result.current.setInput('primeira mensagem'); });
        let first;
        let duplicateClick;
        await act(async () => {
            first = result.current.handleSend();
            duplicateClick = result.current.handleSend();
        });

        const optimistic = result.current.messages.find(message => message.role === 'user');
        expect(optimistic).toMatchObject({
            localId: 'local:request-one:user',
            serverId: null,
            requestId: 'request-one',
            content: 'primeira mensagem',
            deliveryStatus: 'pending',
        });
        expect(transport.sendMessage).toHaveBeenCalledTimes(1);
        expect(uuid).toHaveBeenCalledTimes(1);

        await act(async () => {
            resolveFirst(response('primeira resposta', 'assistant-1'));
            await Promise.all([first, duplicateClick]);
        });

        await act(async () => { result.current.setInput('segunda mensagem'); });
        await act(async () => { await result.current.handleSend(); });

        expect(uuid).toHaveBeenCalledTimes(2);
        expect(transport.sendMessage.mock.calls.map(([, , requestId]) => requestId))
            .toEqual(['request-one', 'request-two']);
    });

    it('retries a timed out intent with the same id and exact content, then reconciles replay once', async () => {
        const transport = makeDesktopTransport({
            sendMessage: vi.fn()
                .mockRejectedValueOnce(new ChatError('timeout', 'A requisição excedeu o tempo limite.'))
                .mockResolvedValueOnce(response('resposta persistida', 'assistant-persisted', true)),
        });
        const { result } = renderHook(() => useChat({ transport }));

        await setAndSend(result, ' mensagem original ');
        const userMessage = result.current.messages.find(message => message.role === 'user');
        expect(userMessage).toMatchObject({
            content: 'mensagem original',
            requestId: 'request-one',
            deliveryStatus: 'unknown',
            retryable: true,
        });

        await act(async () => {
            await result.current.retryMessage(userMessage.localId);
        });

        expect(transport.sendMessage).toHaveBeenCalledTimes(2);
        expect(transport.sendMessage.mock.calls.map(([content, , requestId]) => [content, requestId]))
            .toEqual([
                ['mensagem original', 'request-one'],
                ['mensagem original', 'request-one'],
            ]);
        expect(uuid).toHaveBeenCalledTimes(1);
        expect(result.current.messages.filter(message => message.role === 'user')).toHaveLength(1);
        expect(result.current.messages.filter(message => message.role === 'assistant')).toHaveLength(1);
        expect(result.current.messages.find(message => message.role === 'user'))
            .toMatchObject({ deliveryStatus: 'sent', retryable: false });
        expect(result.current.messages.find(message => message.role === 'assistant'))
            .toMatchObject({ serverId: 'assistant-persisted', content: 'resposta persistida' });
    });

    it('does not start a retry automatically after a request conflict', async () => {
        const transport = makeDesktopTransport({
            sendMessage: vi.fn()
                .mockRejectedValueOnce(
                    new ChatError('request_conflict', 'Este envio não pôde ser reconciliado.'),
                )
                .mockResolvedValueOnce(response('resposta após reconciliação', 'assistant-conflict')),
        });
        const { result } = renderHook(() => useChat({ transport }));

        await setAndSend(result, 'mensagem em disputa');

        expect(transport.sendMessage).toHaveBeenCalledTimes(1);
        expect(result.current.messages.find(message => message.role === 'user'))
            .toMatchObject({
                requestId: 'request-one',
                deliveryStatus: 'unknown',
                retryable: true,
            });
        expect(uuid).toHaveBeenCalledTimes(1);

        const userMessage = result.current.messages.find(message => message.role === 'user');
        await act(async () => {
            await result.current.retryMessage(userMessage.localId);
        });

        expect(transport.sendMessage).toHaveBeenCalledTimes(2);
        expect(transport.sendMessage.mock.calls.map(([content, , requestId]) => [content, requestId]))
            .toEqual([
                ['mensagem em disputa', 'request-one'],
                ['mensagem em disputa', 'request-one'],
            ]);
        expect(uuid).toHaveBeenCalledTimes(1);
    });

    it('does not expose bridge details in desktop message state', async () => {
        const transport = makeDesktopTransport({
            sendMessage: vi.fn().mockRejectedValue(
                new Error('Traceback from /tmp/private/database.sqlite'),
            ),
        });
        const { result } = renderHook(() => useChat({ transport }));

        await setAndSend(result, 'mensagem');

        const message = result.current.messages.find(item => item.role === 'user');
        expect(message.errorMessage).not.toMatch(/Traceback|\/tmp|database\.sqlite/);
        expect(message.deliveryStatus).toBe('unknown');
    });

    it('ignores a response arriving after the screen was cleared', async () => {
        let resolveSend;
        const transport = makeDesktopTransport({
            sendMessage: vi.fn(() => new Promise(resolve => { resolveSend = resolve; })),
        });
        const { result } = renderHook(() => useChat({ transport }));

        await act(async () => { result.current.setInput('não ressuscitar'); });
        let send;
        await act(async () => { send = result.current.handleSend(); });
        expect(result.current.messages).toHaveLength(1);

        await act(async () => { result.current.clearScreen(); });
        await act(async () => {
            resolveSend(response('resposta atrasada', 'late-assistant'));
            await send;
        });

        expect(result.current.messages).toEqual([]);
        expect(result.current.emotionState).toBeNull();
        expect(result.current.isLoading).toBe(false);
    });

    it('does not update the unmounted hook when the bridge settles later', async () => {
        let resolveSend;
        const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
        const transport = makeDesktopTransport({
            sendMessage: vi.fn(() => new Promise(resolve => { resolveSend = resolve; })),
        });
        const { result, unmount } = renderHook(() => useChat({ transport }));

        await act(async () => { result.current.setInput('resposta depois do unmount'); });
        let send;
        await act(async () => { send = result.current.handleSend(); });
        unmount();
        await act(async () => {
            resolveSend(response('resposta tardia', 'late-assistant'));
            await send;
        });

        expect(consoleError).not.toHaveBeenCalled();
        consoleError.mockRestore();
    });

    it('loads only unique authoritative desktop history ids in backend order', async () => {
        const history = [
            { id: 21, role: 'user', content: 'primeiro', created_at: 1 },
            { id: 22, role: 'assistant', content: 'resposta', created_at: 2 },
            { id: 21, role: 'user', content: 'duplicado', created_at: 1 },
        ];
        const transport = makeDesktopTransport({ history });
        const { result } = renderHook(() => useChat({ transport }));

        await waitFor(() => expect(result.current.messages).toHaveLength(2));

        expect(result.current.messages.map(message => message.serverId)).toEqual([21, 22]);
        expect(result.current.messages.map(message => message.id)).toEqual([21, 22]);
        expect(result.current.messages.every(message => message.deliveryStatus === 'sent')).toBe(true);
        expect(result.current.messages[0].content).toBe('primeiro');
        expect(result.current.messages[0].localId).toBe('server:number:21');
    });

    it('does not restore a history result that arrives after the screen was cleared', async () => {
        let resolveHistory;
        const transport = makeDesktopTransport();
        transport.fetchHistory = vi.fn(() => new Promise(resolve => { resolveHistory = resolve; }));
        const { result } = renderHook(() => useChat({ transport }));

        await waitFor(() => expect(transport.fetchHistory).toHaveBeenCalledTimes(1));
        await act(async () => { result.current.clearScreen(); });
        await act(async () => {
            resolveHistory({
                data: [{ id: 31, role: 'user', content: 'removida pela privacidade' }],
            });
        });

        expect(result.current.messages).toEqual([]);
    });
});
