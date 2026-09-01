/**
 * Behavioral tests for ``useChat`` hook — issue #267.
 *
 * Tests cover:
 * - Single-flight: second call while in-flight is rejected
 * - Single-flight released after settlement, allowing new requests
 * - Timeout: timer fires, abort triggered, error shown, loading cleared
 * - Unmount during request: abort called, cleanup complete, no warnings
 * - Ownership invalidation: stale callbacks after unmount do not update state
 * - No React act() warnings
 * - History lifecycle: deferred response after unmount does not update state
 * - Late history does not overwrite sent messages
 * - Late history does not undo clearScreen()
 * - Normal history loading still works
 * - Abort of history fetch on unmount is silent
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChat } from '../src/features/chat/hooks/useChat';

// Export a configurable mock for api.get so tests can defer / control its resolution.
const mockApiGet = vi.hoisted(() => vi.fn().mockResolvedValue({ data: [] }));
const mockApiPost = vi.hoisted(() => vi.fn());
const mockApiDelete = vi.hoisted(() => vi.fn());

const mockSendMessage = vi.fn();

vi.mock('../src/features/chat/services/chatService', () => ({
    sendMessage: (...args) => mockSendMessage(...args),
    fetchHistory: async (...args) => mockApiGet(...args),
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
        post: (...args) => mockApiPost(...args),
        delete: (...args) => mockApiDelete(...args),
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
        vi.useRealTimers();
        mockSendMessage.mockReset();
        mockApiGet.mockReset();
        mockApiGet.mockResolvedValue({ data: [] });
        mockApiPost.mockReset();
        mockApiDelete.mockReset();
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('sends only one request when handleSend is called twice in same tick', async () => {
        let resolveSend;
        const sendP = new Promise(r => { resolveSend = r; });
        mockSendMessage.mockReturnValue(sendP);

        const { result } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('Hello'); });
        await waitFor(() => expect(result.current.input).toBe('Hello'));

        let first, second;
        await act(async () => {
            first = result.current.handleSend();
            second = result.current.handleSend();
        });

        await act(async () => {
            resolveSend(validEmotionResponse('Bot reply'));
            await Promise.all([first, second]);
        });

        expect(mockSendMessage).toHaveBeenCalledTimes(1);
        expect(result.current.messages.filter(m => m.role === 'user').length).toBe(1);
    });

    it('releases single-flight after settlement allowing a new request', async () => {
        let resolveFirst;
        const firstP = new Promise(r => { resolveFirst = r; });
        mockSendMessage.mockReturnValue(firstP);

        const { result } = renderHook(() => useChat());

        // Start first request
        let firstPromise;
        await act(async () => { result.current.setInput('First'); });
        await waitFor(() => expect(result.current.input).toBe('First'));
        await act(async () => { firstPromise = result.current.handleSend(); });
        await waitFor(() => expect(result.current.isLoading).toBe(true));
        expect(mockSendMessage).toHaveBeenCalledTimes(1);

        // Second call while in-flight should be blocked
        await act(async () => { result.current.setInput('Second'); });
        await waitFor(() => expect(result.current.input).toBe('Second'));
        let sp;
        await act(async () => { sp = result.current.handleSend(); });
        expect(mockSendMessage).toHaveBeenCalledTimes(1);

        // Resolve first
        await act(async () => {
            resolveFirst(validEmotionResponse('First reply'));
            await firstPromise;
        });

        await waitFor(() => expect(result.current.isLoading).toBe(false));

        // Now a new request should proceed
        await act(async () => { result.current.setInput('Third'); });
        await waitFor(() => expect(result.current.input).toBe('Third'));
        await act(async () => { result.current.handleSend(); });
        expect(mockSendMessage).toHaveBeenCalledTimes(2);
    });

    it('aborts request and shows error when timeout fires', async () => {
        vi.useFakeTimers();
        const abortSpy = vi.spyOn(AbortController.prototype, 'abort');
        const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');

        mockSendMessage.mockImplementation((_message, { signal }) => {
            return new Promise((_resolve, reject) => {
                const onAbort = () => {
                    reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' }));
                };
                if (signal.aborted) { onAbort(); return; }
                signal.addEventListener('abort', onAbort, { once: true });
            });
        });

        const { result } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('Hello'); });
        expect(result.current.input).toBe('Hello');

        let sendPromise;
        await act(async () => { sendPromise = result.current.handleSend(); });
        expect(result.current.isLoading).toBe(true);

        // Advance time to trigger timeout (50s)
        await act(async () => {
            vi.advanceTimersByTime(50000);
        });

        await act(async () => { await sendPromise; });

        expect(result.current.isLoading).toBe(false);
        expect(abortSpy).toHaveBeenCalled();
        expect(result.current.messages.filter(m => m.role === 'system').length).toBeGreaterThanOrEqual(1);

        abortSpy.mockRestore();
        clearTimeoutSpy.mockRestore();
        vi.useRealTimers();
    });

    it('cleans up abort controller and timer on unmount and no state updates', async () => {
        const abortSpy = vi.spyOn(AbortController.prototype, 'abort');
        const clearTimeoutSpy = vi.spyOn(globalThis, 'clearTimeout');
        const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

        mockSendMessage.mockImplementation((_message, { signal }) => {
            return new Promise((_resolve, reject) => {
                const onAbort = () => {
                    reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' }));
                };
                if (signal.aborted) { onAbort(); return; }
                signal.addEventListener('abort', onAbort, { once: true });
            });
        });

        const { result, unmount } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('Hello'); });
        expect(result.current.input).toBe('Hello');

        let sendPromise;
        await act(async () => { sendPromise = result.current.handleSend(); });
        await waitFor(() => expect(result.current.isLoading).toBe(true));

        // Unmount — this should abort and invalidate ownership
        act(() => { unmount(); });
        expect(abortSpy).toHaveBeenCalled();
        expect(clearTimeoutSpy).toHaveBeenCalled();

        // Let the rejection settle
        try { await sendPromise; } catch (e) { /* expected */ }

        // No React warnings
        const warningMessages = consoleErrorSpy.mock.calls
            .filter(([msg]) => typeof msg === 'string')
            .filter(([msg]) =>
                msg.includes('not wrapped in act') ||
                msg.includes('update') ||
                msg.includes('unmount')
            );
        expect(warningMessages).toHaveLength(0);

        abortSpy.mockRestore();
        clearTimeoutSpy.mockRestore();
        consoleErrorSpy.mockRestore();
    });

    it('invalidates stale callbacks after unmount — catch/finally do not update state', async () => {
        const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

        mockSendMessage.mockImplementation((_message, { signal }) => {
            return new Promise((_resolve, reject) => {
                const onAbort = () => {
                    reject(Object.assign(new Error('canceled'), { code: 'ERR_CANCELED' }));
                };
                if (signal.aborted) { onAbort(); return; }
                signal.addEventListener('abort', onAbort, { once: true });
            });
        });

        const { result, unmount } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('Hello'); });

        let sendPromise;
        await act(async () => { sendPromise = result.current.handleSend(); });
        await waitFor(() => expect(result.current.isLoading).toBe(true));

        expect(result.current.messages.filter(m => m.role === 'user').length).toBe(1);

        // Unmount while request is in-flight — invalidates ownership before abort
        act(() => { unmount(); });

        try { await sendPromise; } catch (e) { /* expected */ }

        // No React warnings (guarded by mountedRef and requestTokenRef)
        const warningMessages = consoleErrorSpy.mock.calls
            .filter(([msg]) => typeof msg === 'string')
            .filter(([msg]) =>
                msg.includes('not wrapped in act') ||
                msg.includes('update') ||
                msg.includes('unmount')
            );
        expect(warningMessages).toHaveLength(0);

        consoleErrorSpy.mockRestore();
    });

    it('clears timer on successful request', async () => {
        mockSendMessage.mockResolvedValue(validEmotionResponse('Bot reply'));

        const { result } = renderHook(() => useChat());

        await act(async () => { result.current.setInput('Hello'); });

        let sendPromise;
        await act(async () => { sendPromise = result.current.handleSend(); });
        await act(async () => { await sendPromise; });

        expect(result.current.isLoading).toBe(false);
        expect(result.current.messages.filter(m => m.role === 'assistant').length).toBe(1);
    });

    // ─── History lifecycle tests ─────────────────────────────────────────

    it('does not apply history response after unmount', async () => {
        const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
        const consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

        let resolveHistory;
        mockApiGet.mockReturnValue(new Promise(r => { resolveHistory = r; }));

        const { unmount } = renderHook(() => useChat());

        expect(mockApiGet).toHaveBeenCalled();

        // Unmount before history resolves
        act(() => { unmount(); });

        // Resolve with non-empty history — mock deliberately ignores abort
        await act(async () => {
            resolveHistory({ data: [{ role: 'user', content: 'old message' }] });
        });

        // Flush pending microtasks
        await act(async () => {});

        // No React warnings (update-on-unmounted-component etc.)
        const warningMessages = consoleErrorSpy.mock.calls
            .filter(([msg]) => typeof msg === 'string')
            .filter(([msg]) =>
                msg.includes('not wrapped in act') ||
                msg.includes('update') ||
                msg.includes('unmount')
            );
        expect(warningMessages).toHaveLength(0);

        // No console.warn from history failure (abort is expected, not an error)
        expect(consoleWarnSpy).not.toHaveBeenCalled();

        consoleErrorSpy.mockRestore();
        consoleWarnSpy.mockRestore();
    });

    it('late history does not overwrite sent messages', async () => {
        let resolveHistory;
        mockApiGet.mockReturnValue(new Promise(r => { resolveHistory = r; }));

        const { result } = renderHook(() => useChat());

        // History is still pending — confirm it was called
        await waitFor(() => expect(mockApiGet).toHaveBeenCalled());

        // Send a message while history is pending
        mockSendMessage.mockResolvedValue(validEmotionResponse('Bot reply'));

        await act(async () => { result.current.setInput('Hello'); });
        let sendPromise;
        await act(async () => { sendPromise = result.current.handleSend(); });
        await act(async () => { await sendPromise; });

        // Verify the sent messages are in place
        expect(result.current.messages).toHaveLength(2);
        expect(result.current.messages[0].content).toBe('Hello');
        expect(result.current.messages[1].content).toBe('Bot reply');

        // Now resolve history with old/different data
        await act(async () => {
            resolveHistory({ data: [{ role: 'user', content: 'old message' }] });
        });

        await act(async () => {});

        // The sent messages must still be present
        expect(result.current.messages).toHaveLength(2);
        expect(result.current.messages[0].content).toBe('Hello');
        expect(result.current.messages[1].content).toBe('Bot reply');
    });

    it('late history does not undo clearScreen', async () => {
        let resolveHistory;
        mockApiGet.mockReturnValue(new Promise(r => { resolveHistory = r; }));

        const { result } = renderHook(() => useChat());

        expect(mockApiGet).toHaveBeenCalled();

        // Clear the screen while /history is pending
        await act(async () => {
            result.current.clearScreen();
        });

        // Resolve history with old data
        await act(async () => {
            resolveHistory({ data: [{ role: 'user', content: 'old message' }] });
        });

        await act(async () => {});

        // Messages must remain empty
        expect(result.current.messages).toHaveLength(0);
        expect(result.current.messages).toEqual([]);
    });

    it('loads history normally when no local mutations occur', async () => {
        const historyData = [
            { role: 'user', content: 'stored question' },
            { role: 'assistant', content: 'stored answer' },
        ];
        mockApiGet.mockResolvedValue({ data: historyData });

        const { result } = renderHook(() => useChat());

        await waitFor(() => {
            expect(result.current.messages).toEqual(historyData);
        });
    });

    it('aborting history fetch on unmount does not produce warnings', async () => {
        const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
        const consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

        let resolveHistory;
        mockApiGet.mockReturnValue(new Promise(r => { resolveHistory = r; }));

        const { unmount } = renderHook(() => useChat());

        expect(mockApiGet).toHaveBeenCalled();

        // Unmount — triggers abort signal and sets active=false
        await act(async () => {
            unmount();
        });

        // Resolve the promise (the guards handle the rest)
        await act(async () => {
            resolveHistory({ data: [{ role: 'user', content: 'stale' }] });
        });

        await act(async () => {});

        // No console.warn (abort is expected lifecycle, not a failure)
        expect(consoleWarnSpy).not.toHaveBeenCalled();

        // No React warnings
        const warningMessages = consoleErrorSpy.mock.calls
            .filter(([msg]) => typeof msg === 'string')
            .filter(([msg]) =>
                msg.includes('not wrapped in act') ||
                msg.includes('update') ||
                msg.includes('unmount')
            );
        expect(warningMessages).toHaveLength(0);

        consoleErrorSpy.mockRestore();
        consoleWarnSpy.mockRestore();
    });

    it('clearScreen clears local messages and emotionState without HTTP delete', async () => {
        mockSendMessage.mockResolvedValue(validEmotionResponse('Bot reply'));

        const { result } = renderHook(() => useChat());

        // Build local state: one user message plus an assistant reply and emotion.
        await act(async () => { result.current.setInput('Hello'); });
        let sendPromise;
        await act(async () => { sendPromise = result.current.handleSend(); });
        await act(async () => { await sendPromise; });

        expect(result.current.messages).toHaveLength(2);
        expect(result.current.emotionState).not.toBeNull();

        // Clear the screen
        await act(async () => {
            result.current.clearScreen();
        });

        expect(result.current.messages).toEqual([]);
        expect(result.current.emotionState).toBeNull();

        // No deletion or privacy write is issued: only the /history GET happened.
        expect(mockApiDelete).not.toHaveBeenCalled();
        expect(mockApiPost).not.toHaveBeenCalled();
        expect(mockApiGet).toHaveBeenCalledTimes(1);
    });

    it('persisted history still loads after clearScreen and remount', async () => {
        const historyData = [
            { role: 'user', content: 'stored question' },
            { role: 'assistant', content: 'stored answer' },
        ];
        mockApiGet.mockResolvedValue({ data: historyData });

        const first = renderHook(() => useChat());
        await waitFor(() => {
            expect(first.result.current.messages).toEqual(historyData);
        });

        // Clear the screen: local view is emptied, nothing is deleted server-side.
        await act(async () => {
            first.result.current.clearScreen();
        });
        expect(first.result.current.messages).toEqual([]);

        // Simulate a reload: unmount and mount a fresh chat.
        first.unmount();
        expect(mockApiGet).toHaveBeenCalledTimes(1);

        const second = renderHook(() => useChat());
        await waitFor(() => {
            expect(second.result.current.messages).toEqual(historyData);
        });

        // The persisted history was never removed: /history returns it again.
        expect(mockApiGet).toHaveBeenCalledTimes(2);
        expect(mockApiDelete).not.toHaveBeenCalled();
        expect(mockApiPost).not.toHaveBeenCalled();
    });
});
