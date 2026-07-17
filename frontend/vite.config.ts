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
      // Scan progress is POLLED from /api/scan/status (there is no SSE stream); the
      // proxy just forwards /api to the backend unchanged.
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
