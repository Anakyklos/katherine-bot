import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createFace } from '../src/vendor/bfce/core.js';

const makeHost = () => {
    const host = document.createElement('div');
    host.style.width = '160px';
    host.style.height = '160px';
    document.body.appendChild(host);
    return host;
};

const installIntersectionObserver = () => {
    const instances = [];
    class TestIntersectionObserver {
        constructor(callback) {
            this.callback = callback;
            this.disconnect = vi.fn();
            instances.push(this);
        }

        observe() {}
    }
    vi.stubGlobal('IntersectionObserver', TestIntersectionObserver);
    return instances;
};

describe('vendored bfce lifecycle boundary', () => {
    beforeEach(() => {
        vi.restoreAllMocks();
        vi.unstubAllGlobals();
        document.body.innerHTML = '';
    });

    it('cleans listeners, observers, and the animation frame on destroy', () => {
        const observerInstances = installIntersectionObserver();
        const frameCallbacks = new Map();
        let nextFrameId = 1;
        const requestFrame = vi.fn((callback) => {
            const id = nextFrameId++;
            frameCallbacks.set(id, callback);
            return id;
        });
        const cancelFrame = vi.fn((id) => frameCallbacks.delete(id));
        vi.stubGlobal('requestAnimationFrame', requestFrame);
        vi.stubGlobal('cancelAnimationFrame', cancelFrame);
        const addEventListener = vi.spyOn(window, 'addEventListener');
        const removeEventListener = vi.spyOn(window, 'removeEventListener');
        const host = makeHost();

        const face = createFace(host, {
            expression: 'happy',
            track: true,
            blink: false,
            idle: false,
        });
        face.destroy();

        expect(observerInstances).toHaveLength(1);
        expect(observerInstances[0].disconnect).toHaveBeenCalledTimes(1);
        expect(cancelFrame).toHaveBeenCalledTimes(1);
        expect(host.querySelector('svg')).toBeNull();

        for (const eventName of ['pointermove', 'pointerdown', 'blur']) {
            expect(addEventListener).toHaveBeenCalledWith(
                eventName,
                expect.any(Function),
                expect.anything(),
            );
            expect(removeEventListener).toHaveBeenCalledWith(
                eventName,
                expect.any(Function),
                expect.anything(),
            );
        }
        expect(addEventListener).toHaveBeenCalledWith(
            'scroll',
            expect.any(Function),
            { capture: true, passive: true },
        );
        expect(removeEventListener).toHaveBeenCalledWith(
            'scroll',
            expect.any(Function),
            { capture: true },
        );
        expect(addEventListener).toHaveBeenCalledWith(
            'resize',
            expect.any(Function),
            { passive: true },
        );
        expect(removeEventListener).toHaveBeenCalledWith('resize', expect.any(Function));
    });

    it('does not install pointer tracking when tracking is disabled', () => {
        vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
        vi.stubGlobal('cancelAnimationFrame', vi.fn());
        const addEventListener = vi.spyOn(window, 'addEventListener');
        const host = makeHost();

        const face = createFace(host, {
            expression: 'idle',
            track: false,
            blink: false,
            idle: false,
        });
        face.destroy();

        expect(addEventListener).not.toHaveBeenCalledWith(
            'pointermove',
            expect.any(Function),
            expect.anything(),
        );
        expect(addEventListener).not.toHaveBeenCalledWith(
            'pointerdown',
            expect.any(Function),
            expect.anything(),
        );
    });

    it('keeps pointer tracking cleanup correct when track is toggled through set', () => {
        vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
        vi.stubGlobal('cancelAnimationFrame', vi.fn());
        const addEventListener = vi.spyOn(window, 'addEventListener');
        const removeEventListener = vi.spyOn(window, 'removeEventListener');
        const host = makeHost();

        const face = createFace(host, {
            expression: 'idle',
            track: false,
            blink: false,
            idle: false,
        });
        face.set({ track: true });
        face.set({ track: false });
        face.destroy();

        for (const eventName of ['pointermove', 'pointerdown', 'blur']) {
            expect(addEventListener).toHaveBeenCalledWith(
                eventName,
                expect.any(Function),
                expect.anything(),
            );
            expect(removeEventListener).toHaveBeenCalledWith(
                eventName,
                expect.any(Function),
                expect.anything(),
            );
        }
    });

    it('does not reactivate pointer tracking after destroy', () => {
        vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
        vi.stubGlobal('cancelAnimationFrame', vi.fn());
        const addEventListener = vi.spyOn(window, 'addEventListener');
        const host = makeHost();

        const face = createFace(host, {
            expression: 'idle',
            track: false,
            blink: false,
            idle: false,
        });
        face.destroy();
        face.set({ track: true });

        expect(addEventListener).not.toHaveBeenCalledWith(
            'pointermove',
            expect.any(Function),
            expect.anything(),
        );
    });

    it('does not wake a destroyed face through animation APIs', () => {
        const requestFrame = vi.fn(() => 1);
        vi.stubGlobal('requestAnimationFrame', requestFrame);
        vi.stubGlobal('cancelAnimationFrame', vi.fn());
        const host = makeHost();

        const face = createFace(host, {
            expression: 'idle',
            track: false,
            blink: false,
            idle: false,
        });
        face.destroy();

        face.setExpression('happy');
        face.react('bounce');
        face.look(1, 1);

        expect(requestFrame).not.toHaveBeenCalled();
    });

    it('consults prefers-reduced-motion and avoids network activity', () => {
        const requestFrame = vi.fn(() => 1);
        vi.stubGlobal('requestAnimationFrame', requestFrame);
        vi.stubGlobal('cancelAnimationFrame', vi.fn());
        const matchMedia = vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
        vi.stubGlobal('matchMedia', matchMedia);
        const fetchSpy = vi.spyOn(globalThis, 'fetch');
        const host = makeHost();

        const face = createFace(host, {
            expression: 'thinking',
            track: false,
            blink: true,
            idle: true,
        });
        face.destroy();

        expect(matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)');
        expect(requestFrame).not.toHaveBeenCalled();
        expect(fetchSpy).not.toHaveBeenCalled();
        fetchSpy.mockRestore();
    });

    it('pauses and resumes the shared animation loop with visibility', () => {
        const observerInstances = installIntersectionObserver();
        const frameCallbacks = new Map();
        let nextFrameId = 1;
        const requestFrame = vi.fn((callback) => {
            const id = nextFrameId++;
            frameCallbacks.set(id, callback);
            return id;
        });
        const cancelFrame = vi.fn((id) => frameCallbacks.delete(id));
        vi.stubGlobal('requestAnimationFrame', requestFrame);
        vi.stubGlobal('cancelAnimationFrame', cancelFrame);
        const host = makeHost();

        const face = createFace(host, {
            expression: 'idle',
            track: false,
            blink: false,
            idle: true,
        });

        expect(requestFrame).toHaveBeenCalledTimes(1);
        observerInstances[0].callback([{ isIntersecting: false }]);
        expect(cancelFrame).toHaveBeenCalledTimes(1);

        observerInstances[0].callback([{ isIntersecting: true }]);
        expect(requestFrame).toHaveBeenCalledTimes(2);
        face.destroy();
    });

    it('sleeps stable faces without continuous behaviors and wakes for expression changes', () => {
        const frameCallbacks = new Map();
        let nextFrameId = 1;
        const requestFrame = vi.fn((callback) => {
            const id = nextFrameId++;
            frameCallbacks.set(id, callback);
            return id;
        });
        const cancelFrame = vi.fn((id) => frameCallbacks.delete(id));
        vi.stubGlobal('requestAnimationFrame', requestFrame);
        vi.stubGlobal('cancelAnimationFrame', cancelFrame);
        const host = makeHost();
        const face = createFace(host, {
            expression: 'idle',
            track: false,
            blink: false,
            idle: false,
        });

        const runUntilSleep = (startingAt) => {
            let now = startingAt;
            let frames = 0;
            while (frameCallbacks.size && frames < 240) {
                const [id, callback] = frameCallbacks.entries().next().value;
                frameCallbacks.delete(id);
                callback(now);
                now += 1000 / 60;
                frames += 1;
            }
            return { frames, now };
        };

        const initial = runUntilSleep(16.666);
        expect(initial.frames).toBe(0);
        expect(frameCallbacks.size).toBe(0);

        const scheduledBeforeExpression = requestFrame.mock.calls.length;
        face.setExpression('happy');
        expect(requestFrame).toHaveBeenCalledTimes(scheduledBeforeExpression + 1);
        expect(frameCallbacks.size).toBe(1);

        const transition = runUntilSleep(initial.now);
        expect(transition.frames).toBeGreaterThan(0);
        expect(frameCallbacks.size).toBe(0);

        const scheduledBeforeSecondExpression = requestFrame.mock.calls.length;
        face.setExpression('sad');
        expect(requestFrame).toHaveBeenCalledTimes(scheduledBeforeSecondExpression + 1);
        expect(frameCallbacks.size).toBe(1);
        runUntilSleep(transition.now);
        expect(frameCallbacks.size).toBe(0);

        face.destroy();
    });
});
