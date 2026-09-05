import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

const faceProps = vi.hoisted(() => ({ current: null }));

vi.mock('../src/features/katherine-face/KatherineFace.jsx', () => ({
    default: (props) => {
        faceProps.current = props;
        return (
            <div
                data-testid="katherine-face"
                aria-hidden="true"
                className={props.className}
            />
        );
    },
}));

import CompanionLayout from '../src/features/chat/components/CompanionLayout.jsx';

if (typeof HTMLElement.prototype.scrollIntoView !== 'function') {
    HTMLElement.prototype.scrollIntoView = () => {};
}

const makeEmotionState = () => ({
    schema_version: 1,
    mood_label: 'NEUTRA',
    pad: { pleasure: 0, arousal: 0, dominance: 0 },
    dominant_emotions: [{ name: 'trust', intensity: 0.4 }],
    timestamp: 1700000000,
});

const makeChatModel = (overrides = {}) => ({
    messages: [],
    input: '',
    setInput: vi.fn(),
    isLoading: false,
    emotionState: null,
    messagesEndRef: React.createRef(),
    inputRef: React.createRef(),
    handleSend: vi.fn(),
    clearScreen: vi.fn(),
    transport: {
        mode: 'desktop',
        runPrivacyOp: vi.fn(async () => ({ status: 'applied' })),
    },
    ...overrides,
});

describe('CompanionLayout', () => {
    beforeEach(() => {
        faceProps.current = null;
        document.body.innerHTML = '';
    });

    it('keeps the face in the dominant presence region and the composer in the conversation rail', () => {
        render(<CompanionLayout {...makeChatModel()} />);

        const presence = screen.getByTestId('companion-presence');
        const history = screen.getByTestId('companion-history');
        const face = screen.getByTestId('katherine-face');

        expect(presence).toHaveAttribute('aria-label', 'Presença da Katherine');
        expect(presence).toContainElement(face);
        expect(history).toHaveAttribute('aria-label', 'Histórico da conversa');
        expect(within(history).getByText(/comece uma conversa/i)).toBeInTheDocument();
        expect(screen.getByRole('textbox', { name: /sua mensagem/i })).toBeInTheDocument();
        expect(face.closest('header')).toBeNull();
        expect(face).toHaveAttribute('aria-hidden', 'true');
        expect(faceProps.current.className).toContain('katherine-face--companion');
    });

    it('keeps secondary surfaces subordinate and renders no future placeholder content without data', () => {
        render(<CompanionLayout {...makeChatModel()} />);

        const privacyDetails = screen.getByTestId('companion-privacy-details');
        expect(privacyDetails.querySelector('summary')).toHaveTextContent('Privacidade local');
        expect(screen.queryByTestId('companion-emotion-details')).toBeNull();
        expect(screen.queryByText(/área futura|em breve|atividade|ouroboros/i)).toBeNull();
    });

    it('passes a valid emotion state to the existing face boundary and keeps its details collapsed', () => {
        const emotionState = makeEmotionState();
        render(<CompanionLayout {...makeChatModel({ emotionState })} />);

        expect(faceProps.current).toEqual(expect.objectContaining({
            emotionState,
            isLoading: false,
        }));
        expect(screen.getByTestId('companion-emotion-details')).not.toHaveAttribute('open');
        expect(screen.getByText('Detalhes do estado')).toBeInTheDocument();
    });

    it('keeps loading visible in the conversation rail and passes it to the face', () => {
        render(<CompanionLayout {...makeChatModel({ isLoading: true })} />);

        expect(screen.getByRole('status')).toHaveTextContent('Katherine está digitando...');
        expect(faceProps.current).toEqual(expect.objectContaining({ isLoading: true }));
        expect(screen.getByRole('textbox', { name: /sua mensagem/i })).toBeDisabled();
    });

    it('keeps existing history and system error text available', () => {
        render(<CompanionLayout {...makeChatModel({
            messages: [
                { role: 'user', content: 'Mensagem já salva' },
                { role: 'system', content: 'Erro ao falar com a Katherine. Tente novamente.' },
            ],
        })} />);

        expect(screen.getByText('Mensagem já salva')).toBeInTheDocument();
        expect(screen.getByText(/erro ao falar com a katherine/i)).toBeInTheDocument();
    });

    it('keeps all real privacy operations reachable behind a keyboard-accessible disclosure', () => {
        const transport = makeChatModel().transport;
        render(<CompanionLayout {...makeChatModel({ transport })} />);

        const privacyDetails = screen.getByTestId('companion-privacy-details');
        const summary = privacyDetails.querySelector('summary');

        expect(summary?.tagName).toBe('SUMMARY');
        expect(summary?.tabIndex).toBeGreaterThanOrEqual(0);
        fireEvent.click(summary);

        expect(privacyDetails).toHaveAttribute('open');
        for (const label of [
            /apagar histórico/i,
            /apagar memórias/i,
            /reiniciar estado emocional/i,
            /reiniciar vínculo/i,
        ]) {
            expect(within(privacyDetails).getByRole('button', { name: label })).toBeInTheDocument();
        }
        expect(transport.runPrivacyOp).not.toHaveBeenCalled();
    });

    it('renders an auxiliary slot only when a future surface explicitly supplies it', () => {
        const { rerender } = render(<CompanionLayout {...makeChatModel()} />);

        expect(screen.queryByTestId('companion-auxiliary-slot')).toBeNull();

        rerender(
            <CompanionLayout
                {...makeChatModel()}
                auxiliarySlot={<div data-testid="future-surface">Real future content</div>}
            />,
        );

        expect(screen.getByTestId('companion-auxiliary-slot')).toContainElement(
            screen.getByTestId('future-surface'),
        );
    });
});
