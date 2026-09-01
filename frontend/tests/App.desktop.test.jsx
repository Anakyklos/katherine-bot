/**
 * App-level desktop mode tests (#336, T015).
 *
 * Inside the pywebview shell the app renders ChatWindow directly: no
 * AuthPage, no Supabase session read. The web branch is untouched
 * (covered by authPage.test.jsx and the existing suites).
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, waitFor } from '@testing-library/react';

const mockGetSession = vi.fn().mockResolvedValue({ data: { session: null } });
const mockOnAuthStateChange = vi.fn(() => ({ data: { subscription: { unsubscribe: vi.fn() } } }));

vi.mock('../src/lib/supabaseClient', () => ({
    supabase: {
        auth: {
            getSession: (...a) => mockGetSession(...a),
            onAuthStateChange: (...a) => mockOnAuthStateChange(...a),
        },
    },
}));

import App from '../src/App';

// jsdom has no smooth-scroll implementation; the chat hook scrolls the
// messages end marker on mount. This is a no-op polyfill for the test env.
if (typeof window.HTMLElement.prototype.scrollIntoView !== 'function') {
    window.HTMLElement.prototype.scrollIntoView = () => {};
}

/** Set/clear the real shell global (the production signal). */
function setShellGlobal(present) {
    if (present) {
        window.pywebview = { api: {} };
    } else {
        delete window.pywebview;
    }
}

describe('App desktop branch', () => {
    it('renders the chat window directly inside the shell (no AuthPage)', () => {
        setShellGlobal(true);
        try {
            render(<App />);

            // Chat UI present without any login flow.
            expect(screen.getByPlaceholderText(/escreva aqui sua mensagem/i)).toBeInTheDocument();
            expect(screen.queryByRole('button', { name: /entrar/i })).toBeNull();

            // No Supabase session read on the desktop branch.
            expect(mockGetSession).not.toHaveBeenCalled();
        } finally {
            setShellGlobal(false);
        }
    });

    it('shows the auth page on the web branch (session null)', async () => {
        setShellGlobal(false);
        try {
            render(<App />);
            await waitFor(() => expect(mockGetSession).toHaveBeenCalled());
            // Auth surface present (web login unchanged).
            expect(screen.queryByPlaceholderText(/escreva aqui sua mensagem/i)).toBeNull();
        } finally {
            setShellGlobal(false);
        }
    });
});
