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
      // The scan runs as a detached background job; the browser polls GET /api/scan/status
      // for its progress (see api/scan.py). This is a plain same-origin proxy of /api to the
      // Python app -- nothing here is SSE- or websocket-specific.
      "/api": {
        target: "http://127.0.0.1:8420",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    // Source maps ship, on purpose (PR-11). Reaper runs on servers we will never see, so an
    // operator's console is the only debugger we get, and a stack trace through minified
    // chunk names is not a bug report. There is nothing to protect by withholding them: the
    // sources are AGPL and published, and no build-time value reaches the bundle (there is
    // no `import.meta.env` use in src/ -- every setting comes from the API at runtime).
    // The cost is ~2 MB of .map files in the image; browsers fetch them only when devtools
    // is open, so a normal first load transfers the `sourceMappingURL` comment and nothing
    // else. Prefer "hidden" over false if that ever needs to change -- it keeps the maps
    // for CI without pointing browsers at them.
    sourcemap: true,
  },
});
