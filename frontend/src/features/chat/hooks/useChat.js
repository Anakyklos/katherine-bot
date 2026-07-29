import { useState, useEffect, useRef, useCallback } from 'react';
import { sendMessage, ChatError } from '../services/chatService';
import api from '../../../shared/services/apiClient';
import { SYSTEM_MESSAGES } from '../constants';
import { validateEmotionState } from '../../../shared/utils/formatters';

/**
 * Hook for managing chat state with AbortController-based timeout.
 *
 * Each send creates a fresh AbortController. The timer is cleaned up on
 * success, error, cancellation, and unmount.
 *
 * Ownership model: single-flight — at most one active request per hook
 * instance. A second handleSend() call while one is in-flight is silently
 * rejected.
 *
 * A monotonically increasing request token invalidates ownership on
 * unmount, ensuring stale callbacks never update state.
 *
 * Lifecycle:
 * 1. mountedRef guards all state updates after await.
 * 2. requestTokenRef guards ownership against stale requests on unmount.
 * 3. inFlightRef synchronously blocks double submission before rerender.
 * 4. focusTimerRef tracks the deferred focus timer for explicit cleanup.
 */
export const useChat = () => {
    const [messages, setMessages] = useState([]);
    const [input, setInput] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const [emotionState, setEmotionState] = useState(null);
    const messagesEndRef = useRef(null);
    const inputRef = useRef(null);
    const abortControllerRef = useRef(null);
    const timerIdRef = useRef(null);
    const requestTokenRef = useRef(0);
    const mountedRef = useRef(true);
    const focusTimerRef = useRef(null);
    // Synchronous in-flight guard: prevents double submission before rerender.
    // isLoading depends on rerender, but the ref is synchronous, so a second
    // handleSend() call in the same microtask is rejected immediately.
    const inFlightRef = useRef(false);

    // Monotonically increasing version counter for local message mutations.
    // Guards the deferred /history response from overwriting messages that
    // were added or cleared after the history read started.
    const localMessagesVersionRef = useRef(0);

    const cleanupRequest = useCallback(() => {
        if (timerIdRef.current !== null) {
            clearTimeout(timerIdRef.current);
            timerIdRef.current = null;
        }
        if (abortControllerRef.current !== null) {
            abortControllerRef.current.abort();
            abortControllerRef.current = null;
        }
    }, []);

    const cleanupFocusTimer = useCallback(() => {
        if (focusTimerRef.current !== null) {
            clearTimeout(focusTimerRef.current);
            focusTimerRef.current = null;
        }
    }, []);

    // Lifecycle effect: set mountedRef on mount, teardown on unmount
    useEffect(() => {
        mountedRef.current = true;

        return () => {
            mountedRef.current = false;

            // Invalidate current request ownership before aborting, so the
            // rejection caused by abort() is treated as stale continuation.
            requestTokenRef.current += 1;
            inFlightRef.current = false;

            cleanupRequest();
            cleanupFocusTimer();
        };
    }, [cleanupRequest, cleanupFocusTimer]);

    // Auto-spin fetchHistory (EFH) with guarded lifecycle.
    // Uses a local active flag, an AbortController, and a version snapshot
    // to prevent stale /history responses from updating state after:
    //   - unmount (active flag + abort)
    //   - a local mutation (version mismatch: send or clearHistory)
    //   - the read was superseded by a newer fetch (the effect runs once)
    useEffect(() => {
        let active = true;
        const controller = new AbortController();
        const versionAtStart = localMessagesVersionRef.current;

        const fetchHistory = async () => {
            try {
                const response = await api.get('/history', {
                    signal: controller.signal,
                });

                if (
                    !active ||
                    !mountedRef.current ||
                    versionAtStart !== localMessagesVersionRef.current
                ) {
                    return;
                }

                if (Array.isArray(response.data)) {
                    setMessages(response.data);
                }
            } catch (error) {
                // Silent return on expected lifecycle termination
                if (!active || controller.signal.aborted) {
                    return;
                }

                // History fetch failure is not critical — log sanitised
                if (
                    typeof import.meta !== 'undefined' &&
                    import.meta.env?.MODE !== 'test'
                ) {
                    console.warn('Failed to fetch history');
                }
            }
        };

        fetchHistory();

        return () => {
            active = false;
            controller.abort();
        };
    }, []);

    // Auto-scroll to bottom
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, isLoading]);

    const handleSend = useCallback(async () => {
        // Synchronous guard: reject second call before any optimistic effects.
        if (!input.trim() || isLoading || inFlightRef.current) return;

        const userMessageText = input.trim();
        const newUserMessage = { role: 'user', content: userMessageText };

        // Mark in-flight synchronously BEFORE any side effects.
        inFlightRef.current = true;
        localMessagesVersionRef.current += 1;

        // Optimistic update
        setMessages(prev => [...prev, newUserMessage]);
        setInput('');
        setIsLoading(true);

        // Clear any stale controller/timer from previous request
        cleanupRequest();

        // Claim ownership of this request with a monotonically increasing token
        const token = ++requestTokenRef.current;

        // Create fresh AbortController for this request
        const controller = new AbortController();
        abortControllerRef.current = controller;

        // Create timeout timer
        const timeoutMs = 50000;
        const timerId = setTimeout(() => {
            controller.abort();
        }, timeoutMs);
        timerIdRef.current = timerId;

        try {
            const data = await sendMessage(userMessageText, {
                signal: controller.signal,
                timeout: timeoutMs,
            });

            // Guard: only mounted and owning request may update state
            if (!mountedRef.current || token !== requestTokenRef.current) return;

            // Clear timer on success
            clearTimeout(timerId);
            timerIdRef.current = null;

            const botMessage = { role: 'assistant', content: data.response };
            setMessages(prev => [...prev, botMessage]);

            // Always validate emotion_state: clear panel on invalid or missing contract
            const validated = validateEmotionState(data.emotion_state);
            setEmotionState(validated);
        } catch (error) {
            // Guard: only mounted and owning request may show error state
            if (!mountedRef.current || token !== requestTokenRef.current) return;

            // Clear timer on error/cancel
            clearTimeout(timerId);
            timerIdRef.current = null;

            if (error instanceof ChatError) {
                const errorMessage = {
                    role: 'system',
                    content: error.message,
                };
                setMessages(prev => [...prev, errorMessage]);
            } else {
                // Unknown error — use safe default
                const errorMessage = {
                    role: 'system',
                    content: SYSTEM_MESSAGES.ERROR_SENDING,
                };
                setMessages(prev => [...prev, errorMessage]);
            }
        } finally {
            // Guard: only mounted and owning request may clear refs, inFlight, and loading state
            if (mountedRef.current && token === requestTokenRef.current) {
                setIsLoading(false);
                inFlightRef.current = false;
                abortControllerRef.current = null;
                timerIdRef.current = null;
                // Focus back on input — tracked by focusTimerRef for explicit cleanup
                cleanupFocusTimer();
                focusTimerRef.current = setTimeout(() => inputRef.current?.focus(), 100);
            }
        }
    }, [input, isLoading, cleanupRequest, cleanupFocusTimer]);

    const clearHistory = useCallback(() => {
        localMessagesVersionRef.current += 1;
        setMessages([]);
        setEmotionState(null);
    }, []);

    return {
        messages,
        input,
        setInput,
        isLoading,
        emotionState,
        messagesEndRef,
        inputRef,
        handleSend,
        clearHistory
    };
};
