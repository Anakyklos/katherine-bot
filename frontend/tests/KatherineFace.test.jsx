import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';

const vendorProps = vi.hoisted(() => ({ current: null }));

vi.mock('../src/vendor/bfce/Face.jsx', () => ({
    default: (props) => {
        vendorProps.current = props;
        return (
            <div
                data-testid="bfce-face"
                data-expression={props.expression}
                aria-hidden="true"
            />
        );
    },
}));

import KatherineFace from '../src/features/katherine-face/KatherineFace.jsx';
import * as bfcePublic from '../src/vendor/bfce/index.js';

const stateFor = (name, intensity = 0.2) => ({
    schema_version: 1,
    mood_label: 'NEUTRA',
    pad: { pleasure: 0, arousal: 0, dominance: 0 },
    dominant_emotions: [{ name, intensity }],
    timestamp: 1700000000,
});

describe('KatherineFace', () => {
    beforeEach(() => {
        vendorProps.current = null;
    });

    it('renders the mapped expression as a decorative face', () => {
        render(<KatherineFace emotionState={stateFor('joy', 0.8)} isLoading={false} />);

        expect(screen.getByTestId('katherine-face')).toHaveAttribute('aria-hidden', 'true');
        expect(screen.getByTestId('bfce-face')).toHaveAttribute('data-expression', 'joy');
    });

    it('projects loading as thinking without changing the emotion state', () => {
        const emotionState = stateFor('sadness', 0.9);
        const snapshot = structuredClone(emotionState);

        render(<KatherineFace emotionState={emotionState} isLoading />);

        expect(screen.getByTestId('bfce-face')).toHaveAttribute('data-expression', 'thinking');
        expect(emotionState).toEqual(snapshot);
    });

    it('fails closed to idle for absent or invalid public state', () => {
        render(<KatherineFace emotionState={null} isLoading={false} />);
        expect(screen.getByTestId('bfce-face')).toHaveAttribute('data-expression', 'idle');
    });

    it('passes only the minimum approved rendering props to bfce', () => {
        render(
            <KatherineFace
                emotionState={stateFor('trust')}
                isLoading={false}
                className="test-face"
                message="private conversation"
                prompt="private prompt"
                token="secret-token"
                userId="user-123"
                memory={{ sensitive: true }}
                relationship={{ private: true }}
                response="private response"
                transport={{ sendMessage: vi.fn() }}
            />,
        );

        expect(vendorProps.current).toEqual(expect.objectContaining({
            expression: 'content',
            mouth: true,
            pupils: false,
            track: false,
            blink: false,
            idle: false,
        }));
        expect(vendorProps.current).not.toHaveProperty('message');
        expect(vendorProps.current).not.toHaveProperty('prompt');
        expect(vendorProps.current).not.toHaveProperty('token');
        expect(vendorProps.current).not.toHaveProperty('userId');
        expect(vendorProps.current).not.toHaveProperty('memory');
        expect(vendorProps.current).not.toHaveProperty('relationship');
        expect(vendorProps.current).not.toHaveProperty('response');
        expect(vendorProps.current).not.toHaveProperty('transport');
    });

    it('does not initiate network requests or expose focusable controls', () => {
        const fetchSpy = vi.spyOn(globalThis, 'fetch');
        const xhrOpen = vi.spyOn(XMLHttpRequest.prototype, 'open');

        render(<KatherineFace emotionState={stateFor('gratitude')} isLoading={false} />);

        expect(fetchSpy).not.toHaveBeenCalled();
        expect(xhrOpen).not.toHaveBeenCalled();
        expect(screen.getByTestId('katherine-face')).not.toHaveAttribute('tabindex');
        expect(screen.queryByRole('button')).toBeNull();
        expect(screen.queryByRole('textbox')).toBeNull();

        fetchSpy.mockRestore();
        xhrOpen.mockRestore();
    });

    it('keeps unsupported bfce expression presets out of the public vendor surface', () => {
        expect(bfcePublic).not.toHaveProperty('EXPRESSIONS');
        expect(bfcePublic).not.toHaveProperty('EXPRESSION_NAMES');
        expect(bfcePublic).not.toHaveProperty('REACTIONS');
        expect(bfcePublic).not.toHaveProperty('REACTION_NAMES');
    });
});
