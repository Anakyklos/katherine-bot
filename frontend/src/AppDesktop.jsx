/**
 * Desktop companion root (#336, review blocker 1).
 *
 * This module is the desktop app's ONLY root. It contains ONLY
 * desktop concerns and imports nothing from the web stack: no
 * supabaseClient, no AuthPage, no web auth/session logic. The web
 * modules live exclusively behind `AppWeb`/`main-web.jsx`.
 *
 * The desktop companion is single-user local (no login, no session,
 * no cloud): ChatWindow renders directly and all data flows through
 * the local bridge transport (useChat / chatTransport /
 * desktopBridge).
 */
import ChatWindow from './features/chat/components/ChatWindow';
import CompanionLayout from './features/chat/components/CompanionLayout.jsx';

const renderCompanionLayout = (chatModel) => (
    <CompanionLayout {...chatModel} />
);

export default function AppDesktop() {
    return (
        <div className="min-h-screen bg-gray-900 text-gray-100 font-sans antialiased">
            <ChatWindow renderLayout={renderCompanionLayout} />
        </div>
    );
}
