import React from 'react';
import ChatHeader from './ChatHeader';
import ChatInput from './ChatInput';
import EmotionPanel from './EmotionPanel';
import MessageList from './MessageList';
import KatherineFace from '../../katherine-face/KatherineFace.jsx';
import PrivacyPanel from '../../privacy/PrivacyPanel';
import { isDesktopTransport } from '../services/transportMode';
import './CompanionLayout.css';

/**
 * Presentational desktop companion composition.
 *
 * ChatWindow owns the single useChat() call and passes its model here. This
 * component only arranges existing surfaces. The face remains decorative and
 * receives the same public state boundary as the original desktop layout.
 */
export default function CompanionLayout({
    messages,
    input,
    setInput,
    isLoading,
    emotionState,
    messagesEndRef,
    inputRef,
    handleSend,
    clearScreen,
    transport,
    auxiliarySlot = null,
}) {
    const hasAuxiliarySlot = Boolean(auxiliarySlot);
    const hasDesktopPrivacy = isDesktopTransport(transport);

    return (
        <div className="companion-layout" data-testid="companion-layout">
            <ChatHeader clearScreen={clearScreen} />

            <main
                className={`companion-layout__body${hasAuxiliarySlot ? ' companion-layout__body--with-auxiliary' : ''}`}
                aria-label="Companion Katherine"
            >
                <section
                    className="companion-layout__presence"
                    aria-label="Presença da Katherine"
                    data-testid="companion-presence"
                >
                    <KatherineFace
                        emotionState={emotionState}
                        isLoading={isLoading}
                        className="katherine-face--companion"
                    />
                </section>

                <section
                    className="companion-layout__conversation"
                    aria-label="Conversa com Katherine"
                >
                    <div
                        className="companion-layout__history"
                        data-testid="companion-history"
                        aria-label="Histórico da conversa"
                    >
                        <MessageList
                            messages={messages}
                            isLoading={isLoading}
                            messagesEndRef={messagesEndRef}
                        />
                    </div>

                    <ChatInput
                        input={input}
                        setInput={setInput}
                        handleSend={handleSend}
                        isLoading={isLoading}
                        inputRef={inputRef}
                    />

                    <div
                        className="companion-layout__utilities"
                        data-testid="companion-utilities"
                        aria-label="Ferramentas do companion"
                    >
                        {emotionState && (
                            <details data-testid="companion-emotion-details">
                                <summary>Detalhes do estado</summary>
                                <div className="companion-layout__utility-content">
                                    <EmotionPanel emotionState={emotionState} />
                                </div>
                            </details>
                        )}

                        {hasDesktopPrivacy && (
                            <details data-testid="companion-privacy-details">
                                <summary>Privacidade local</summary>
                                <div className="companion-layout__utility-content">
                                    <PrivacyPanel transport={transport} />
                                </div>
                            </details>
                        )}
                    </div>
                </section>

                {hasAuxiliarySlot && (
                    <aside
                        className="companion-layout__auxiliary"
                        aria-label="Área auxiliar da Katherine"
                        data-testid="companion-auxiliary-slot"
                    >
                        {auxiliarySlot}
                    </aside>
                )}
            </main>
        </div>
    );
}
