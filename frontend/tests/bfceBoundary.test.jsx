import React, { createRef } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';

const createFaceMock = vi.hoisted(() => vi.fn());

vi.mock('../src/vendor/bfce/core.js', () => ({
    createFace: createFaceMock,
}));

import Face from '../src/vendor/bfce/Face.jsx';

const makeInstance = () => ({
    destroy: vi.fn(),
    setExpression: vi.fn(),
    react: vi.fn(),
    look: vi.fn(),
});

describe('bfce React boundary', () => {
    beforeEach(() => {
        vi.restoreAllMocks();
        createFaceMock.mockReset();
        document.body.innerHTML = '';
    });

    it('normalizes unsupported expressions before they reach bfce', () => {
        const instance = makeInstance();
        createFaceMock.mockReturnValue(instance);

        render(
            <Face
                expression="love"
                data-sensitive="must-not-reach-the-vendor"
            />,
        );

        expect(createFaceMock).toHaveBeenCalledWith(
            expect.any(HTMLElement),
            expect.objectContaining({ expression: 'idle' }),
        );
        expect(instance.setExpression).toHaveBeenCalledWith('idle');
        expect(document.querySelector('.bwface-host').hasAttribute('data-sensitive')).toBe(false);
    });

    it('keeps imperative expression changes inside the approved vocabulary', () => {
        const instance = makeInstance();
        createFaceMock.mockReturnValue(instance);
        const ref = createRef();

        render(<Face ref={ref} expression="happy" />);

        act(() => {
            ref.current.setExpression('smug');
        });

        expect(instance.setExpression).toHaveBeenLastCalledWith('idle');
    });
});
