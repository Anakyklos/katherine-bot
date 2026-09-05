import { validateEmotionState } from '../../shared/utils/formatters.js';

export const STRONG_EXPRESSION_THRESHOLD = 0.70;

export const KATHERINE_FACE_EXPRESSIONS = Object.freeze([
    'idle',
    'thinking',
    'happy',
    'joy',
    'sad',
    'annoyed',
    'angry',
    'worried',
    'scared',
    'surprised',
    'content',
    'curious',
]);

const EXPRESSION_BY_EMOTION = Object.freeze({
    joy: Object.freeze({ moderate: 'happy', strong: 'joy' }),
    sadness: Object.freeze({ moderate: 'sad' }),
    anger: Object.freeze({ moderate: 'annoyed', strong: 'angry' }),
    fear: Object.freeze({ moderate: 'worried', strong: 'scared' }),
    disgust: Object.freeze({ moderate: 'annoyed' }),
    surprise: Object.freeze({ moderate: 'surprised' }),
    trust: Object.freeze({ moderate: 'content' }),
    anticipation: Object.freeze({ moderate: 'curious' }),
    tenderness: Object.freeze({ moderate: 'content', strong: 'happy' }),
    guilt: Object.freeze({ moderate: 'worried' }),
    pride: Object.freeze({ moderate: 'content', strong: 'happy' }),
    jealousy: Object.freeze({ moderate: 'worried' }),
    gratitude: Object.freeze({ moderate: 'content', strong: 'happy' }),
});

const idle = () => ({ expression: 'idle' });

/**
 * Converts the validated public emotion response into a safe visual state.
 *
 * The public response is validated again at this boundary so malformed data
 * fails closed. Only the structured dominant emotion fields select an
 * expression. Loading is ephemeral UI state and never changes emotionState.
 */
export const selectKatherineFaceState = ({ emotionState, isLoading } = {}) => {
    if (isLoading === true) return { expression: 'thinking' };

    const validated = validateEmotionState(emotionState);
    if (!validated || validated.dominant_emotions.length === 0) return idle();

    const dominant = validated.dominant_emotions.reduce((current, candidate) => {
        if (!current || candidate.intensity > current.intensity) return candidate;
        return current;
    }, null);
    const variants = EXPRESSION_BY_EMOTION[dominant.name];
    if (!variants) return idle();

    const expression = dominant.intensity >= STRONG_EXPRESSION_THRESHOLD
        ? variants.strong ?? variants.moderate
        : variants.moderate;

    return KATHERINE_FACE_EXPRESSIONS.includes(expression)
        ? { expression }
        : idle();
};
