/**
 * AuthPage runtime mode tests (#334, review blocker B4).
 *
 * The auth page must branch on the *explicit* runtime mode, never on
 * "credentials are missing" (which would confuse a misconfigured web
 * deploy with the desktop shell):
 *
 * - web (production, no shell global): renders the Supabase auth
 *   component; when credentials are missing it shows the web config
 *   notice — never the desktop notice;
 * - web-dev (Vite dev): same as web (mode detection is covered in
 *   runtimeMode.test.js);
 * - desktop shell (window.pywebview present): renders the local
 *   desktop notice (no web auth in the shell).
 *
 * Vitest/jsdom tests: `window.pywebview` presence drives the branch.
 */
import React from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, cleanup } from '@testing-library/react';

// AuthPage imports supabaseClient (module-level) — for the web-with-
// credentials case we need a non-null client. vi.mock factories are
// hoisted above imports, so the state must live on a stable holder
// object that each test mutates.
const supabaseState = { supabase: null };
vi.mock('../src/lib/supabaseClient', () => ({
    get supabase() {
        return supabaseState.supabase;
    },
}));

// Keep auth-ui out of these tests: rendering the real Auth component
// is unnecessary for the mode-branch contract (and drags network mocks).
vi.mock('@supabase/auth-ui-react', () => ({
    Auth: () => <div data-testid="web-auth-component" />,
}));
vi.mock('@supabase/auth-ui-shared', () => ({ ThemeSupa: {} }));

// Import after mocks are registered.
import AuthPage from '../src/features/auth/AuthPage';

function setWindow({ pywebview } = {}) {
    if (pywebview === undefined) {
        delete window.pywebview;
    } else {
        window.pywebview = pywebview;
    }
}

describe('AuthPage runtime mode branches (#334 B4)', () => {
    afterEach(() => {
        cleanup();
        setWindow({});
    });

    it('desktop shell (window.pywebview present) renders the desktop notice', () => {
        setWindow({ pywebview: { api: {} } });
        render(<AuthPage />);
        expect(screen.getByText(/Modo desktop local/i)).toBeInTheDocument();
        expect(screen.queryByTestId('web-auth-component')).toBeNull();
    });

    it('web without credentials renders the web config notice, never the desktop one', () => {
        // Core B4 regression: missing credentials in web mode must NOT
        // show "desktop" behavior.
        supabaseState.supabase = null;
        setWindow({});
        render(<AuthPage />);
        expect(
            screen.getByText(/autenticação não está configurada nesta implantação web/i)
        ).toBeInTheDocument();
        expect(screen.queryByText(/Modo desktop local/i)).toBeNull();
        expect(screen.queryByTestId('web-auth-component')).toBeNull();
    });

    it('web with credentials renders the Supabase auth component', () => {
        supabaseState.supabase = {
            auth: { getSession: async () => ({ data: { session: null } }) },
        };
        setWindow({});
        render(<AuthPage />);
        expect(screen.getByTestId('web-auth-component')).toBeInTheDocument();
        expect(screen.queryByText(/Modo desktop local/i)).toBeNull();
    });

    it('desktop shell keeps the desktop notice even if credentials exist', () => {
        // The shell wins over credentials: presence of window.pywebview
        // is the desktop signal, not the absence of credentials.
        supabaseState.supabase = {
            auth: { getSession: async () => ({ data: { session: null } }) },
        };
        setWindow({ pywebview: { api: {} } });
        render(<AuthPage />);
        expect(screen.getByText(/Modo desktop local/i)).toBeInTheDocument();
        expect(screen.queryByTestId('web-auth-component')).toBeNull();
    });
});
