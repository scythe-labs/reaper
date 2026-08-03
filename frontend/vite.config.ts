// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// This file runs in Node, but `tsconfig.json` pins `types` to `vite/client` on purpose: the
// SPA is a browser bundle, and widening them would let `src/` reach a Node global that is not
// there at runtime. So declare the one field this config reads rather than widening it.
declare const process: { env: Record<string, string | undefined> };

// The API port the dev proxy points at, following `scripts/dev-local.sh`'s REAPER_PORT (which
// that script also passes to uvicorn, and now into this process). Hardcoding it broke the
// documented way to run a SECOND instance -- a parallel agent session, a PR test-drive: the API
// came up on the new port while every /api call through Vite answered 502, so the UI was a dead
// shell that read as a crashed backend. REAPER_WEB_PORT worked (dev-local.sh passes it to Vite
// as --port), so the pair read as supported with only half of it wired. Both halves move
// together or neither does; the default is the one documented in CLAUDE.md and README.md.
const apiPort = process.env.REAPER_PORT ?? "8420";

// In development the SPA is served by Vite and /api is proxied to the Python app.
// In production `vite build` emits ./dist, which FastAPI serves via app.frontend().
// Both paths therefore speak to the API at a *same-origin* /api, so there is no CORS
// configuration anywhere -- and nothing to accidentally loosen.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Fail on a taken port instead of quietly taking the next one. Vite's default is to slide
    // to 5174 and say so only in its own log, so a second launcher that also omits REAPER_PORT
    // came up on a port nobody was told about and proxied /api to the FIRST instance's API:
    // a UI serving this tree's code over someone else's backend, every request answering 200.
    // An agent then verifies a change end to end against a build that does not contain it
    // (#239). This is the one file every launcher shares -- .claude/launch.json, the verify
    // skill's manual boot, README.md -- so failing closed here closes all of them at once,
    // and `tests/test_repo_hygiene.py` keeps a launcher from overriding it back.
    strictPort: true,
    proxy: {
      // The scan runs as a detached background job; the browser polls GET /api/scan/status
      // for its progress (see api/scan.py). This is a plain same-origin proxy of /api to the
      // Python app -- nothing here is SSE- or websocket-specific.
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
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
