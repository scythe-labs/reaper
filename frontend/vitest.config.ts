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
    // Vitest's default is 5000ms, which is a developer's machine talking. A loaded CI runner
    // measured 2.3-13x slower than an unloaded local run of the same files, and on one such run
    // the slowest test crossed 5000ms and failed a commit that had nothing wrong with it. The
    // per-test COST is not this setting's job and was not left to it -- `src/test/forms.ts` took
    // the keystroke amplification out of the worst family, which is what actually failed. This
    // covers what optimizing cannot: the two audits that are irreducibly expensive (axe over the
    // General and policy trees, ~4s on that runner) are within a fifth of the old ceiling with no
    // bug under them. 15000ms puts a real hang about three seconds later than a wasted CI run
    // costs anyway. If a test needs this much on an IDLE machine, that is the test's problem to
    // fix, not a number to raise again.
    testTimeout: 15000,
  },
});
