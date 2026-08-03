// SPDX-License-Identifier: AGPL-3.0-or-later
// Registers the jest-dom matchers (toBeDisabled, toBeInTheDocument, ...) with vitest.
import "@testing-library/jest-dom/vitest";

import { afterEach } from "vitest";

// jsdom has no layout, so window.scrollTo is unimplemented and logs a noisy "Not
// implemented" on every call. ModalShell restores the scroll offset with it when a modal
// closes, so make it a quiet no-op here -- nothing in the tests reads a real scroll.
window.scrollTo = () => {};

// Same reason, one layer down: jsdom does not implement Element.scrollIntoView AT ALL, so the
// property is `undefined` rather than a no-op and calling it throws. Four components scroll a
// keyboard target into view (the suggester's active option, the docs anchor, the policy
// warning anchors), and each would take its whole test file down with a TypeError. Assigned
// rather than spied, because `vi.spyOn` cannot wrap a method that does not exist.
Element.prototype.scrollIntoView = () => {};

// Pay jsdom's first `getComputedStyle` here, where nothing is timing it.
//
// The first such call in a jsdom instance costs ~52ms building the CSSOM, and every
// `*ByRole` query makes one: `queryAllByRole` filters inaccessible elements, which reads
// computed visibility. So whichever role query runs first in a file paid that ~52ms, and
// when that query was a test's first `await findByRole(...)` the cost landed INSIDE
// `findBy`'s fixed 1000ms budget, spending 5% of the margin before the read it is waiting
// for had a chance to land. Ten test files opened on a role query that way.
//
// Measured on a four-element DOM, so this is not about tree size and not about matching an
// accessible name: a bare `getAllByRole("button")` costs 60ms cold and 0.6ms warm, and one
// `getComputedStyle` beforehand drops the cold query to 7.8ms. See docs/LEARNINGS.md.
//
// Vitest gives each test file its own module registry and jsdom, so this runs per file,
// which is exactly the granularity the cost is paid at. It leaves no DOM behind (rule 133).
//
// The property is read, not just the call made, because jsdom builds the CSSOM on first
// access rather than on the call -- and the value is then asserted, so a jsdom that stops
// answering fails here loudly instead of leaving this line silently warming nothing.
const warm = document.createElement("div");
document.body.appendChild(warm);
const warmedVisibility = window.getComputedStyle(warm).visibility;
warm.remove();
if (warmedVisibility === "") {
  throw new Error(
    "jsdom returned no computed visibility for a plain div, so this file is no longer " +
      "paying the first-getComputedStyle cost it exists to pay. Every test file's first " +
      "*ByRole query is back to spending ~52ms of its findBy budget on it.",
  );
}

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
let duplicateKeys: string[] = [];
const forwardError = console.error.bind(console);
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string") {
    if (args[0].includes("No queryFn was passed")) missingQueryFn.push(args[0]);
    // "An update to %s inside a test was not wrapped in act(...)", the component name second.
    if (args[0].includes("was not wrapped in act(")) {
      outsideAct.push(String(args[1] ?? "a component"));
    }
    // "Encountered two children with the same key, `%s`." React 19 still renders both, so
    // nothing about the page looks wrong and only this line ever said so -- which is the
    // shape rule 135 exists to delete. The reconciliation guarantee rule 19 asks for is
    // what is actually lost, and it is lost silently.
    if (args[0].includes("Encountered two children with the same key")) {
      duplicateKeys.push(String(args[1] ?? "an unnamed key"));
    }
  }
  forwardError(...args);
};

afterEach(() => {
  const queries = missingQueryFn;
  const unacted = outsideAct;
  const dupes = duplicateKeys;
  missingQueryFn = [];
  outsideAct = [];
  duplicateKeys = [];
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
  if (dupes.length > 0) {
    const keys = [...new Set(dupes)];
    throw new Error(
      `Rendered siblings sharing a key: ${keys.join(", ")}. Both children still paint, so no ` +
        `assertion here would have caught it, but React can no longer tell the two rows apart ` +
        `across a re-render and may keep the wrong one's state. Key on something that differs ` +
        `between siblings. See rule 19.`,
    );
  }
});
