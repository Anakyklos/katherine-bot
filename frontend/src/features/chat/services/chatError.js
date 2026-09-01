/**
 * The shared chat error type (#267, extracted #336).
 *
 * A stable, sanitised classification surface for chat failures. The type
 * union is shared by the web service (Axios classification) and the
 * desktop bridge (stable bridge code mapping), so consumers can handle
 * both branches identically.
 *
 * This module is dependency-free on purpose: the desktop bridge imports
 * it, and importing chatService from there would pull the Supabase
 * client into the desktop module graph.
 */

export const CHAT_ERROR_TYPES = Object.freeze([
    'timeout',
    'rate_limited',
    'service_unavailable',
    'validation',
    'request_replay',
    'request_conflict',
    'configuration',
    'unknown',
]);

export class ChatError extends Error {
    /**
     * @param {'timeout'|'rate_limited'|'service_unavailable'|'validation'|'request_replay'|'request_conflict'|'configuration'|'unknown'} type
     * @param {string} message
     */
    constructor(type, message) {
        super(message);
        this.name = 'ChatError';
        this.type = type;
    }
}
