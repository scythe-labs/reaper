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
//
// A state update outside act(...) fails the test for the same reason: it says the test asserted
// on a moment the component had already left, because something it never awaited settled behind
// the assertions. A promise a test forgot to await resolves as the body returns, every run.
//
// It judges EVERY such update, with no exemption for the framework (rule 136). There was one,
// for React Query's `setTimeout(0)` notification, on the reading that its timing was the
// machine's rather than the test's -- and it was wrong. What it tolerated had two fixable
// causes: `refetchOnWindowFocus` refetching the queue's own reads whenever user-event clicked
// something focusable (`src/test/queryClient.ts`), and a mid-test `await import(...)` of
// user-event, which is a bare await outside act for a fetch to land in. The tell was that it
// reproduced on one test, five runs out of five; a race does not do that.
//
// An exempted warning still PRINTED, so a green CI run carried an act warning nobody could act
// on -- the same "warning nobody reads" this file exists to delete, now with the gate's own
// blessing. Nothing here warns: it fails, or it has nothing to say.
let missingQueryFn: string[] = [];
let outsideAct: string[] = [];
const forwardError = console.error.bind(console);
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string") {
    if (args[0].includes("No queryFn was passed")) missingQueryFn.push(args[0]);
    // "An update to %s inside a test was not wrapped in act(...)", the component name second.
    if (args[0].includes("was not wrapped in act(")) {
      outsideAct.push(String(args[1] ?? "a component"));
    }
  }
  forwardError(...args);
};

afterEach(() => {
  const queries = missingQueryFn;
  const unacted = outsideAct;
  missingQueryFn = [];
  outsideAct = [];
  if (queries.length > 0) {
    // The message opens with the query hash, e.g. `[["profile"]]: No queryFn ...`.
    const keys = [...new Set(queries.map((m) => m.slice(0, m.indexOf("]:") + 1)))];
    throw new Error(
      `Ran a query with no queryFn: ${keys.join(", ")}. The mock for "../api" is missing a ` +
        `function a hook in this tree reads, so that query rendered as a failed read. Add it to ` +
        `the mock; src/test/apiFixtures.ts holds the payloads. See rule 135.`,
    );
  }
  if (unacted.length > 0) {
    const names = [...new Set(unacted)];
    throw new Error(
      `Updated state outside act(...): ${names.join(", ")}. Something this test never awaited ` +
        `settled after its last act() returned, so the assertions above ran on a moment that ` +
        `had already passed. Await it inside act (\`await act(async () => ...)\`), or hold it ` +
        `in flight so the moment the test asserts is the one it means. See rule 136.`,
    );
  }
});
