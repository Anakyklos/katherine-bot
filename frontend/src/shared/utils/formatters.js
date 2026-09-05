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

const readOwnDataProperty = (object, key) => {
    if (!object || typeof object !== 'object') return { present: false, value: undefined };

    try {
        const descriptor = Object.getOwnPropertyDescriptor(object, key);
        if (!descriptor || !hasOwn(descriptor, 'value')) {
            return { present: false, value: undefined };
        }
        return { present: true, value: descriptor.value };
    } catch {
        return { present: false, value: undefined };
    }
};

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

    // JSON contract fields must be own data properties. This rejects inherited
    // values and accessors that could otherwise masquerade as validated data.
    const schemaVersion = readOwnDataProperty(payload, 'schema_version');
    if (!schemaVersion.present || schemaVersion.value !== 1) return null;

    // Validate pad
    const padResult = readOwnDataProperty(payload, 'pad');
    const pad = padResult.value;
    if (!padResult.present || !pad || typeof pad !== 'object' || Array.isArray(pad)) return null;

    const pleasureResult = readOwnDataProperty(pad, 'pleasure');
    const arousalResult = readOwnDataProperty(pad, 'arousal');
    const dominanceResult = readOwnDataProperty(pad, 'dominance');
    if (!pleasureResult.present || !arousalResult.present || !dominanceResult.present) return null;

    const { value: pleasure } = pleasureResult;
    const { value: arousal } = arousalResult;
    const { value: dominance } = dominanceResult;
    if (typeof pleasure !== 'number' || !Number.isFinite(pleasure)) return null;
    if (typeof arousal !== 'number' || !Number.isFinite(arousal)) return null;
    if (typeof dominance !== 'number' || !Number.isFinite(dominance)) return null;
    if ([pleasure, arousal, dominance].some((value) => value < -1 || value > 1)) return null;

    // Validate dominant_emotions (must be array, may be empty)
    const dominantEmotionsResult = readOwnDataProperty(payload, 'dominant_emotions');
    const dominantEmotions = dominantEmotionsResult.value;
    if (!dominantEmotionsResult.present || !Array.isArray(dominantEmotions)) return null;

    // At most 3 emotions
    if (dominantEmotions.length > 3) return null;

    const seenNames = new Set();
    const validatedEmotions = [];
    for (let i = 0; i < dominantEmotions.length; i++) {
        const itemResult = readOwnDataProperty(dominantEmotions, i);
        const item = itemResult.value;
        if (!itemResult.present) return null;
        if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
        const nameResult = readOwnDataProperty(item, 'name');
        const intensityResult = readOwnDataProperty(item, 'intensity');
        if (!nameResult.present || !intensityResult.present) return null;

        const { value: name } = nameResult;
        const { value: intensity } = intensityResult;
        if (typeof name !== 'string' || name.length === 0) return null;
        // Reject unknown canonical names
        if (!hasOwn(EMOTION_LABELS, name)) return null;
        // Reject duplicates
        if (seenNames.has(name)) return null;
        seenNames.add(name);
        if (typeof intensity !== 'number' || !Number.isFinite(intensity)) return null;
        if (intensity < 0 || intensity > 1) return null;
        validatedEmotions.push({ name, intensity });
    }

    // Valid mood_label
    const moodLabelResult = readOwnDataProperty(payload, 'mood_label');
    const moodLabel = moodLabelResult.value;
    if (!moodLabelResult.present || typeof moodLabel !== 'string' || moodLabel.length === 0) return null;

    // Valid timestamp
    const timestampResult = readOwnDataProperty(payload, 'timestamp');
    const timestamp = timestampResult.value;
    if (!timestampResult.present || typeof timestamp !== 'number' || !Number.isFinite(timestamp)) return null;
    if (timestamp <= 0) return null;

    // Deep-copy dominant_emotions to avoid sharing references with the outside payload.
    // Each emotion object is reconstructed so mutations to the original payload have
    // no effect on the validated result.
    const clonedEmotions = validatedEmotions.map(({ name, intensity }) => ({ name, intensity }));

    return {
        schema_version: 1,
        pad: { pleasure, arousal, dominance },
        mood_label: moodLabel,
        dominant_emotions: clonedEmotions,
        timestamp,
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
