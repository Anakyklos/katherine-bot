const TEST_REQUEST_ID = '00000000-0000-0000-0000-000000000001';

if (!globalThis.crypto) {
    Object.defineProperty(globalThis, 'crypto', {
        configurable: true,
        value: {},
    });
}

if (typeof globalThis.crypto.randomUUID !== 'function') {
    Object.defineProperty(globalThis.crypto, 'randomUUID', {
        configurable: true,
        value: () => TEST_REQUEST_ID,
    });
}
