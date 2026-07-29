import api from '../../../shared/services/apiClient.js';

/**
 * Sanitised error classification for chat API responses.
 *
 * Never contains: raw Axios error objects, headers, tokens, config, or request data.
 */
export class ChatError extends Error {
    /**
     * @param {'timeout'|'rate_limited'|'service_unavailable'|'validation'|'unknown'} type
     * @param {string} message
     */
    constructor(type, message) {
        super(message);
        this.name = 'ChatError';
        this.type = type;
    }
}

/**
 * Classify an HTTP error code into a stable ChatError type.
 *
 * @param {number} status - HTTP status code
 * @returns {'timeout'|'rate_limited'|'service_unavailable'|'validation'|'unknown'}
 */
export function classifyHttpError(status) {
    if (status === 504 || status === 0) return 'timeout';
    if (status === 429) return 'rate_limited';
    if (status === 503) return 'service_unavailable';
    if (status === 422) return 'validation';
    return 'unknown';
}

/**
 * Create a ChatError from an Axios error, safely extracting only the status code.
 *
 * Axios errors may contain config, headers, and tokens — never log the raw object.
 *
 * @param {import('axios').AxiosError} error
 * @returns {ChatError}
 */
export function createChatError(error) {
    // Timeout / abort
    if (error.code === 'ECONNABORTED' || error.code === 'ERR_CANCELED') {
        return new ChatError('timeout', 'A requisição excedeu o tempo limite.');
    }

    // No response (network error)
    if (!error.response) {
        return new ChatError('timeout', 'Sem resposta do servidor.');
    }

    const status = error.response.status;
    const type = classifyHttpError(status);

    const messages = {
        timeout: 'A requisição excedeu o tempo limite.',
        rate_limited: 'Muitas requisições. Aguarde um momento e tente novamente.',
        service_unavailable: 'Serviço temporariamente indisponível. Tente novamente mais tarde.',
        validation: 'Dados inválidos enviados.',
        unknown: 'Erro ao falar com a Katherine. Tente novamente.',
    };

    return new ChatError(type, messages[type] || messages.unknown);
}

/**
 * Send a message to the chat API with AbortController support.
 *
 * @param {string} message - User message text
 * @param {object} [options]
 * @param {AbortSignal} [options.signal] - AbortSignal from AbortController
 * @param {number} [options.timeout=50000] - Timeout in milliseconds
 * @returns {Promise<{response: string, emotion_state: object}>}
 * @throws {ChatError}
 */
export const sendMessage = async (message, options = {}) => {
    const { signal, timeout = 50000 } = options;

    try {
        const response = await api.post('/chat', {
            message: message,
        }, {
            signal,
            timeout,
        });
        return response.data;
    } catch (error) {
        throw createChatError(error);
    }
};
