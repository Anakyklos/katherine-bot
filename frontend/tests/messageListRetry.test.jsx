import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import MessageList, { getMessageReactKey } from '../src/features/chat/components/MessageList.jsx';

describe('MessageList delivery feedback', () => {
    it('derives React keys from persisted or stable local identity', () => {
        const persisted = { serverId: 42, localId: 'server:number:42' };
        const optimistic = { serverId: null, localId: 'local:request-1:user' };
        const initialKeys = [persisted, optimistic].map(getMessageReactKey);
        const reorderedKeys = [optimistic, persisted].map(getMessageReactKey);

        expect(getMessageReactKey(persisted)).toBe('server:number:42');
        expect(getMessageReactKey(optimistic)).toBe('local:local:request-1:user');
        expect(initialKeys).toEqual(['server:number:42', 'local:local:request-1:user']);
        expect(reorderedKeys).toEqual(['local:local:request-1:user', 'server:number:42']);
    });

    it('shows an accessible explicit retry action for an uncertain message', () => {
        const onRetry = vi.fn();
        render(
            <MessageList
                messages={[
                    {
                        localId: 'local:request-1:user',
                        serverId: null,
                        requestId: 'request-1',
                        role: 'user',
                        content: 'pergunta original',
                        deliveryStatus: 'unknown',
                        errorMessage: 'A requisição excedeu o tempo limite.',
                        retryable: true,
                    },
                ]}
                isLoading={false}
                onRetry={onRetry}
                messagesEndRef={{ current: null }}
            />,
        );

        expect(screen.getByRole('alert')).toHaveTextContent('A requisição excedeu o tempo limite.');
        fireEvent.click(screen.getByRole('button', { name: /tentar novamente/i }));
        expect(onRetry).toHaveBeenCalledWith('local:request-1:user');
    });
});
