/**
 * Web application root (#336, review blocker 1).
 *
 * This module is the web app root. It contains ONLY web concerns:
 * Supabase session observation and the AuthPage gate. It is reachable
 * exclusively from `main-web.jsx`; the desktop shell renders
 * `AppDesktop` via `main-desktop.jsx` (separate vite HTML entry), so
 * this module — and every web-only import it pulls in
 * (supabaseClient, AuthPage, and the web chatService path inside
 * chatTransport) — is never part of the desktop bundle graph.
 *
 * The mechanical proof lives in `tests/desktopGraph.test.js`.
 */
import { useState, useEffect } from 'react';
import { supabase } from './lib/supabaseClient';
import AuthPage from './features/auth/AuthPage';
import ChatWindow from './features/chat/components/ChatWindow';

function AppWeb() {
    const [session, setSession] = useState(null);

    useEffect(() => {
        // Without a Supabase client (e.g. a build without web
        // credentials, #334) there is no session to observe: show the
        // auth screen instead of crashing on null.auth.
        if (!supabase) {
            setSession(null);
            return;
        }

        supabase.auth.getSession().then(({ data: { session } }) => {
            setSession(session);
        });

        const {
            data: { subscription },
        } = supabase.auth.onAuthStateChange((_event, session) => {
            setSession(session);
        });

        return () => subscription.unsubscribe();
    }, []);

    if (!session) {
        return <AuthPage />;
    }

    return (
        <div className="min-h-screen bg-gray-900 text-gray-100 font-sans antialiased">
            <ChatWindow />
        </div>
    );
}

export default AppWeb;
