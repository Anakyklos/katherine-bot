import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';

const chatHarness = vi.hoisted(() => ({
    calls: 0,
    model: {
        messages: [],
        input: '',
        setInput: vi.fn(),
        isLoading: false,
        emotionState: null,
        messagesEndRef: { current: null },
        inputRef: { current: null },
        handleSend: vi.fn(),
        clearScreen: vi.fn(),
        transport: {
            mode: 'desktop',
            runPrivacyOp: vi.fn(async () => ({ status: 'applied' })),
        },
    },
}));

const faceHarness = vi.hoisted(() => ({ current: null }));

vi.mock('../src/features/chat/hooks/useChat', () => ({
    useChat: () => {
        chatHarness.calls += 1;
        return chatHarness.model;
    },
}));

vi.mock('../src/features/katherine-face/KatherineFace.jsx', () => ({
    default: (props) => {
        faceHarness.current = props;
        return (
            <div
                data-testid="katherine-face"
                aria-hidden="true"
                className={props.className}
            />
        );
    },
}));

import AppDesktop from '../src/AppDesktop.jsx';

if (typeof HTMLElement.prototype.scrollIntoView !== 'function') {
    HTMLElement.prototype.scrollIntoView = () => {};
}

const validEmotionState = {
    schema_version: 1,
    mood_label: 'NEUTRA',
    pad: { pleasure: 0.2, arousal: 0.1, dominance: -0.1 },
    dominant_emotions: [{ name: 'gratitude', intensity: 0.8 }],
    timestamp: 1700000000,
};

function resetModel() {
    chatHarness.calls = 0;
    Object.assign(chatHarness.model, {
        messages: [],
        input: '',
        isLoading: false,
        emotionState: null,
    });
    faceHarness.current = null;
    document.body.innerHTML = '';
    delete window.pywebview;
}

describe('AppDesktop companion integration', () => {
    beforeEach(resetModel);

    it('uses one chat model and places the empty desktop conversation beside the dominant face', () => {
        render(<AppDesktop />);

        expect(chatHarness.calls).toBe(1);
        expect(screen.getByTestId('companion-layout')).toBeInTheDocument();
        expect(screen.getByTestId('companion-presence')).toContainElement(
            screen.getByTestId('katherine-face'),
        );
        expect(screen.getByTestId('katherine-face').closest('header')).toBeNull();
        expect(screen.getByText(/comece uma conversa com a katherine/i)).toBeInTheDocument();
        expect(screen.getByRole('textbox', { name: /sua mensagem/i })).toBeInTheDocument();
        expect(faceHarness.current).toEqual(expect.objectContaining({
            emotionState: null,
            isLoading: false,
        }));
    });

    it('passes a valid emotion state and loading status through the existing desktop model', () => {
        chatHarness.model.emotionState = validEmotionState;
        chatHarness.model.isLoading = true;

        render(<AppDesktop />);

        expect(faceHarness.current).toEqual(expect.objectContaining({
            emotionState: validEmotionState,
            isLoading: true,
        }));
        expect(screen.getByRole('status')).toHaveTextContent('Katherine está digitando...');
        expect(screen.getByRole('textbox', { name: /sua mensagem/i })).toBeDisabled();
    });

    it('keeps history and technical error copy in the real desktop conversation rail', () => {
        chatHarness.model.messages = [
            { role: 'user', content: 'Mensagem anterior' },
            { role: 'system', content: 'Erro ao falar com a Katherine. Tente novamente.' },
        ];

        render(<AppDesktop />);

        const history = screen.getByTestId('companion-history');
        expect(history).toHaveTextContent('Mensagem anterior');
        expect(history).toHaveTextContent(/erro ao falar com a katherine/i);
    });
});
