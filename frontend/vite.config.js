import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [react()],
    // Relative asset paths so the production build also loads from file://
    // (desktop shell via pywebview, #334). Server/preview modes are
    // unaffected: relative paths resolve identically there.
    base: './',
    // Second build entry: the desktop smoke page (#334, review B3).
    // It mounts the REAL ChatWindow (no fake auth) for the reproducible
    // shell validation; index.html (the web app + production shell
    // entry) is unchanged by this input.
    build: {
        rollupOptions: {
            input: {
                main: 'index.html',
                desktopSmoke: 'desktop-smoke.html',
            },
        },
    },
    server: {
        port: 3000,
    }
})
