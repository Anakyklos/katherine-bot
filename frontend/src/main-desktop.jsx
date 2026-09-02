/**
 * Desktop shell entry (#336, review blocker 1).
 *
 * `desktop.html` + this entry form the desktop app: a graph that
 * provably contains no web modules (supabaseClient/apiClient/
 * chatService/AuthPage live only behind `AppWeb`, which is reachable
 * exclusively from `main-web.jsx`). A mechanical graph test
 * (`tests/desktopGraph.test.js`) fails the build if that ever regresses.
 *
 * The mode is a *positive* shell signal (`window.pywebview`, decided in
 * runtimeMode): this entry does not sniff credentials and does not fall
 * back to the web flow. No login screen exists in the desktop app.
 */
import React from 'react';
import ReactDOM from 'react-dom/client';
import AppDesktop from './AppDesktop.jsx';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <AppDesktop />
    </React.StrictMode>,
);
