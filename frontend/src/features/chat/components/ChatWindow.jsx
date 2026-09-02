import React from 'react';
import EmotionPanel from './EmotionPanel';
import ChatHeader from './ChatHeader';
import MessageList from './MessageList';
import ChatInput from './ChatInput';
import PrivacyPanel from '../../privacy/PrivacyPanel';
import { useChat } from '../hooks/useChat';

const ChatWindow = () => {
    const {
        messages,
        input,
        setInput,
        isLoading,
        emotionState,
        messagesEndRef,
        inputRef,
        handleSend,
        clearScreen,
        transport
    } = useChat();

    return (
        <div className="flex flex-col h-screen max-w-6xl mx-auto relative">
            {/* Header */}
            <ChatHeader clearScreen={clearScreen} />

            {/* Main Content Area */}
            <div className="flex-1 flex flex-col md:flex-row overflow-hidden">

                {/* Chat Area */}
                <main className="flex-1 flex flex-col relative min-w-0">
                    <MessageList
                        messages={messages}
                        isLoading={isLoading}
                        messagesEndRef={messagesEndRef}
                    />

                    {/* Input Area */}
                    <ChatInput
                        input={input}
                        setInput={setInput}
                        handleSend={handleSend}
                        isLoading={isLoading}
                        inputRef={inputRef}
                    />
                </main>

                {/* Emotion Panel (Sidebar on desktop, hidden if no emotion state yet) */}
                {emotionState && (
                    <aside className="hidden md:block border-l border-gray-800 bg-gray-900 p-4 overflow-y-auto">
                        <EmotionPanel emotionState={emotionState} />
                    </aside>
                )}

                {/* #336: local privacy ops — desktop shell only. The panel
                     itself renders nothing under a web transport, so the
                     web app never sees this surface. Kept visible even
                     before the first turn so the user can always erase
                     local data. */}
                <aside className="hidden md:block border-l border-gray-800 bg-gray-900 p-4 overflow-y-auto">
                    <PrivacyPanel transport={transport} />
                </aside>
            </div>

            {/* Mobile Emotion Panel */}
            {emotionState && (
                <div className="md:hidden px-4 pt-2">
                    <EmotionPanel emotionState={emotionState} />
                </div>
            )}
        </div>
    );
};

export default ChatWindow;
