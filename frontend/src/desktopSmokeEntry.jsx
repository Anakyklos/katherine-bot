/**
 * Smoke entry for the desktop shell validation (#334, review B3).
 *
 * Purpose: give the reproducible smoke (`scripts/desktop_smoke.py`) a
 * target that renders the **real chat UI** (the same `ChatWindow` used
 * by the web app, with the same header/input/message components and the
 * desktop bridge badge) inside the pywebview shell.
 *
 * This is a smoke-only entry: it is NOT part of the web application
 * (index.html does not reference it), never reaches production users,
 * and mounts no fake auth state. The web app keeps its own App/Chat
 * flow untouched (AppWeb via main-web.jsx); the shell's production
 * entry is desktop.html -> main-desktop.jsx -> AppDesktop (#336,
 * review blocker 1) — this smoke entry mirrors that graph (ChatWindow
 * only, no web modules).
 *
 * Why a separate entry: the production shell build intentionally has no
 * Supabase credentials, so `App` shows the (expected) DesktopNotice.
 * The #334 acceptance "chat UI renders without external server" needs
 * the real chat components mounted in the shell — this entry does
 * exactly that, without faking production auth.
 */
import React from 'react';
import ReactDOM from 'react-dom/client';
import ChatWindow from './features/chat/components/ChatWindow.jsx';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <ChatWindow />
    </React.StrictMode>,
);
