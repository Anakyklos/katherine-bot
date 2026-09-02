import api from '../../../shared/services/apiClient.js';

/**
 * Sanitised error classification for chat API responses.
 *
 * Never contains: raw Axios error objects, headers, tokens, config, request IDs,
 * or request data.
 */
import { ChatError } from './chatError.js';

// Re-exported for the existing web-mode consumers (#267 tests import it
// from this module); the class itself is dependency-free (see chatError.js).
export { ChatError };

/**
 * Classify an HTTP status and allowlisted public code into a stable ChatError type.
 */
export function classifyHttpError(status, code = null) {
    if (status === 504 || status === 0) return 'timeout';
    if (status === 409 && code === 'request_replay_unavailable') return 'request_replay';
    if (status === 409 && code === 'request_id_conflict') return 'request_conflict';
    if (status === 429) return 'rate_limited';
    if (status === 503) return 'service_unavailable';
    if (status === 422) return 'validation';
    return 'unknown';
}

function extractPublicCode(error) {
    const detail = error?.response?.data?.detail;
    if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null;
    const code = detail.code;
    if (code === 'request_replay_unavailable' || code === 'request_id_conflict') {
        return code;
    }
    return null;
}

/**
 * Create a ChatError from an Axios error while extracting only status and an
 * allowlisted public reconciliation code.
 */
export function createChatError(error) {
    if (error?.code === 'ECONNABORTED' || error?.code === 'ERR_CANCELED') {
        return new ChatError('timeout', 'A requisição excedeu o tempo limite.');
    }

    if (!error?.response) {
        return new ChatError('timeout', 'Sem resposta do servidor.');
    }

    const status = error.response.status;
    const type = classifyHttpError(status, extractPublicCode(error));

    const messages = {
        timeout: 'A requisição excedeu o tempo limite.',
        request_replay: 'Este envio já foi recebido, mas a resposta não pode ser recuperada.',
        request_conflict: 'Este envio não pôde ser reconciliado. Envie a mensagem novamente.',
        rate_limited: 'Muitas requisições. Aguarde um momento e tente novamente.',
        service_unavailable: 'Serviço temporariamente indisponível. Tente novamente mais tarde.',
        validation: 'Dados inválidos enviados.',
        unknown: 'Erro ao falar com a Katherine. Tente novamente.',
    };

    return new ChatError(type, messages[type] || messages.unknown);
}

/**
 * Fetch the persisted conversation history (web branch).
 *
 * Kept here (not in the hook) since #336 so the transport boundary can
 * delegate both branches uniformly. Returns the Axios response shape
 * `{ data: [...] }` the hook always consumed.
 *
 * @param {object} [options]
 * @param {AbortSignal} [options.signal]
 */
export const fetchHistory = async (options = {}) => {
    const { signal } = options;
    return api.get('/history', { signal });
};

/**
 * Send a message to the chat API with an explicit logical request ID.
 *
 * @param {string} message
 * @param {object} [options]
 * @param {string} requestId
 * @param {AbortSignal} [options.signal]
 * @param {number} [options.timeout=50000]
 */
export const sendMessage = async (message, options = {}, requestId) => {
    const { signal, timeout = 50000 } = options;

    if (typeof requestId !== 'string' || requestId.length === 0) {
        throw new ChatError('validation', 'Não foi possível identificar este envio.');
    }

    try {
        const response = await api.post('/chat', {
            request_id: requestId,
            message,
        }, {
            signal,
            timeout,
        });
        return response.data;
    } catch (error) {
        if (error instanceof ChatError) throw error;
        throw createChatError(error);
    }
};
