import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [react()],
    // Relative asset paths so the production build also loads from file://
    // (desktop shell via pywebview, #334). Server/preview modes are
    // unaffected: relative paths resolve identically there.
    base: './',
    server: {
        port: 3000,
    }
})
