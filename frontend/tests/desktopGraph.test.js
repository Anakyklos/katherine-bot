/**
 * Desktop bundle graph isolation (#336, review blocker 1).
 *
 * Mechanical proof that the DESKTOP bundle contains no web modules:
 * after `vite build`, every chunk reachable from `desktop.html`
 * (traced through script tags and the chunks' own relative js
 * references) must not be or reference the web-only chunks
 * (`chatService-*.js`, `supabaseClient-*.js`) that carry Axios and
 * Supabase.
 *
 * This fails the suite if anyone reintroduces a static import from the
 * desktop graph into those modules (e.g. `import ... from chatService`
 * inside a desktop-reachable file).
 *
 * Runs as a node test (`node --test`) because it needs the built
 * `dist/` artifacts; vitest covers the component-level behavior.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = join(__dirname, '..');
const DIST = join(FRONTEND_ROOT, 'dist');

// Self-contained CI: this suite needs the built dist. When it is
// absent (fresh checkout, tests run before `npm run build`), build
// once here — deterministic, same vite config as the Build step.
if (!existsSync(join(DIST, 'desktop.html'))) {
    execFileSync('npm', ['run', 'build'], {
        cwd: FRONTEND_ROOT,
        stdio: 'inherit',
    });
}

/**
 * Filenames of web-only chunks that must never be referenced (static
 * import, dynamic-import map, preload hint) from desktop-reachable
 * chunks. A *filename* check is precise: the shared runtime chunk is
 * legitimately shared, but the desktop graph must never point at the
 * web chunks `chatService-*.js` / `supabaseClient-*.js`.
 */
const WEB_CHUNK_BASENAMES = ['chatService', 'supabaseClient'];

/**
 * Parse the built desktop.html and return the JS chunk files it
 * references (script src) plus the CSS files (link href) — the entire
 * asset surface the desktop entry loads.
 */
function desktopEntryAssets() {
    const htmlPath = join(DIST, 'desktop.html');
    assert.ok(existsSync(htmlPath), 'dist/desktop.html not found — run `npm run build` first');
    const html = readFileSync(htmlPath, 'utf8');
    const assets = [];
    for (const match of html.matchAll(/<script[^>]+src="([^"]+)"/g)) {
        assets.push(match[1].replace(/^\.?\//, ''));
    }
    for (const match of html.matchAll(/<link[^>]+href="([^"]+)"/g)) {
        assets.push(match[1].replace(/^\.?\//, ''));
    }
    return { html, assets };
}

test('dist has the desktop entry built', () => {
    const { html, assets } = desktopEntryAssets();
    assert.ok(assets.length > 0, 'desktop.html references no scripts — build misconfigured');
    for (const asset of assets) {
        assert.ok(
            existsSync(join(DIST, asset)),
            `desktop.html references ${asset} but it does not exist in dist/`,
        );
    }
    assert.ok(!/supabase|AuthPage/i.test(html), 'desktop.html itself must not mention web modules');
});

test('desktop entry chunk graph contains no web modules', () => {
    const { assets } = desktopEntryAssets();

    // Walk the desktop chunks + every relative `./xxx.js` reference
    // inside them (static imports, dynamic-import dependency maps,
    // preload hints). Every referenced chunk must exist, and none may
    // be a web-only chunk (chatService/supabaseClient).
    const seen = new Set();
    const queue = [...assets];
    const scanned = [];

    while (queue.length > 0) {
        const rel = queue.shift();
        if (seen.has(rel)) {
            continue;
        }
        if (rel.endsWith('.css')) {
            seen.add(rel);
            continue;
        }
        seen.add(rel);
        const abs = join(DIST, rel);
        assert.ok(existsSync(abs), `chunk ${rel} referenced but missing from dist/`);
        const content = readFileSync(abs, 'utf8');
        scanned.push(rel);

        for (const base of WEB_CHUNK_BASENAMES) {
            assert.ok(
                !rel.startsWith(`assets/${base}-`),
                `desktop-reachable chunk ${rel} is a web-only chunk (${base})`,
            );
            const referenced = new RegExp(
                `["'()]\\./assets/${base}-[^"'\\)]+[."']`,
            );
            assert.ok(
                !referenced.test(content),
                `desktop chunk ${rel} references the web-only chunk ${base}`,
            );
        }

        // Follow every relative js reference (chunk graph + dynamic
        // import maps): the desktop graph must stay closed under it.
        for (const match of content.matchAll(/["'](\\.\/?[^"']+\\.js)["']/g)) {
            let target = match[1].replace(/^\.?\//, '');
            if (!seen.has(target)) {
                queue.push(target);
            }
        }
    }

    assert.ok(scanned.length > 0, 'no desktop chunks were scanned — build misconfigured');
});

test('web entry still builds separately (index.html unaffected)', () => {
    const indexPath = join(DIST, 'index.html');
    assert.ok(existsSync(indexPath), 'dist/index.html (web deployment) missing from build');
    // The web entry must exist and be distinct from the desktop entry.
    const webHtml = readFileSync(indexPath, 'utf8');
    const desktopHtml = readFileSync(join(DIST, 'desktop.html'), 'utf8');
    assert.notEqual(webHtml, desktopHtml);
});

test('source tree: desktop root imports no web modules (pre-build guard)', () => {
    // Belt and suspenders: even without a build, the desktop SOURCE
    // graph must be free of the web modules. This catches the
    // regression in dev (`vite dev`) where no dist/ exists yet.
    const forbiddenImports = [
        /from\s+['"].*supabaseClient/,
        /from\s+['"].*apiClient/,
        /from\s+['"].*chatService/,
        /from\s+['"].*AuthPage/,
    ];
    const desktopSources = [
        'src/main-desktop.jsx',
        'src/AppDesktop.jsx',
        'src/features/chat/components/ChatWindow.jsx',
        'src/features/chat/hooks/useChat.js',
        'src/features/chat/services/chatTransport.js',
        'src/features/chat/services/transportMode.js',
        'src/features/chat/services/chatError.js',
        'src/lib/desktopBridge.js',
        'src/lib/runtimeMode.js',
    ];
    for (const rel of desktopSources) {
        const abs = join(FRONTEND_ROOT, rel);
        assert.ok(existsSync(abs), `expected desktop source ${rel} to exist`);
        const content = readFileSync(abs, 'utf8');
        for (const pattern of forbiddenImports) {
            assert.ok(
                !pattern.test(content),
                `desktop source ${rel} imports a web module (pattern ${pattern})`,
            );
        }
    }
});

test('no stray build inputs reference the removed unified App', () => {
    // App.jsx/main.jsx were replaced by AppDesktop/AppWeb +
    // main-desktop/main-web; nothing may reference the old names.
    const entries = readdirSync(join(FRONTEND_ROOT), { withFileTypes: true })
        .filter((e) => e.isFile() && e.name.endsWith('.html'))
        .map((e) => e.name);
    for (const html of entries) {
        const content = readFileSync(join(FRONTEND_ROOT, html), 'utf8');
        assert.ok(
            !content.includes('/src/App.jsx') && !content.includes('/src/main.jsx'),
            `${html} still references the removed unified App/main entry`,
        );
    }
});
