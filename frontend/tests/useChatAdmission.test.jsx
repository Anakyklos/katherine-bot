import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockSendMessage = vi.hoisted(() => vi.fn());
const mockApiGet = vi.hoisted(() => vi.fn().mockResolvedValue({ data: [] }));

vi.mock('../src/features/chat/services/chatService', () => ({
    sendMessage: (...args) => mockSendMessage(...args),
    ChatError: class ChatError extends Error {
        constructor(type, message) {
            super(message);
            this.name = 'ChatError';
            this.type = type;
        }
    },
}));

vi.mock('../src/shared/services/apiClient', () => ({
    default: {
        get: (...args) => mockApiGet(...args),
        post: vi.fn(),
    },
}));

import { useChat } from '../src/features/chat/hooks/useChat';
import { SYSTEM_MESSAGES } from '../src/features/chat/constants';

const UUID_A = '550e8400-e29b-41d4-a716-446655440000';
const UUID_B = '550e8400-e29b-41d4-a716-446655440001';

function response(text = 'ok') {
    return {
        response: text,
        emotion_state: {
            schema_version: 1,
            pad: { pleasure: 0, arousal: 0, dominance: 0 },
            dominant_emotions: [],
            mood_label: 'NEUTRA',
            timestamp: 1000,
        },
    };
}

describe('useChat admission identity', () => {
    let randomUUID;

    beforeEach(() => {
        mockSendMessage.mockReset();
        mockApiGet.mockReset();
        mockApiGet.mockResolvedValue({ data: [] });
        randomUUID = vi.fn(() => UUID_A);
        vi.stubGlobal('crypto', { randomUUID });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
        vi.useRealTimers();
    });

    it('generates exactly one UUID for one accepted logical send', async () => {
        let resolveSend;
        mockSendMessage.mockReturnValue(new Promise(resolve => { resolveSend = resolve; }));
        const { result } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('hello'); });
        await waitFor(() => expect(result.current.input).toBe('hello'));

        let first;
        await act(async () => {
            first = result.current.handleSend();
            result.current.handleSend();
        });

        expect(randomUUID).toHaveBeenCalledTimes(1);
        expect(mockSendMessage).toHaveBeenCalledTimes(1);
        expect(mockSendMessage.mock.calls[0][0]).toBe('hello');
        expect(mockSendMessage.mock.calls[0][2]).toBe(UUID_A);

        await act(async () => {
            resolveSend(response());
            await first;
        });
    });

    it('uses a new UUID for a new manual send', async () => {
        randomUUID.mockReturnValueOnce(UUID_A).mockReturnValueOnce(UUID_B);
        mockSendMessage.mockResolvedValue(response());
        const { result } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('first'); });
        await act(async () => { await result.current.handleSend(); });
        await waitFor(() => expect(result.current.isLoading).toBe(false));

        await act(async () => { result.current.setInput('second'); });
        await act(async () => { await result.current.handleSend(); });

        expect(randomUUID).toHaveBeenCalledTimes(2);
        expect(mockSendMessage.mock.calls[0][2]).toBe(UUID_A);
        expect(mockSendMessage.mock.calls[1][2]).toBe(UUID_B);
    });

    it('does not call the API when Web Crypto randomUUID is unavailable', async () => {
        vi.stubGlobal('crypto', {});
        const { result } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('hello'); });
        await act(async () => { await result.current.handleSend(); });

        expect(mockSendMessage).not.toHaveBeenCalled();
        expect(result.current.messages).toEqual([
            {
                role: 'system',
                content: SYSTEM_MESSAGES.REQUEST_ID_UNAVAILABLE,
            },
        ]);
        expect(result.current.isLoading).toBe(false);
        expect(result.current.input).toBe('hello');
    });

    it('does not call the API when Web Crypto randomUUID throws', async () => {
        const failingRandomUUID = vi.fn(() => {
            throw new Error('randomUUID failure');
        });
        vi.stubGlobal('crypto', { randomUUID: failingRandomUUID });
        const { result } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('hello'); });
        await waitFor(() => expect(result.current.input).toBe('hello'));
        await act(async () => { await result.current.handleSend(); });

        expect(failingRandomUUID).toHaveBeenCalledTimes(1);
        expect(mockSendMessage).not.toHaveBeenCalled();
        expect(result.current.messages).toEqual([
            {
                role: 'system',
                content: SYSTEM_MESSAGES.REQUEST_ID_UNAVAILABLE,
            },
        ]);
        expect(result.current.isLoading).toBe(false);
        expect(result.current.input).toBe('hello');
    });

    it('does not let late history overwrite a local Web Crypto failure', async () => {
        let resolveHistory;
        mockApiGet.mockReturnValue(new Promise(resolve => { resolveHistory = resolve; }));
        vi.stubGlobal('crypto', {});
        const { result } = renderHook(() => useChat());

        await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));
        await act(async () => { result.current.setInput('hello'); });
        await act(async () => { await result.current.handleSend(); });

        await act(async () => {
            resolveHistory({ data: [{ role: 'user', content: 'stale history' }] });
        });
        await act(async () => {});

        expect(result.current.messages).toEqual([
            {
                role: 'system',
                content: SYSTEM_MESSAGES.REQUEST_ID_UNAVAILABLE,
            },
        ]);
        expect(result.current.input).toBe('hello');
    });
});
