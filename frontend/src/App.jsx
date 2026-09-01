import { useState, useEffect } from 'react';
import { supabase } from './lib/supabaseClient';
import { isDesktopShell } from './lib/runtimeMode';
import AuthPage from './features/auth/AuthPage';
import ChatWindow from './features/chat/components/ChatWindow';

function App() {
    const [session, setSession] = useState(null);

    // #336: inside the pywebview shell the app is the companion: no
    // login, no Supabase session, no cloud. The chat window renders
    // directly and all data flows through the local bridge transport
    // (see useChat / chatTransport). This branch is decided once, from
    // the explicit shell signal — never from "credentials are missing".
    if (isDesktopShell()) {
        return (
            <div className="min-h-screen bg-gray-900 text-gray-100 font-sans antialiased">
                <ChatWindow />
            </div>
        );
    }

    useEffect(() => {
        // Without a Supabase client (e.g. desktop shell build without
        // web credentials, #334) there is no session to observe: show
        // the auth screen instead of crashing on null.auth.
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

export default App;
