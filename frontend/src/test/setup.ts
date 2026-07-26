// SPDX-License-Identifier: AGPL-3.0-or-later
// Registers the jest-dom matchers (toBeDisabled, toBeInTheDocument, ...) with vitest.
import "@testing-library/jest-dom/vitest";

import { afterEach } from "vitest";

// jsdom has no layout, so window.scrollTo is unimplemented and logs a noisy "Not
// implemented" on every call. ModalShell restores the scroll offset with it when a modal
// closes, so make it a quiet no-op here -- nothing in the tests reads a real scroll.
window.scrollTo = () => {};

// A query with no queryFn FAILS the test rather than warning (rule 135).
//
// React Query answers a missing queryFn with a console.error and an error state, so a test that
// mocked "../api" without a function some nested hook reads renders the tree's "we could not
// read it" branch and still passes. A warning cannot be the signal here: vitest's console
// interception drops test console output entirely on some Node versions (on Node 26 a bare
// console.error inside a test prints nothing, while CI on Node 24 prints it), so the one place
// the warning is visible is the CI log nobody reads. 302 of these accumulated behind a green
// suite before anyone looked. Failing the test is the only form of this that survives locally.
let missingQueryFn: string[] = [];
const forwardError = console.error.bind(console);
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string" && args[0].includes("No queryFn was passed")) {
    missingQueryFn.push(args[0]);
  }
  forwardError(...args);
};

afterEach(() => {
  const seen = missingQueryFn;
  missingQueryFn = [];
  if (seen.length === 0) return;
  // The message opens with the query hash, e.g. `[["profile"]]: No queryFn ...`.
  const keys = [...new Set(seen.map((m) => m.slice(0, m.indexOf("]:") + 1)))];
  throw new Error(
    `Ran a query with no queryFn: ${keys.join(", ")}. The mock for "../api" is missing a ` +
      `function a hook in this tree reads, so that query rendered as a failed read. Add it to ` +
      `the mock; src/test/apiFixtures.ts holds the payloads. See rule 135.`,
  );
});
