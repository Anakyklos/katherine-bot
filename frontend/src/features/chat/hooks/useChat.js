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
 * A monotonically increasing request token prevents ownership races:
 * a stale `finally` block cannot clear the controller/timer of a newer
 * request or change `isLoading` of a request that already completed.
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
    // Synchronous in-flight guard: prevents double submission before rerender.
    // isLoading depends on rerender, but the ref is synchronous, so a second
    // handleSend() call in the same microtask is rejected immediately.
    const inFlightRef = useRef(false);

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

    // Cleanup abort controller and timer on unmount
    useEffect(() => {
        return () => {
            cleanupRequest();
        };
    }, [cleanupRequest]);

    // Auto-spin fetchHistory (EFH)
    useEffect(() => {
        const fetchHistory = async () => {
            try {
                // api.get uses the interceptor to add the Bearer token automatically
                const response = await api.get('/history');
                if (Array.isArray(response.data)) {
                    setMessages(response.data);
                }
            } catch (error) {
                // History fetch failure is not critical — log sanitised
                if (typeof import.meta !== 'undefined' && import.meta.env?.MODE !== 'test') {
                    console.warn('Failed to fetch history');
                }
            }
        };

        fetchHistory();
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

            // Guard: only the owning request may update state
            if (token !== requestTokenRef.current) return;

            // Clear timer on success
            clearTimeout(timerId);
            timerIdRef.current = null;

            const botMessage = { role: 'assistant', content: data.response };
            setMessages(prev => [...prev, botMessage]);

            // Always validate emotion_state: clear panel on invalid or missing contract
            const validated = validateEmotionState(data.emotion_state);
            setEmotionState(validated);
        } catch (error) {
            // Guard: only the owning request may show error state
            if (token !== requestTokenRef.current) return;

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
            // Guard: only the owning request may clear refs, inFlight, and loading state
            if (token === requestTokenRef.current) {
                setIsLoading(false);
                inFlightRef.current = false;
                abortControllerRef.current = null;
                timerIdRef.current = null;
                // Focus back on input
                setTimeout(() => inputRef.current?.focus(), 100);
            }
        }
    }, [input, isLoading, cleanupRequest]);

    const clearHistory = useCallback(() => {
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
