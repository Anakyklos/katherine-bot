import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [react()],
    // Relative asset paths so the production build also loads from file://
    // (desktop shell via pywebview, #334). Server/preview modes are
    // unaffected: relative paths resolve identically there.
    base: './',
    // Two build entries plus the smoke page (#334, review B1/B3):
    // * index.html → main-web.jsx → AppWeb (Supabase + AuthPage) —
    //   the web deployment; never loaded by the desktop shell.
    // * desktop.html → main-desktop.jsx → AppDesktop — the desktop
    //   companion entry; its module graph contains no web modules.
    // * desktop-smoke.html mounts the REAL ChatWindow (no fake auth)
    //   for the reproducible shell validation.
    build: {
        rollupOptions: {
            input: {
                main: 'index.html',
                desktop: 'desktop.html',
                desktopSmoke: 'desktop-smoke.html',
            },
        },
    },
    server: {
        port: 3000,
    }
})
