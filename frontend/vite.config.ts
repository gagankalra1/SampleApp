import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * In production the built assets are served by nginx, which also reverse
 * proxies /api/* to the in-cluster services (see nginx.conf). The dev server
 * mirrors those same proxy paths so that `npm run dev` against locally-run
 * services behaves identically to the deployed stack — no separate base URL
 * and no CORS configuration in either place.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Loopback only. The dev server has no auth and should never be reachable
    // from another machine.
    host: '127.0.0.1',
    proxy: {
      '/api/catalog': { target: 'http://127.0.0.1:8081', changeOrigin: true, rewrite: (p) => p.replace(/^\/api\/catalog/, '') },
      '/api/inventory': { target: 'http://127.0.0.1:8082', changeOrigin: true, rewrite: (p) => p.replace(/^\/api\/inventory/, '') },
      '/api/cart': { target: 'http://127.0.0.1:8083', changeOrigin: true, rewrite: (p) => p.replace(/^\/api\/cart/, '') },
      '/api/orders': { target: 'http://127.0.0.1:8084', changeOrigin: true, rewrite: (p) => p.replace(/^\/api\/orders/, '') },
      '/api/checkout': { target: 'http://127.0.0.1:8086', changeOrigin: true, rewrite: (p) => p.replace(/^\/api\/checkout/, '') },
      '/api/support': { target: 'http://127.0.0.1:8087', changeOrigin: true, rewrite: (p) => p.replace(/^\/api\/support/, '') },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
})
