/// <reference types="vitest" />
import { fileURLToPath, URL } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

/**
 * The app is local-first: it talks to the FastAPI process on the same machine,
 * proxied in development so the browser sees one origin and the SSE stream needs
 * no CORS negotiation to stay open.
 *
 * `HOST` says which interface to bind, and loopback is the default: a dev server
 * that put an editorial pipeline on the network merely because someone started
 * it would be the wrong way round. Read here rather than only passed as a flag
 * by `scripts/dev.sh`, so that `HOST=0.0.0.0 npm run dev` does the same thing as
 * the script that wraps it — the two disagreeing is how a server ends up
 * answering on loopback while its operator is waiting for it across the room.
 */
const host = process.env.HOST || '127.0.0.1';
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@contracts': fileURLToPath(new URL('../contracts', import.meta.url)),
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host,
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    restoreMocks: true,
  },
});
