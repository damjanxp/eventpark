/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The API is served from the SAME origin as the app (docs/02 §13, ADR-029): in dev, Vite forwards these paths to
// the API on :8000; on AWS, CloudFront forwards them to the ALB. Same origin = no CORS, and the SameSite=Strict
// refresh cookie is sent. Point API_URL elsewhere to use e.g. the Caddy load balancer: API_URL=http://localhost:8080
const api = process.env.API_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': new URL('./src', import.meta.url).pathname } },
  server: {
    port: 5173,
    proxy: {
      '/admin/v1': api,
      '/public/v1': api,
      '/ws/v1': { target: api.replace(/^http/, 'ws'), ws: true },
      '/healthz': api,
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
