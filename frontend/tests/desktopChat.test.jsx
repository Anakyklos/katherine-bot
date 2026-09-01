/**
 * Behavioral tests for useChat with a desktop (bridge) transport (#336,
 * T009/T014).
 *
 * The hook receives its transport injected; with the desktop transport
 * the history load and sends flow through the pywebview bridge instead
 * of Axios. All invariants are preserved: single-flight, loading state,
 * error message surface, history load, timeout guard clearing on
 * resolve.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChat } from '../src/features/chat/hooks/useChat';

// Supabase client must stay inert in the desktop test environment
// (the module throws when env vars are missing — the desktop build has
// none, so the mock mirrors the null-client path).
vi.mock('../src/lib/supabaseClient', () => ({
    supabase: null,
}));

vi.mock('../src/shared/services/apiClient', () => ({
    default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
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

function makeDesktopTransport() {
    const calls = { history: [], send: [], privacy: [] };
    const transport = {
        mode: 'desktop',
        fetchHistory: vi.fn(async ({ signal } = {}) => {
            calls.history.push(signal);
            return { data: [{ role: 'user', content: 'old msg' }] };
        }),
        sendMessage: vi.fn(async (message, options, requestId) => {
            calls.send.push([message, requestId]);
            return validEmotionResponse('local reply');
        }),
        runPrivacyOp: vi.fn(async (op) => {
            calls.privacy.push(op);
            return { status: 'applied' };
        }),
        getRuntimeState: vi.fn(async () => ({
            ok: true, storage: true, provider_configured: true, revision: 0,
        })),
    };
    return { transport, calls };
}

describe('useChat with desktop transport', () => {
    beforeEach(() => {
        vi.useRealTimers();
    });

    afterEach(() => {
        vi.useRealTimers();
    });

    it('loads history through the injected transport on mount', async () => {
        const { transport } = makeDesktopTransport();
        const { result } = renderHook(() => useChat({ transport }));

        await waitFor(() => expect(transport.fetchHistory).toHaveBeenCalledTimes(1));
        await waitFor(() =>
            expect(result.current.messages).toEqual([{ role: 'user', content: 'old msg' }]),
        );
    });

    it('sends through the transport with a fresh requestId', async () => {
        const { transport, calls } = makeDesktopTransport();
        const { result } = renderHook(() => useChat({ transport }));

        await act(async () => { result.current.setInput('olá'); });
        await waitFor(() => expect(result.current.input).toBe('olá'));
        await act(async () => { result.current.handleSend(); });

        await waitFor(() => expect(result.current.isLoading).toBe(false));
        await waitFor(() => {
            const roles = result.current.messages.map(m => m.role);
            // one history row + the sent user message + the reply
            expect(roles).toEqual(['user', 'user', 'assistant']);
        });
        expect(calls.send.length).toBe(1);
        expect(calls.send[0][0]).toBe('olá');
        // request id is a non-empty string
        expect(typeof calls.send[0][1]).toBe('string');
        expect(calls.send[0][1].length).toBeGreaterThan(0);
    });

    it('preserves single-flight on the desktop branch', async () => {
        let resolveSend;
        const pending = new Promise(r => { resolveSend = r; });
        const { transport } = makeDesktopTransport();
        transport.sendMessage.mockReturnValueOnce(pending);

        const { result } = renderHook(() => useChat({ transport }));
        await act(async () => { result.current.setInput('msg'); });
        await waitFor(() => expect(result.current.input).toBe('msg'));

        let p1, p2;
        await act(async () => {
            p1 = result.current.handleSend();
            p2 = result.current.handleSend();
        });

        await act(async () => {
            resolveSend(validEmotionResponse('r'));
            await Promise.all([p1, p2]);
        });

        expect(transport.sendMessage).toHaveBeenCalledTimes(1);
        // history row + the single accepted send's user message
        expect(result.current.messages.filter(m => m.role === 'user').length).toBe(2);
    });

    it('surfaces a ChatError message from the bridge as a system message', async () => {
        const { transport } = makeDesktopTransport();
        const { ChatError } = await import('../src/features/chat/services/chatService');
        const error = new ChatError(
            'configuration',
            'O provedor remoto não está configurado neste ambiente.',
        );
        transport.sendMessage.mockRejectedValueOnce(error);

        const { result } = renderHook(() => useChat({ transport }));
        await act(async () => { result.current.setInput('oi'); });
        await waitFor(() => expect(result.current.input).toBe('oi'));
        await act(async () => { result.current.handleSend(); });

        await waitFor(() => expect(result.current.isLoading).toBe(false));
        const system = result.current.messages.filter(m => m.role === 'system');
        expect(system.length).toBe(1);
        expect(system[0].content).toContain('provedor remoto');
    });

    it('clears the 50s guard timer on resolve (desktop)', async () => {
        vi.useFakeTimers();
        let resolveSend;
        const pending = new Promise(r => { resolveSend = r; });
        const { transport } = makeDesktopTransport();
        transport.sendMessage.mockReturnValueOnce(pending);

        const { result } = renderHook(() => useChat({ transport }));
        await act(async () => { result.current.setInput('x'); });

        let p;
        await act(async () => { p = result.current.handleSend(); });
        expect(result.current.isLoading).toBe(true);

        await act(async () => {
            resolveSend(validEmotionResponse('r'));
            await p;
        });

        // Advance past the 50s guard: nothing must happen (the timer
        // was cleared on resolve; no phantom timeout error appears).
        await act(async () => { vi.advanceTimersByTime(60000); });
        expect(result.current.isLoading).toBe(false);
        expect(result.current.messages.filter(m => m.role === 'system').length).toBe(0);
        const bot = result.current.messages.filter(m => m.role === 'assistant');
        expect(bot.length).toBe(1);
    });
});
