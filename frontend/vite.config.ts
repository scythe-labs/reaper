// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the SPA is served by Vite and /api is proxied to the Python app.
// In production `vite build` emits ./dist, which FastAPI serves via app.frontend().
// Both paths therefore speak to the API at a *same-origin* /api, so there is no CORS
// configuration anywhere -- and nothing to accidentally loosen.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The scan endpoint is a long-lived SSE stream; the proxy passes text/event-stream
      // through unbuffered, so progress events arrive as they are emitted rather than in
      // one lump when the scan finishes.
      "/api": {
        target: "http://127.0.0.1:8420",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
