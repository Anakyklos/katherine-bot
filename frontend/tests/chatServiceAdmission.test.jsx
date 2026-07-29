import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockPost = vi.hoisted(() => vi.fn());

vi.mock('../src/shared/services/apiClient.js', () => ({
    default: {
        post: mockPost,
    },
}));

import {
    ChatError,
    classifyHttpError,
    createChatError,
    sendMessage,
} from '../src/features/chat/services/chatService.js';

const REQUEST_ID = '550e8400-e29b-41d4-a716-446655440000';

describe('chat admission service contract', () => {
    beforeEach(() => {
        mockPost.mockReset();
    });

    it('sends the exact request_id and message payload', async () => {
        mockPost.mockResolvedValue({ data: { response: 'ok', emotion_state: {} } });
        const signal = new AbortController().signal;

        await sendMessage('hello', { signal, timeout: 1234 }, REQUEST_ID);

        expect(mockPost).toHaveBeenCalledTimes(1);
        expect(mockPost).toHaveBeenCalledWith(
            '/chat',
            { request_id: REQUEST_ID, message: 'hello' },
            { signal, timeout: 1234 },
        );
    });

    it('fails locally when request ID is absent', async () => {
        await expect(sendMessage('hello', {}, undefined)).rejects.toMatchObject({
            name: 'ChatError',
            type: 'validation',
        });
        expect(mockPost).not.toHaveBeenCalled();
    });

    it('rethrows an existing ChatError without wrapping it', async () => {
        const existing = new ChatError('validation', 'safe existing error');
        mockPost.mockRejectedValue(existing);

        let received;
        try {
            await sendMessage('hello', {}, REQUEST_ID);
        } catch (error) {
            received = error;
        }

        expect(received).toBe(existing);
    });

    it('classifies replay and conflict as distinct safe errors', () => {
        expect(classifyHttpError(409, 'request_replay_unavailable')).toBe('request_replay');
        expect(classifyHttpError(409, 'request_id_conflict')).toBe('request_conflict');

        const replay = createChatError({
            response: {
                status: 409,
                data: { detail: { code: 'request_replay_unavailable' } },
            },
        });
        const conflict = createChatError({
            response: {
                status: 409,
                data: { detail: { code: 'request_id_conflict' } },
            },
        });

        expect(replay).toBeInstanceOf(ChatError);
        expect(replay.type).toBe('request_replay');
        expect(conflict.type).toBe('request_conflict');
        expect(replay.message).not.toBe(conflict.message);
    });

    it('does not expose raw request identifiers or response fields', () => {
        const marker = 'sensitive-request-id-marker';
        const error = createChatError({
            response: {
                status: 409,
                data: {
                    detail: {
                        code: 'request_id_conflict',
                        request_id: marker,
                        message: marker,
                    },
                },
            },
            config: { data: marker },
        });

        expect(error.message).not.toContain(marker);
        expect(error.message).not.toContain('request_id');
    });

    it('preserves the existing 429 classification', () => {
        const error = createChatError({ response: { status: 429, data: {} } });
        expect(error.type).toBe('rate_limited');
    });
});
