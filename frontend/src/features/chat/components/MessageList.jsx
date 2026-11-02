import React from 'react';
import { Loader2 } from 'lucide-react';
import MessageBubble from './MessageBubble';

export function getMessageReactKey(message) {
    if (message.serverId !== null && message.serverId !== undefined) {
        return `server:${typeof message.serverId}:${String(message.serverId)}`;
    }
    if (message.id !== null && message.id !== undefined) {
        return `server:${typeof message.id}:${String(message.id)}`;
    }
    return `local:${message.localId}`;
}

const MessageList = ({
    messages,
    isLoading,
    messagesEndRef,
    onRetry,
    loadingLabel = 'Katherine está digitando...',
}) => {
    return (
        <div className="flex-1 overflow-y-auto p-4 md:p-8 space-y-6 scroll-smooth">
            {messages.length === 0 && (
                <div className="h-full flex flex-col items-center justify-center text-gray-500 opacity-50">
                    <p>Comece uma conversa com a Katherine...</p>
                </div>
            )}

            {messages.map(message => (
                message.role === 'system' ? (
                    <div key={getMessageReactKey(message)} className="text-center text-red-400 text-sm py-2">
                        {message.content}
                    </div>
                ) : (
                    <div key={getMessageReactKey(message)} className="space-y-1">
                        <MessageBubble
                            message={message.content}
                            isUser={message.role === 'user'}
                        />
                        {message.role === 'user' && message.deliveryStatus && message.deliveryStatus !== 'sent' && (
                            <div
                                className={`flex items-center gap-3 px-4 text-xs ${
                                    message.deliveryStatus === 'pending'
                                        ? 'justify-end text-gray-400'
                                        : 'justify-end text-red-300'
                                }`}
                                role={message.deliveryStatus === 'pending' ? 'status' : 'alert'}
                                aria-live={message.deliveryStatus === 'pending' ? 'polite' : 'assertive'}
                            >
                                <span>
                                    {message.deliveryStatus === 'pending'
                                        ? 'Enviando…'
                                        : message.errorMessage || 'Não foi possível confirmar este envio.'}
                                </span>
                                {message.retryable && message.deliveryStatus !== 'pending' && (
                                    <button
                                        type="button"
                                        onClick={() => onRetry?.(message.localId)}
                                        disabled={isLoading}
                                        className="rounded px-2 py-1 text-gray-200 underline underline-offset-2 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-400 disabled:cursor-not-allowed disabled:opacity-50"
                                    >
                                        Tentar novamente
                                    </button>
                                )}
                            </div>
                        )}
                    </div>
                )
            ))}

            {isLoading && (
                <div
                    className="flex items-center gap-2 text-gray-500 text-sm ml-2 animate-pulse"
                    role="status"
                    aria-live="polite"
                >
                    <Loader2 size={16} className="animate-spin" />
                    {loadingLabel}
                </div>
            )}
            <div ref={messagesEndRef} />
        </div>
    );
};

export default MessageList;
