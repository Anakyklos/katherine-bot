/* global process */
process.env.NODE_ENV = 'test';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    KATHERINE_FACE_EXPRESSIONS,
    STRONG_EXPRESSION_THRESHOLD,
    selectKatherineFaceState,
} from '../src/features/katherine-face/faceState.js';

const emotionState = (dominant_emotions, overrides = {}) => ({
    schema_version: 1,
    mood_label: 'NEUTRA',
    pad: { pleasure: 0, arousal: 0, dominance: 0 },
    dominant_emotions,
    timestamp: 1700000000,
    ...overrides,
});

const stateFor = (name, intensity = 0.2, overrides = {}) =>
    emotionState([{ name, intensity }], overrides);

const approvedMappings = {
    joy: ['happy', 'joy'],
    sadness: ['sad'],
    anger: ['annoyed', 'angry'],
    fear: ['worried', 'scared'],
    disgust: ['annoyed'],
    surprise: ['surprised'],
    trust: ['content'],
    anticipation: ['curious'],
    tenderness: ['content', 'happy'],
    guilt: ['worried'],
    pride: ['content', 'happy'],
    jealousy: ['worried'],
    gratitude: ['content', 'happy'],
};

// ─── contract surface ────────────────────────────────────────────────────────

test('exports the single approved strong-expression threshold', () => {
    assert.strictEqual(STRONG_EXPRESSION_THRESHOLD, 0.70);
});

test('exports only the approved expression vocabulary', () => {
    assert.deepStrictEqual(
        [...KATHERINE_FACE_EXPRESSIONS].sort(),
        [
            'angry', 'annoyed', 'content', 'curious', 'happy', 'idle',
            'joy', 'sad', 'scared', 'surprised', 'thinking', 'worried',
        ].sort(),
    );
});

// ─── canonical emotion mapping ───────────────────────────────────────────────

test('maps all 13 canonical emotions to approved expressions', () => {
    for (const [name, [moderate]] of Object.entries(approvedMappings)) {
        assert.strictEqual(
            selectKatherineFaceState({
                emotionState: stateFor(name),
                isLoading: false,
            }).expression,
            moderate,
            `expected ${name} to map to ${moderate}`,
        );
    }
});

test('uses the strong variant for every configured moderate/strong pair', () => {
    for (const [name, variants] of Object.entries(approvedMappings)) {
        if (variants.length !== 2) continue;
        assert.strictEqual(
            selectKatherineFaceState({
                emotionState: stateFor(name, STRONG_EXPRESSION_THRESHOLD),
                isLoading: false,
            }).expression,
            variants[1],
            `expected ${name} to use its strong variant at the threshold`,
        );
    }
});

test('uses the moderate variant immediately below the threshold', () => {
    for (const [name, variants] of Object.entries(approvedMappings)) {
        if (variants.length !== 2) continue;
        assert.strictEqual(
            selectKatherineFaceState({
                emotionState: stateFor(name, STRONG_EXPRESSION_THRESHOLD - 0.001),
                isLoading: false,
            }).expression,
            variants[0],
            `expected ${name} to remain moderate below the threshold`,
        );
    }
});

test('uses the strong variant immediately above the threshold', () => {
    for (const [name, variants] of Object.entries(approvedMappings)) {
        if (variants.length !== 2) continue;
        assert.strictEqual(
            selectKatherineFaceState({
                emotionState: stateFor(name, STRONG_EXPRESSION_THRESHOLD + 0.001),
                isLoading: false,
            }).expression,
            variants[1],
            `expected ${name} to be strong above the threshold`,
        );
    }
});

// ─── dominance and safety ────────────────────────────────────────────────────

test('selects the highest-intensity dominant emotion deterministically', () => {
    const input = emotionState([
        { name: 'joy', intensity: 0.4 },
        { name: 'sadness', intensity: 0.8 },
    ]);
    assert.deepStrictEqual(
        selectKatherineFaceState({ emotionState: input, isLoading: false }),
        { expression: 'sad' },
    );
});

test('keeps the first emotion when intensities tie', () => {
    const input = emotionState([
        { name: 'joy', intensity: 0.8 },
        { name: 'anger', intensity: 0.8 },
    ]);
    assert.deepStrictEqual(
        selectKatherineFaceState({ emotionState: input, isLoading: false }),
        { expression: 'joy' },
    );
});

test('mood_label and PAD changes do not control the expression', () => {
    const input = stateFor('joy', 0.2, {
        mood_label: 'TRISTE / ERRO / TIMEOUT',
        pad: { pleasure: -1, arousal: -1, dominance: -1 },
    });
    assert.strictEqual(
        selectKatherineFaceState({ emotionState: input, isLoading: false }).expression,
        'happy',
    );
});

test('unsupported bfce expressions never leave the mapper', () => {
    for (const name of ['love', 'smug', 'sly', 'bored', 'sleepy', 'sleep', 'lovesmugly', 'suspicious']) {
        assert.deepStrictEqual(
            selectKatherineFaceState({
                emotionState: stateFor(name, 1),
                isLoading: false,
            }),
            { expression: 'idle' },
            `unsupported ${name} must fail closed to idle`,
        );
    }
});

test('technical failures and inactivity-like inputs stay neutral', () => {
    const technicalInput = {
        error: new Error('provider failed'),
        timedOut: true,
        reconnecting: true,
        messageCount: 99,
        lastActivityAt: 1,
    };
    assert.deepStrictEqual(
        selectKatherineFaceState({ emotionState: technicalInput, isLoading: false }),
        { expression: 'idle' },
    );
});

test('absent or invalid emotion state falls back to idle', () => {
    for (const input of [
        undefined,
        null,
        {},
        { dominant_emotions: [] },
        emotionState([{ name: 'unknown', intensity: 0.9 }]),
        emotionState([{ name: 'joy', intensity: Number.NaN }]),
        emotionState([{ name: 'joy', intensity: 1.01 }]),
        emotionState([{ name: 'joy', intensity: 0.8 }, { name: 'bad', intensity: 0.2 }]),
    ]) {
        assert.deepStrictEqual(
            selectKatherineFaceState({ emotionState: input, isLoading: false }),
            { expression: 'idle' },
        );
    }
});

// ─── UI-only loading and purity ──────────────────────────────────────────────

test('loading projects thinking without mutating emotionState', () => {
    const input = stateFor('sadness', 0.9);
    const snapshot = structuredClone(input);

    assert.deepStrictEqual(
        selectKatherineFaceState({ emotionState: input, isLoading: true }),
        { expression: 'thinking' },
    );
    assert.deepStrictEqual(input, snapshot);
});

test('the same input always produces the same output', () => {
    const input = emotionState([
        { name: 'pride', intensity: 0.71 },
        { name: 'gratitude', intensity: 0.7 },
    ]);
    const first = selectKatherineFaceState({ emotionState: input, isLoading: false });
    const second = selectKatherineFaceState({ emotionState: input, isLoading: false });

    assert.deepStrictEqual(first, second);
});

test('loading takes precedence only when it is strictly true', () => {
    const input = stateFor('joy', 0.8);
    assert.strictEqual(
        selectKatherineFaceState({ emotionState: input, isLoading: 1 }).expression,
        'joy',
    );
    assert.strictEqual(
        selectKatherineFaceState({ emotionState: input, isLoading: false }).expression,
        'joy',
    );
    assert.strictEqual(
        selectKatherineFaceState({ emotionState: input, isLoading: true }).expression,
        'thinking',
    );
});
