/**
 * Behavioral tests for ``useChat`` hook — issue #267.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChat } from '../src/features/chat/hooks/useChat';

const mockSendMessage = vi.fn();

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
        get: vi.fn().mockResolvedValue({ data: [] }),
        post: vi.fn(),
    },
}));

vi.mock('../src/lib/supabaseClient', () => ({
    supabase: {
        auth: {
            getSession: vi.fn().mockResolvedValue({ data: { session: null } }),
        },
    },
}));

function validEmotionResponse(text) {
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

describe('useChat', () => {
    beforeEach(() => {
        mockSendMessage.mockReset();
    });

    it('sends only one request when handleSend is called twice in same tick', async () => {
        let resolveSend;
        const sendP = new Promise(r => { resolveSend = r; });
        mockSendMessage.mockReturnValue(sendP);

        const { result } = renderHook(() => useChat());

        act(() => { result.current.setInput('Hello'); });
        await waitFor(() => expect(result.current.input).toBe('Hello'));

        const first = result.current.handleSend();
        const second = result.current.handleSend();

        await act(async () => {
            resolveSend(validEmotionResponse('Bot reply'));
            await Promise.all([first, second]);
        });

        expect(mockSendMessage).toHaveBeenCalledTimes(1);
        expect(result.current.messages.filter(m => m.role === 'user').length).toBe(1);
    });

    it('prevents stale finally from clearing state of active request', async () => {
        let resolve1;
        const p1 = new Promise(r => { resolve1 = r; });
        mockSendMessage.mockReturnValueOnce(p1);

        const { result } = renderHook(() => useChat());

        act(() => { result.current.setInput('First'); });
        await waitFor(() => expect(result.current.input).toBe('First'));

        const firstReq = result.current.handleSend();
        resolve1(validEmotionResponse('stale'));
        await act(async () => { await firstReq; });

        expect(result.current.isLoading).toBe(false);
        expect(result.current.messages.filter(m => m.role === 'assistant').length).toBe(1);
    });

    it('aborts request and shows error when timeout fires', async () => {
        let timeoutCb = null;
        const originalSetTimeout = globalThis.setTimeout;
        // Only intercept the 50s timeout from the hook, not React's internal timers
        vi.spyOn(globalThis, 'setTimeout').mockImplementation((cb, ms) => {
            if (ms >= 49000) {
                timeoutCb = cb;
                return 12345;
            }
            return originalSetTimeout(cb, ms);
        });
        vi.spyOn(globalThis, 'clearTimeout');
        const abortSpy = vi.spyOn(AbortController.prototype, 'abort');

        mockSendMessage.mockImplementation((_msg, { signal }) => {
            return new Promise((_, reject) => {
                if (signal.aborted) {
                    reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' }));
                    return;
                }
                signal.addEventListener('abort', () => {
                    reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' }));
                }, { once: true });
            });
        });

        const { result } = renderHook(() => useChat());

        act(() => { result.current.setInput('Hello'); });
        await waitFor(() => expect(result.current.input).toBe('Hello'));

        // Start request
        result.current.handleSend();

        // Wait for isLoading to be true
        await waitFor(() => expect(result.current.isLoading).toBe(true));

        // Fire the captured timeout callback
        act(() => { timeoutCb(); });

        // Wait for the async catch/finally to update state
        await waitFor(() => expect(result.current.isLoading).toBe(false));

        expect(abortSpy).toHaveBeenCalled();
        expect(result.current.messages.filter(m => m.role === 'system').length).toBeGreaterThanOrEqual(1);

        vi.mocked(globalThis.setTimeout).mockRestore();
        vi.mocked(globalThis.clearTimeout).mockRestore();
        abortSpy.mockRestore();
    });

    it('cleans up abort controller and timer on unmount', async () => {
        const abortSpy = vi.spyOn(AbortController.prototype, 'abort');
        mockSendMessage.mockReturnValue(new Promise(() => {})); // never resolves

        const { result, unmount } = renderHook(() => useChat());

        act(() => { result.current.setInput('Hello'); });
        await waitFor(() => expect(result.current.input).toBe('Hello'));

        // Start request
        result.current.handleSend();

        await waitFor(() => expect(result.current.isLoading).toBe(true));

        unmount();
        expect(abortSpy).toHaveBeenCalled();
    });
});
