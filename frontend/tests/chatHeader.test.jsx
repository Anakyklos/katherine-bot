/**
 * Behavioral tests for ``ChatHeader`` clear-screen action — issue #313.
 *
 * The action must be presented as a local "Limpar tela" (clear screen) with
 * no persistent-deletion semantics:
 * - Label is "Limpar tela" (no "Limpar histórico" / "Limpar conversa")
 * - Neutral representation (no trash/delete icon)
 * - Confirmation states that the screen is cleared and saved history stays
 * - Confirm calls ``clearScreen``; cancel and Escape do not
 * - Escape handling, focus restoration and keyboard navigation are preserved
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ChatHeader from '../src/features/chat/components/ChatHeader';

function renderHeader(clearScreen = vi.fn()) {
    return render(<ChatHeader clearScreen={clearScreen} />);
}

describe('ChatHeader clear screen action', () => {
    it('presents the action as "Limpar tela"', () => {
        renderHeader();

        const clearButton = screen.getByRole('button', { name: 'Limpar tela' });
        expect(clearButton).toBeInTheDocument();
        expect(clearButton).toHaveAttribute('title', 'Limpar tela');
        expect(clearButton).toHaveAccessibleName('Limpar tela');
    });

    it('does not present "Limpar histórico" or "Limpar conversa" for the action', () => {
        const { container } = renderHeader();

        expect(container).not.toHaveTextContent('Limpar histórico');
        expect(container).not.toHaveTextContent('Limpar conversa');
        expect(screen.queryByRole('button', { name: /Limpar histórico/ })).toBeNull();
        expect(screen.queryByRole('button', { name: /Limpar conversa/ })).toBeNull();
    });

    it('uses a neutral eraser representation, not a trash/delete icon', () => {
        const { container } = renderHeader();

        // The action is represented by the eraser icon (lucide-eraser), which
        // means clearing the visible surface, not deleting persisted data.
        expect(container.querySelector('svg.lucide-eraser')).not.toBeNull();
        expect(container.querySelector('svg.lucide-trash2')).toBeNull();
        expect(container.querySelector('svg.lucide-trash')).toBeNull();
    });

    it('confirmation informs that the screen is cleared and saved history remains', () => {
        renderHeader();

        fireEvent.click(screen.getByRole('button', { name: 'Limpar tela' }));

        expect(screen.getByText(/Limpar a tela\?/)).toBeInTheDocument();
        expect(screen.getByText(/hist[oó]rico salvo permanece/i)).toBeInTheDocument();
    });

    it('confirming clears the screen and closes the confirmation', () => {
        const clearScreen = vi.fn();
        renderHeader(clearScreen);

        fireEvent.click(screen.getByRole('button', { name: 'Limpar tela' }));
        fireEvent.click(screen.getByRole('button', { name: 'Confirmar limpeza da tela' }));

        expect(clearScreen).toHaveBeenCalledTimes(1);
        expect(screen.queryByRole('button', { name: 'Confirmar limpeza da tela' })).toBeNull();
        expect(screen.getByRole('button', { name: 'Limpar tela' })).toBeInTheDocument();
    });

    it('canceling closes the confirmation without clearing', () => {
        const clearScreen = vi.fn();
        renderHeader(clearScreen);

        fireEvent.click(screen.getByRole('button', { name: 'Limpar tela' }));
        fireEvent.click(screen.getByRole('button', { name: 'Cancelar' }));

        expect(clearScreen).not.toHaveBeenCalled();
        expect(screen.getByRole('button', { name: 'Limpar tela' })).toBeInTheDocument();
    });

    it('Escape closes the confirmation without clearing', () => {
        const clearScreen = vi.fn();
        renderHeader(clearScreen);

        fireEvent.click(screen.getByRole('button', { name: 'Limpar tela' }));
        fireEvent.keyDown(document, { key: 'Escape' });

        expect(clearScreen).not.toHaveBeenCalled();
        expect(screen.getByRole('button', { name: 'Limpar tela' })).toBeInTheDocument();
    });

    it('restores focus to the clear button after the confirmation closes', () => {
        renderHeader();

        const clearButton = screen.getByRole('button', { name: 'Limpar tela' });
        fireEvent.click(clearButton);
        expect(screen.getByRole('button', { name: 'Cancelar' })).toHaveFocus();

        fireEvent.keyDown(document, { key: 'Escape' });
        expect(screen.getByRole('button', { name: 'Limpar tela' })).toHaveFocus();
    });
});
