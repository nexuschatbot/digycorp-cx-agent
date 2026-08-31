import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies /api → backend on :8000 (see CLAUDE.md "API contract").
export default defineConfig({
  plugins: [react()],
  server: {
    // Allow the app to be served through a cloudflared tunnel (a *.trycloudflare.com
    // host) so QR-code / phone testing works — the mic (getUserMedia) and the
    // WhatsApp handoff link both need a public HTTPS origin. A leading dot allows
    // any subdomain, so a fresh tunnel URL keeps working without editing this.
    allowedHosts: ['.trycloudflare.com'],
    proxy: {
      // 127.0.0.1, NOT localhost (2026-08-03): Node resolves localhost to ::1
      // first, and the docker compose backend publishes [::]:8000 — so with the
      // stack up, dev traffic silently landed in the CONTAINER (old code, its
      // own DB) instead of the local uvicorn on 127.0.0.1:8000.
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // The voice-call channel (WS /api/call/...) rides the same prefix; without
        // this the dev proxy would silently drop the Upgrade handshake and the
        // call button would fail only in dev — the worst kind of difference.
        ws: true,
      },
      // Vehicle imagery (rule 18): the backend serves seed/media at /media; the
      // dev server proxies it so grounded /media/... URLs load same-origin.
      '/media': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
