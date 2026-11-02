import { useState, useEffect, useRef, useCallback } from 'react';
import { ChatError } from '../services/chatError.js';
import { createDefaultTransport } from '../services/chatTransport';
import { SYSTEM_MESSAGES } from '../constants';
import { validateEmotionState } from '../../../shared/utils/formatters';

const REQUEST_TIMEOUT_MS = 50_000;
const NON_RETRYABLE_DESKTOP_ERRORS = new Set([
    'configuration',
    'request_replay',
    'validation',
]);

function isPersistedId(value) {
    return (
        (typeof value === 'string' && value.length > 0)
        || (typeof value === 'number' && Number.isSafeInteger(value))
    );
}

function persistedLocalId(id) {
    return `server:${typeof id}:${String(id)}`;
}

function normalizeHistory(messages, { desktop, nextLocalId }) {
    const seen = new Set();
    const normalized = [];

    for (const message of messages) {
        const serverId = isPersistedId(message?.id) ? message.id : null;
        if (desktop && serverId !== null) {
            const identity = `${typeof serverId}:${String(serverId)}`;
            if (seen.has(identity)) continue;
            seen.add(identity);
        }

        normalized.push({
            ...message,
            localId: serverId === null ? nextLocalId('history') : persistedLocalId(serverId),
            serverId,
            ...(desktop ? { deliveryStatus: 'sent' } : {}),
        });
    }

    return normalized;
}

function upsertMessage(messages, candidate) {
    const existingIndex = messages.findIndex(message => (
        message.localId === candidate.localId
        || (
            candidate.serverId !== null
            && candidate.serverId !== undefined
            && message.serverId === candidate.serverId
        )
    ));

    if (existingIndex === -1) return [...messages, candidate];
    return messages.map((message, index) => (
        index === existingIndex ? { ...message, ...candidate } : message
    ));
}

/**
 * Hook for managing chat state over the mode-aware transport.
 *
 * Desktop sends keep one request id and one local message identity for the
 * full logical intent. A failed or uncertain attempt stays attached to that
 * message; explicit retry reuses its original content and request id. The
 * web branch keeps its previous error presentation and transport behavior.
 */
