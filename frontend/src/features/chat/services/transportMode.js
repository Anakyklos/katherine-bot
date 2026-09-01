/**
 * Transport-kind predicate (#336).
 *
 * Dependency-free on purpose: the privacy feature (and any other
 * desktop-gated UI) imports this without pulling the web transport's
 * Axios/Supabase module graph.
 */

/**
 * True iff the transport is the desktop (bridge) branch.
 * Callers use this to gate desktop-only UI (privacy panel).
 */
export function isDesktopTransport(transport) {
    return transport?.mode === 'desktop';
}
