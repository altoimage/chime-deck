import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [svelte()],
  // base stays at the Vite default '/'. A relative base ('./') resolves fine
  // for '/' and single-segment routes, but breaks multi-segment SPA routes
  // like /settings/entities — './assets/x.js' would resolve to
  // /settings/assets/x.js. The backend is root-mounted (no URL prefix), so
  // root-absolute asset URLs resolve from the server root no matter where
  // the dist tree lives.
  build: { sourcemap: false, emptyOutDir: true },
  // Dev only: forward the API and the websocket to the FastAPI backend, which
  // listens on 127.0.0.1:8080 by default, so `npm run dev` talks to the real
  // app instead of the Vite dev server. Nothing here affects the build.
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080' },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true }
    }
  }
});
