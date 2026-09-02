/**
 * PrivacyPanel + useDesktopPrivacy tests (#336, T020/T021).
 *
 * Desktop-only privacy surface: four destructive local operations, each
 * behind an explicit confirmation, wired through the transport's
 * desktop privacy ops (never Axios, never Supabase). The panel must
 * never render in web mode.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';

import PrivacyPanel from '../src/features/privacy/PrivacyPanel';
import { useDesktopPrivacy } from '../src/features/privacy/useDesktopPrivacy';

function makeTransport({ privacyImpl } = {}) {
    const calls = [];
    return {
        mode: 'desktop',
        calls,
        runPrivacyOp: vi.fn(async (op) => {
            calls.push(op);
            if (privacyImpl) return privacyImpl(op);
            return { status: 'applied', rows: 3 };
        }),
    };
}

describe('useDesktopPrivacy', () => {
    it('runs an op through the transport and reports the result', async () => {
        const transport = makeTransport();
        let hook;
        function Probe() {
            hook = useDesktopPrivacy(transport);
            return null;
        }
        render(<Probe />);

        expect(hook.pending).toBe(null);
        const result = await hook.runOp('delete_history');
        expect(result).toEqual({ ok: true, status: 'applied', rows: 3 });
        expect(transport.calls).toEqual(['delete_history']);
    });

    it('blocks a second op while one is in flight', async () => {
        let resolve;
        const transport = {
            mode: 'desktop',
            runPrivacyOp: vi.fn(() => new Promise(r => { resolve = r; })),
        };
        let hook;
        function Probe() {
            hook = useDesktopPrivacy(transport);
            return null;
        }
        render(<Probe />);

        const first = hook.runOp('delete_history');
        await act(async () => {}); // let the pending state commit
        const blocked = await hook.runOp('delete_memories'); // no-op, not a call
        expect(transport.runPrivacyOp).toHaveBeenCalledTimes(1);
        expect(blocked.ok).toBe(false);

        await act(async () => {
            resolve({ status: 'applied' });
            await first;
        });
    });

    it('surfaces a sanitized error message on failure', async () => {
        const transport = {
            mode: 'desktop',
            runPrivacyOp: vi.fn(async () => {
                throw Object.assign(new Error('O armazenamento local não está disponível.'), {
                    name: 'ChatError', type: 'service_unavailable',
                });
            }),
        };
        let hook;
        function Probe() {
            hook = useDesktopPrivacy(transport);
            return null;
        }
        render(<Probe />);

        const result = await hook.runOp('delete_history');
        expect(result.ok).toBe(false);
        expect(result.message).toContain('armazenamento local');
    });
});

describe('PrivacyPanel', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('lists the four local privacy operations in Portuguese', () => {
        render(<PrivacyPanel transport={makeTransport()} />);
        expect(screen.getByRole('button', { name: /apagar histórico/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /apagar memórias/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /reiniciar estado emocional/i })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /reiniciar vínculo/i })).toBeInTheDocument();
    });

    it('asks for confirmation before running a destructive op', async () => {
        const transport = makeTransport();
        render(<PrivacyPanel transport={transport} />);

        fireEvent.click(screen.getByRole('button', { name: /apagar histórico/i }));

        // Confirmation is required; nothing ran yet.
        expect(transport.calls).toEqual([]);
        expect(screen.getByText(/tem certeza/i)).toBeInTheDocument();

        fireEvent.click(screen.getByRole('button', { name: /confirmar/i }));
        await waitFor(() => expect(transport.calls).toEqual(['delete_history']));
        expect(await screen.findByText(/operação concluída/i)).toBeInTheDocument();
    });

    it('cancel dismisses the confirmation without running anything', () => {
        const transport = makeTransport();
        render(<PrivacyPanel transport={transport} />);

        fireEvent.click(screen.getByRole('button', { name: /apagar memórias/i }));
        fireEvent.click(screen.getByRole('button', { name: /cancelar/i }));

        expect(transport.calls).toEqual([]);
        expect(screen.queryByText(/tem certeza/i)).toBeNull();
    });

    it('shows a sanitized failure message when an op fails', async () => {
        const transport = {
            mode: 'desktop',
            runPrivacyOp: vi.fn(async () => {
                throw Object.assign(new Error('O armazenamento local não está disponível.'), {
                    name: 'ChatError', type: 'service_unavailable',
                });
            }),
        };
        render(<PrivacyPanel transport={transport} />);

        fireEvent.click(screen.getByRole('button', { name: /reiniciar vínculo/i }));
        fireEvent.click(screen.getByRole('button', { name: /confirmar/i }));

        expect(await screen.findByText(/armazenamento local/i)).toBeInTheDocument();
    });

    it('renders nothing when no transport is provided (web mode)', () => {
        const { container } = render(<PrivacyPanel />);
        expect(container).toBeEmptyDOMElement();
    });
});
