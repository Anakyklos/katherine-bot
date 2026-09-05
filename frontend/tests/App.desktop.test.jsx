/**
 * App-level desktop mode tests (#336, review blocker 1).
 *
 * The desktop root (`AppDesktop`) is a separate module from the web
 * root (`AppWeb`): it renders ChatWindow directly — no AuthPage, no
 * Supabase session read — and importing it must not evaluate any web
 * module. The web branch keeps its own behavior, covered by
 * authPage.test.jsx and the existing suites.
 *
 * The graph-level proof (no supabaseClient/apiClient/chatService/
 * AuthPage in the desktop bundle) lives in tests/desktopGraph.test.jsx.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, waitFor } from '@testing-library/react';

const mockGetSession = vi.fn().mockResolvedValue({ data: { session: null } });
const mockOnAuthStateChange = vi.fn(() => ({ data: { subscription: { unsubscribe: vi.fn() } } }));

vi.mock('../src/features/katherine-face/KatherineFace.jsx', () => ({
    default: ({ emotionState, isLoading }) => (
        <div
            data-testid="katherine-face"
            data-has-emotion={String(Boolean(emotionState))}
            data-loading={String(isLoading)}
        />
    ),
}));

vi.mock('../src/lib/supabaseClient', () => ({
    supabase: {
        auth: {
            getSession: (...a) => mockGetSession(...a),
            onAuthStateChange: (...a) => mockOnAuthStateChange(...a),
        },
    },
}));

import AppDesktop from '../src/AppDesktop.jsx';
import AppWeb from '../src/AppWeb.jsx';

// jsdom has no smooth-scroll implementation; the chat hook scrolls the
// messages end marker on mount. This is a no-op polyfill for the test env.
if (typeof window.HTMLElement.prototype.scrollIntoView !== 'function') {
    window.HTMLElement.prototype.scrollIntoView = () => {};
}

describe('App desktop root', () => {
    it('renders the chat window directly (no AuthPage, no Supabase read)', () => {
        render(<AppDesktop />);

        // Chat UI present without any login flow.
        expect(screen.getByPlaceholderText(/escreva aqui sua mensagem/i)).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: /entrar/i })).toBeNull();
        expect(screen.getByTestId('katherine-face')).toHaveAttribute('data-has-emotion', 'false');
        expect(screen.getByTestId('katherine-face')).toHaveAttribute('data-loading', 'false');

        // No Supabase session read happened on the desktop root.
        expect(mockGetSession).not.toHaveBeenCalled();
    });
});

describe('App web root', () => {
    it('shows the auth page on the web branch (session null)', async () => {
        render(<AppWeb />);
        await waitFor(() => expect(mockGetSession).toHaveBeenCalled());
        // Auth surface present (web login unchanged).
        expect(screen.queryByPlaceholderText(/escreva aqui sua mensagem/i)).toBeNull();
        expect(screen.queryByTestId('katherine-face')).toBeNull();
    });
});
