// SPDX-License-Identifier: AGPL-3.0-or-later
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Component tests run in jsdom with Testing Library. Kept separate from vite.config.ts
// so the dev server and the build stay exactly as they were; `npm run test` is the
// entry point, and CI runs it beside the build.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    // Testing Library's between-test DOM cleanup registers itself via the global
    // afterEach, so globals stay on; tests still import their vitest helpers
    // explicitly for tsc's sake.
    globals: true,
  },
});
