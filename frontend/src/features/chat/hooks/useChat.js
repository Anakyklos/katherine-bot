import { useState, useEffect, useRef, useCallback } from 'react';
import { sendMessage, ChatError } from '../services/chatService';
import api from '../../../shared/services/apiClient';
import { SYSTEM_MESSAGES } from '../constants';
import { validateEmotionState } from '../../../shared/utils/formatters';

/**
 * Hook for managing chat state with AbortController-based timeout.
 *
 * Each accepted logical send creates exactly one Web Crypto UUID and one
 * request. Single-flight, ownership, history, timeout, and unmount guards are
 * preserved.
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
    const inFlightRef = useRef(false);
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

    useEffect(() => {
        mountedRef.current = true;

        return () => {
            mountedRef.current = false;
            requestTokenRef.current += 1;
            inFlightRef.current = false;
            cleanupRequest();
            cleanupFocusTimer();
        };
    }, [cleanupRequest, cleanupFocusTimer]);

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
                if (!active || controller.signal.aborted) {
                    return;
                }

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

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, isLoading]);

    const handleSend = useCallback(async () => {
        if (!input.trim() || isLoading || inFlightRef.current) return;

        const userMessageText = input.trim();
        const randomUUID = globalThis.crypto?.randomUUID;
        if (typeof randomUUID !== 'function') {
            localMessagesVersionRef.current += 1;
            setMessages(prev => [...prev, {
                role: 'system',
                content: SYSTEM_MESSAGES.REQUEST_ID_UNAVAILABLE,
            }]);
            return;
        }

        let requestId;
        try {
            requestId = randomUUID.call(globalThis.crypto);
        } catch {
            localMessagesVersionRef.current += 1;
            setMessages(prev => [...prev, {
                role: 'system',
                content: SYSTEM_MESSAGES.REQUEST_ID_UNAVAILABLE,
            }]);
            return;
        }

        const newUserMessage = { role: 'user', content: userMessageText };

        inFlightRef.current = true;
        localMessagesVersionRef.current += 1;

        setMessages(prev => [...prev, newUserMessage]);
        setInput('');
        setIsLoading(true);

        cleanupRequest();

        const token = ++requestTokenRef.current;
        const controller = new AbortController();
        abortControllerRef.current = controller;

        const timeoutMs = 50000;
        const timerId = setTimeout(() => {
            controller.abort();
        }, timeoutMs);
        timerIdRef.current = timerId;

        try {
            const data = await sendMessage(userMessageText, {
                signal: controller.signal,
                timeout: timeoutMs,
            }, requestId);

            if (!mountedRef.current || token !== requestTokenRef.current) return;

            clearTimeout(timerId);
            timerIdRef.current = null;

            const botMessage = { role: 'assistant', content: data.response };
            setMessages(prev => [...prev, botMessage]);

            const validated = validateEmotionState(data.emotion_state);
            setEmotionState(validated);
        } catch (error) {
            if (!mountedRef.current || token !== requestTokenRef.current) return;

            clearTimeout(timerId);
            timerIdRef.current = null;

            if (error instanceof ChatError) {
                const errorMessage = {
                    role: 'system',
                    content: error.message,
                };
                setMessages(prev => [...prev, errorMessage]);
            } else {
                const errorMessage = {
                    role: 'system',
                    content: SYSTEM_MESSAGES.ERROR_SENDING,
                };
                setMessages(prev => [...prev, errorMessage]);
            }
        } finally {
            if (mountedRef.current && token === requestTokenRef.current) {
                setIsLoading(false);
                inFlightRef.current = false;
                abortControllerRef.current = null;
                timerIdRef.current = null;
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
