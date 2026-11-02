import React from 'react';
import { Minimize2, Settings } from 'lucide-react';
import ChatHeader from './ChatHeader';
import ChatInput from './ChatInput';
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
 *
 * #347: `onOpenSettings` adds a single discrete gear button beside the
 * presence toggle in the header. Settings live in their own workspace, not
 * in this layout and not in the #344 state sidebar — this surface keeps
 * answering "how Katherine is right now".
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
    retryMessage,
    clearScreen,
    transport,
    auxiliarySlot = null,
    onEnterPresence = null,
    onOpenSettings = null,
    settingsButtonRef = null,
}) {
    const hasAuxiliarySlot = Boolean(auxiliarySlot);
    const hasDesktopPrivacy = isDesktopTransport(transport);

    return (
        <div className="companion-layout" data-testid="companion-layout">
            {onEnterPresence ? (
                <div className="companion-layout__header" data-testid="companion-header-bar">
                    <ChatHeader clearScreen={clearScreen} />
                    <div className="companion-layout__header-actions">
                        {onOpenSettings && (
                            <button
                                ref={settingsButtonRef}
                                type="button"
                                onClick={onOpenSettings}
                                className="companion-layout__header-btn"
                                data-testid="companion-open-settings-btn"
                                aria-label="Configurações da Katherine"
                            >
                                <Settings size={20} aria-hidden="true" />
                            </button>
                        )}
                        <button
                            type="button"
                            onClick={onEnterPresence}
                            className="companion-layout__presence-toggle"
                            data-testid="companion-enter-presence-btn"
                            title="Modo presença flutuante"
                            aria-label="Modo presença flutuante"
                        >
                            <Minimize2 size={20} aria-hidden="true" />
                        </button>
                    </div>
                </div>
            ) : (
                <ChatHeader clearScreen={clearScreen} />
            )}


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
                            onRetry={retryMessage}
                            messagesEndRef={messagesEndRef}
                            loadingLabel="Preparando resposta…"
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
                        {hasDesktopPrivacy && (
                            <details data-testid="companion-privacy-details">
                                <summary>Privacidade local</summary>
                                <div className="companion-layout__utility-content">
                                <PrivacyPanel transport={transport} onHistoryDeleted={clearScreen} />
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
