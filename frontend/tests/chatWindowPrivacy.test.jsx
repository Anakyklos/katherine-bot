/**
 * ChatWindow privacy-panel integration (#336, T020).
 *
 * ChatWindow feeds its useChat transport to the local privacy panel.
 * Desktop transport: the panel is present. Web transport: the panel
 * must not exist in the DOM at all (the web app never sees the local
 * data surface).
 *
 * The mode enters through the default transport factory (what useChat
 * uses inside ChatWindow); isDesktopTransport follows the same module
 * so the panel's own gate and the transport branch stay consistent.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';

// Desktop shell has no Supabase credentials: ChatWindow must mount with
// the client module inert.
vi.mock('../src/lib/supabaseClient', () => ({ supabase: null }));
vi.mock('../src/shared/services/apiClient', () => ({
    default: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

// The mode under test, swapped per test via the state holder.
const state = { mode: 'desktop', transport: null };

vi.mock('../src/features/chat/services/chatTransport', async (importOriginal) => {
    const actual = await importOriginal();
    return {
        ...actual,
        createDefaultTransport: () => state.transport,
    };
});

vi.mock('../src/features/chat/services/transportMode', () => ({
    isDesktopTransport: (transport) => transport?.mode === 'desktop',
}));

function makeTransport(mode) {
    return {
        mode,
        fetchHistory: vi.fn(async () => ({ data: [] })),
        sendMessage: vi.fn(async () => { throw new Error('not used'); }),
        runPrivacyOp: vi.fn(async () => ({ status: 'applied' })),
        getRuntimeState: vi.fn(async () => ({
            ok: true, storage: true, provider_configured: true, revision: 0,
        })),
    };
}

// jsdom does not implement scrollIntoView; ChatWindow's auto-scroll must
// stay a no-op under test.
if (typeof HTMLElement.prototype.scrollIntoView !== 'function') {
    HTMLElement.prototype.scrollIntoView = () => {};
}

import ChatWindow from '../src/features/chat/components/ChatWindow';

describe('ChatWindow privacy panel integration', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('shows the privacy panel for a desktop transport', () => {
        state.transport = makeTransport('desktop');
        render(<ChatWindow />);
        expect(screen.getByTestId('privacy-panel')).toBeInTheDocument();
    });

    it('renders no privacy panel for a web transport', () => {
        state.transport = makeTransport('web');
        render(<ChatWindow />);
        expect(screen.queryByTestId('privacy-panel')).toBeNull();
    });
});
