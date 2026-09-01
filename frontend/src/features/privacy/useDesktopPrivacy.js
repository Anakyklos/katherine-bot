import { useState, useCallback, useRef } from 'react';
import { isDesktopTransport } from '../chat/services/transportMode';

/**
 * Desktop-only privacy operations hook (#336, T020).
 *
 * Wraps the transport's `runPrivacyOp` with:
 * - single-flight (one op at a time; a second call while pending is a
 *   no-op, mirroring the chat send guard);
 * - a stable `{ok, ...}` result shape: `ok: true` + the applied result
 *   fields on success, `ok: false` + the sanitized message on failure
 *   (ChatError message from the stable bridge mapping — never raw
 *   internals);
 * - no throw: callers render, they do not try/catch.
 *
 * Returns null-ish when the transport is not the desktop branch so the
 * panel can decide not to render in web mode.
 */
export function useDesktopPrivacy(transport) {
    const [pending, setPending] = useState(null);
    const [lastResult, setLastResult] = useState(null);
    const inFlight = useRef(false);

    const runOp = useCallback(async (op) => {
        if (!isDesktopTransport(transport) || inFlight.current) {
            return { ok: false, message: '' };
        }
        inFlight.current = true;
        setPending(op);
        try {
            const result = await transport.runPrivacyOp(op);
            const payload = { ok: true, ...result };
            setLastResult(payload);
            return payload;
        } catch (error) {
            const message = (
                error?.name === 'ChatError' && typeof error.message === 'string'
            ) ? error.message : 'Erro ao concluir a operação. Tente novamente.';
            const payload = { ok: false, message };
            setLastResult(payload);
            return payload;
        } finally {
            inFlight.current = false;
            setPending(null);
        }
    }, [transport]);

    return { runOp, pending, lastResult };
}
