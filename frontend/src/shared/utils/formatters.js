/**
 * Converts a bipolar PAD value (-1..+1) to a display percentage (0..100).
 *
 * Map: -1.0 → 0, 0.0 → 50, +1.0 → 100.
 * Non-finite values return 50 (neutral).
 * Values outside [-1, 1] are clamped before mapping.
 */
export const bipolarToPercent = (val) => {
    if (typeof val !== 'number' || !Number.isFinite(val)) return 50;
    const clamped = Math.max(-1, Math.min(1, val));
    return Math.round((clamped + 1) * 50);
};

/**
 * Converts a unipolar intensity (0..1) to a display percentage (0..100).
 *
 * Map: 0.0 → 0, 0.5 → 50, 1.0 → 100.
 * Non-finite values return 0.
 * Values outside [0, 1] are clamped before mapping.
 */
export const intensityToPercent = (val) => {
    if (typeof val !== 'number' || !Number.isFinite(val)) return 0;
    const clamped = Math.max(0, Math.min(1, val));
    return Math.round(clamped * 100);
};

/**
 * Legacy helper: converts 0..1 to 0..100.
 * Kept for backward compatibility but deprecated in favour of
 * ``bipolarToPercent`` and ``intensityToPercent``.
 */
export const toPercent = (val) => intensityToPercent(val);

const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

/**
 * Validate the public emotion state payload from the backend.
 * Returns the validated payload object, or null if invalid.
 *
 * Validation rules:
 * - Payload must be a non-null object.
 * - schema_version must be 1.
 * - pad must be present and must have finite numeric pleasure/arousal/dominance.
 * - dominant_emotions must be an array (may be empty).
 *   - At most 3 emotions (per the public contract).
 *   - Each name must be a known canonical emotion (in EMOTION_LABELS).
 *   - No duplicate names allowed.
 * - schema_version is preserved in the returned object.
 * - No partial state is ever rendered.
 */
export const validateEmotionState = (payload) => {
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;

    // Validate schema_version
    if (payload.schema_version !== 1) return null;

    // Validate pad
    if (!payload.pad || typeof payload.pad !== 'object' || Array.isArray(payload.pad)) return null;

    const { pleasure, arousal, dominance } = payload.pad;
    if (typeof pleasure !== 'number' || !Number.isFinite(pleasure)) return null;
    if (typeof arousal !== 'number' || !Number.isFinite(arousal)) return null;
    if (typeof dominance !== 'number' || !Number.isFinite(dominance)) return null;
    if ([pleasure, arousal, dominance].some((value) => value < -1 || value > 1)) return null;

    // Validate dominant_emotions (must be array, may be empty)
    if (!Array.isArray(payload.dominant_emotions)) return null;

    // At most 3 emotions
    if (payload.dominant_emotions.length > 3) return null;

    const seenNames = new Set();
    for (let i = 0; i < payload.dominant_emotions.length; i++) {
        const item = payload.dominant_emotions[i];
        if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
        if (typeof item.name !== 'string' || item.name.length === 0) return null;
        // Reject unknown canonical names
        if (!hasOwn(EMOTION_LABELS, item.name)) return null;
        // Reject duplicates
        if (seenNames.has(item.name)) return null;
        seenNames.add(item.name);
        if (typeof item.intensity !== 'number' || !Number.isFinite(item.intensity)) return null;
        if (item.intensity < 0 || item.intensity > 1) return null;
    }

    // Valid mood_label
    if (typeof payload.mood_label !== 'string' || payload.mood_label.length === 0) return null;

    // Valid timestamp
    if (typeof payload.timestamp !== 'number' || !Number.isFinite(payload.timestamp)) return null;
    if (payload.timestamp <= 0) return null;

    // Deep-copy dominant_emotions to avoid sharing references with the outside payload.
    // Each emotion object is reconstructed so mutations to the original payload have
    // no effect on the validated result.
    const clonedEmotions = payload.dominant_emotions.map(
        (item) => ({ name: item.name, intensity: item.intensity })
    );

    return {
        schema_version: 1,
        pad: { pleasure, arousal, dominance },
        mood_label: payload.mood_label,
        dominant_emotions: clonedEmotions,
        timestamp: payload.timestamp,
    };
};

/**
 * Map canonical emotion names to Portuguese display labels.
 */
export const EMOTION_LABELS = Object.freeze({
    joy: 'Alegria',
    sadness: 'Tristeza',
    anger: 'Raiva',
    fear: 'Medo',
    disgust: 'Nojo',
    surprise: 'Surpresa',
    trust: 'Confiança',
    anticipation: 'Antecipação',
    tenderness: 'Ternura',
    guilt: 'Culpa',
    pride: 'Orgulho',
    jealousy: 'Ciúmes',
    gratitude: 'Gratidão',
});

/**
 * Get the display label for a canonical emotion name.
 * Returns null if the name is not a known canonical emotion.
 * This ensures unknown/untrusted payload names are never rendered as raw text.
 */
export const getEmotionLabel = (name) => {
    if (!name || typeof name !== 'string') return null;
    return hasOwn(EMOTION_LABELS, name) ? EMOTION_LABELS[name] : null;
};
