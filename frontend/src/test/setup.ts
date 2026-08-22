// SPDX-License-Identifier: AGPL-3.0-or-later
// Registers the jest-dom matchers (toBeDisabled, toBeInTheDocument, ...) with vitest.
import "@testing-library/jest-dom/vitest";

// The app's own i18n init, with the real English catalog and en-US pinned, so every
// copy-bound query in the suite reads the exact bytes the app renders (docs/history/I18N_PLAN.md §4).
// It guards its own document writes, so the node-environment test files can run it too.
import "../i18n";

import { configure, getConfig } from "@testing-library/react";
import { afterEach } from "vitest";

import { forgetWrittenUrl } from "../navUrl";

// `findBy*` and `waitFor` run on Testing Library's own `asyncUtilTimeout`, and
// `vite.config.ts`'s `testTimeout` does not reach it. It defaults to 1000ms, which is a budget
// for waiting on a read, and this suite spends it rendering whole panels: the nine-case anchor
// walk in `PolicyEditor.warnings.test.tsx` measured 630-948ms per case on an IDLE box, so its
// widest case had 5% of the budget left. Three full-suite runs failed this way on branches that
// touch none of it, each passing again alone and on a re-run (#887). Reproduced under a load of
// 32 on a 20-core box: two full-suite runs in three, three distinct assertions, every one of
// them a wait in that file.
//
// Raised rather than made deterministic, because nothing here is racing. The assertion waits for
// a first render whose cost belongs to the machine, so there is no timer for a fake clock to
// advance and no event for a deterministic wait to key on. Two files already answered this once:
// `AppStaleRead.test.tsx` and `AppUrl.test.tsx` warm a `React.lazy` boundary in `beforeAll` so a
// cold transform cannot land inside the 1000ms (#651). Both still turned up in #887's failing
// sets, because that fix covers the one boundary it names and the cost is everywhere.
//
// 5000ms, well under `testTimeout`'s 15000, so an element that is genuinely missing still loses
// HERE and prints Testing Library's "unable to find" with the DOM it searched. Let it lose to
// `testTimeout` instead and the report is the test's name and nothing else. A real hang now costs
// five seconds, which is cheaper than the re-run a false failure costs. Per test file, like the
// rest of this file, so it leaves nothing behind for the next worker (rule 133).
configure({ asyncUtilTimeout: 5000 });

// Read back, for the same reason the warm-up below asserts its own effect. `configure` takes a
// partial and ignores a key it does not know, so a rename in a major bump of
// `@testing-library/dom` leaves this file setting nothing, every wait back on 1000ms, and #887
// returning with no line to blame. Fail here instead, where the message names the cause.
if (getConfig().asyncUtilTimeout !== 5000) {
  throw new Error(
    "Testing Library kept its own asyncUtilTimeout, so `configure` above set nothing and every " +
      "`findBy*`/`waitFor` in the suite is back on the 1000ms default. Check whether the option " +
      "was renamed. See #887.",
  );
}

// This file is `setupFiles`, so it runs for EVERY test file, including the twelve carrying an
// `@vitest-environment node` docblock. Those have no DOM at all rather than an empty one, so
// the three writes below are guarded. The console guards further down are not: rule 135's mock
// gap and rule 136's stray update are as real without a DOM as with one.
const hasDom = typeof document !== "undefined";

if (hasDom) {
  // jsdom has no layout, so window.scrollTo is unimplemented and logs a noisy "Not
  // implemented" on every call. ModalShell restores the scroll offset with it when a modal
  // closes, so make it a quiet no-op here -- nothing in the tests reads a real scroll.
  window.scrollTo = () => {};

  // Same reason, one layer down: jsdom does not implement Element.scrollIntoView AT ALL, so
  // the property is `undefined` rather than a no-op and calling it throws. Four components
  // scroll a keyboard target into view (the suggester's active option, the docs anchor, the
  // policy warning anchors), and each would take its whole test file down with a TypeError.
  // Assigned rather than spied, because `vi.spyOn` cannot wrap a method that does not exist.
  Element.prototype.scrollIntoView = () => {};
}

// Pay jsdom's first `getComputedStyle` here, where nothing is timing it.
//
// The first such call in a jsdom instance costs ~52ms building the CSSOM, and every
// `*ByRole` query makes one: `queryAllByRole` filters inaccessible elements, which reads
// computed visibility. So whichever role query runs first in a file paid that ~52ms, and
// when that query was a test's first `await findByRole(...)` the cost landed INSIDE
// `findBy`'s budget, spending 5% of the then-1000ms margin before the read it is waiting
// for had a chance to land. Ten test files opened on a role query that way. The budget is
// 5000ms now (above), so this buys headroom rather than rescuing the margin, and it is still
// ~52ms nobody has to spend inside a wait.
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
if (hasDom) {
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
let undefinedData: string[] = [];
let outsideAct: string[] = [];
let duplicateKeys: string[] = [];
const forwardError = console.error.bind(console);
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string") {
    if (args[0].includes("No queryFn was passed")) missingQueryFn.push(args[0]);
    // The same failure reached through an arrow, which is why the collector above cannot see
    // it: `queryFn: () => api.vocabularyValues(f)` HAS a queryFn, and the mock gap is inside
    // it. React Query files that as an ordinary rejection, the tree paints its could-not-read
    // branch, and the test asserts against that branch believing it is the app. Twenty of
    // these sat behind a green suite, in two files (#704). Rule 135 named this as its own
    // blind spot and nothing enforced it.
    if (args[0].includes("Query data cannot be undefined")) undefinedData.push(args[0]);
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
  // The address bar is app state now (navUrl.ts): `App` reads its section from the path and the
  // queue seeds its filters from the query string, both at mount. jsdom carries one location
  // across a whole file, so a test that leaves `/review/limbo?genre=…` behind would silently
  // open the next test's queue on that lane, filtered. Replaced, never pushed, so the file's
  // session history is left as it was found (rule 133).
  if (hasDom) history.replaceState(null, "", "/");
  // ...and the module-level record of what was last written, or a pop in the next test
  // re-asserts this one's URL over it (rule 133).
  forgetWrittenUrl();

  const queries = missingQueryFn;
  const undefineds = undefinedData;
  const unacted = outsideAct;
  const dupes = duplicateKeys;
  missingQueryFn = [];
  undefinedData = [];
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
  if (undefineds.length > 0) {
    // "... Affected query key: [\"vocabulary-values\",\"genre\"]" is how the message ends.
    const keys = [...new Set(undefineds.map((m) => m.slice(m.indexOf("Affected query key:"))))];
    throw new Error(
      `A query function resolved to undefined: ${keys.join(", ")}. The mock for "../api" ` +
        `answers nothing for a read this tree makes through an arrow, so that query rendered ` +
        `as a failed read and the assertions above ran against the could-not-read branch. ` +
        `Answer it; src/test/apiFixtures.ts holds the payloads. See rule 135.`,
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
