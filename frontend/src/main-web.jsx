/**
 * Web entry (#336, review blocker 1).
 *
 * The web deployment keeps its own root and its own entry: `AppWeb`
 * (Supabase session + AuthPage gate) is imported statically here and
 * NOWHERE in the desktop graph. The desktop shell renders
 * `AppDesktop` via `main-desktop.jsx` instead.
 */
import React from 'react';
import ReactDOM from 'react-dom/client';
import AppWeb from './AppWeb.jsx';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')).render(
    <React.StrictMode>
        <AppWeb />
    </React.StrictMode>,
);