export const useChat = ({ transport: injectedTransport } = {}) => {
    const transportRef = useRef(null);
    if (transportRef.current === null) {
        transportRef.current = injectedTransport ?? createDefaultTransport();
    }
    const transport = transportRef.current;
    const isDesktop = transport.mode === 'desktop';
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
    const localMessageSequenceRef = useRef(0);
    const screenGenerationRef = useRef(0);
    const intentGenerationRef = useRef(new Map());

    const nextLocalId = useCallback((kind) => {
        localMessageSequenceRef.current += 1;
        return `local:${kind}:${localMessageSequenceRef.current}`;
    }, []);

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
                const response = await transport.fetchHistory({
                    signal: controller.signal,
                });

                if (
                    !active
                    || !mountedRef.current
                    || versionAtStart !== localMessagesVersionRef.current
                ) {
                    return;
                }

                if (Array.isArray(response.data)) {
                    setMessages(normalizeHistory(response.data, {
                        desktop: isDesktop,
                        nextLocalId,
                    }));
                }
            } catch {
                if (!active || controller.signal.aborted) {
                    return;
                }

                if (
                    typeof import.meta !== 'undefined'
                    && import.meta.env?.MODE !== 'test'
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
    }, [transport, isDesktop, nextLocalId]);

    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, isLoading]);

    const sendIntent = useCallback(async (intent) => {
        if (!mountedRef.current || inFlightRef.current) return;

        const generation = intentGenerationRef.current.get(intent.localId);
        if (generation === undefined || generation !== screenGenerationRef.current) return;

        inFlightRef.current = true;
        localMessagesVersionRef.current += 1;
        setMessages(prev => (
            intent.deliveryStatus === undefined
                ? upsertMessage(prev, intent)
                : upsertMessage(prev, { ...intent, deliveryStatus: 'pending', retryable: false, errorMessage: null })
        ));
        setIsLoading(true);

        cleanupRequest();

        const token = ++requestTokenRef.current;
        const controller = new AbortController();
        abortControllerRef.current = controller;

        const timerId = setTimeout(() => {
            controller.abort();
        }, REQUEST_TIMEOUT_MS);
        timerIdRef.current = timerId;

        const ownsVisibleIntent = () => (
            mountedRef.current
            && token === requestTokenRef.current
            && generation === screenGenerationRef.current
        );

        try {
            const data = await transport.sendMessage(
                intent.content,
                {
                    signal: controller.signal,
                    timeout: REQUEST_TIMEOUT_MS,
                },
                intent.requestId,
            );

            if (!ownsVisibleIntent()) return;

            clearTimeout(timerId);
            timerIdRef.current = null;

            const assistantMessage = {
                localId: `local:${intent.requestId}:assistant`,
                serverId: isPersistedId(data.message_id) ? data.message_id : null,
                requestId: intent.requestId,
                role: 'assistant',
                content: data.response,
                ...(isDesktop ? { deliveryStatus: 'sent' } : {}),
            };

            setMessages(prev => {
                const userUpdated = isDesktop
                    ? prev.map(message => (
                        message.localId === intent.localId
                            ? {
                                ...message,
                                deliveryStatus: 'sent',
                                retryable: false,
                                errorMessage: null,
                            }
                            : message
                    ))
                    : prev;
                return upsertMessage(userUpdated, assistantMessage);
            });
            intentGenerationRef.current.delete(intent.localId);

            const validated = validateEmotionState(data.emotion_state);
            setEmotionState(validated);
        } catch (error) {
            if (!ownsVisibleIntent()) return;

            clearTimeout(timerId);
            timerIdRef.current = null;

            if (isDesktop) {
                const errorType = error instanceof ChatError ? error.type : 'unknown';
                const retryable = !NON_RETRYABLE_DESKTOP_ERRORS.has(errorType);
                const deliveryStatus = (
                    errorType === 'timeout'
                    || errorType === 'unknown'
                    || errorType === 'service_unavailable'
                    || errorType === 'request_conflict'
                ) ? 'unknown' : 'failed';
                const errorMessage = error instanceof ChatError
                    ? error.message
                    : SYSTEM_MESSAGES.ERROR_SENDING;

                setMessages(prev => prev.map(message => (
                    message.localId === intent.localId
                        ? { ...message, deliveryStatus, retryable, errorMessage }
                        : message
                )));
                if (!retryable) intentGenerationRef.current.delete(intent.localId);
            } else {
                const systemMessage = {
                    localId: nextLocalId('system'),
                    serverId: null,
                    role: 'system',
                    content: error instanceof ChatError
                        ? error.message
                        : SYSTEM_MESSAGES.ERROR_SENDING,
                };
                setMessages(prev => [...prev, systemMessage]);
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
    }, [
        cleanupFocusTimer,
        cleanupRequest,
        isDesktop,
        nextLocalId,
        transport,
    ]);

    const handleSend = useCallback(() => {
        if (!input.trim() || isLoading || inFlightRef.current) return;

        const requestIdFactory = globalThis.crypto?.randomUUID;
        if (typeof requestIdFactory !== 'function') {
            localMessagesVersionRef.current += 1;
            const systemMessage = {
                localId: nextLocalId('system'),
                serverId: null,
                role: 'system',
                content: SYSTEM_MESSAGES.REQUEST_ID_UNAVAILABLE,
            };
            setMessages(prev => [...prev, systemMessage]);
            return;
        }

        let requestId;
        try {
            requestId = requestIdFactory.call(globalThis.crypto);
        } catch {
            localMessagesVersionRef.current += 1;
            const systemMessage = {
                localId: nextLocalId('system'),
                serverId: null,
                role: 'system',
                content: SYSTEM_MESSAGES.REQUEST_ID_UNAVAILABLE,
            };
            setMessages(prev => [...prev, systemMessage]);
            return;
        }

        const content = input.trim();
        const localId = `local:${requestId}:user`;
        const intent = {
            localId,
            serverId: null,
            requestId,
            role: 'user',
            content,
            ...(isDesktop ? {
                deliveryStatus: 'pending',
                retryable: false,
                errorMessage: null,
            } : {}),
        };
        intentGenerationRef.current.set(localId, screenGenerationRef.current);
        setInput('');
        return sendIntent(intent);
    }, [input, isLoading, isDesktop, nextLocalId, sendIntent]);

    const retryMessage = useCallback((localId) => {
        if (!isDesktop || isLoading || inFlightRef.current) return;
        const intent = messages.find(message => (
            message.localId === localId
            && message.role === 'user'
            && (message.deliveryStatus === 'failed' || message.deliveryStatus === 'unknown')
            && message.retryable === true
        ));
        if (!intent) return;
        return sendIntent(intent);
    }, [isDesktop, isLoading, messages, sendIntent]);

    /** Clears the local chat screen only; persisted history remains untouched. */
    const clearScreen = useCallback(() => {
        localMessagesVersionRef.current += 1;
        screenGenerationRef.current += 1;
        intentGenerationRef.current.clear();
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
        retryMessage,
        clearScreen,
        transport,
    };
};
