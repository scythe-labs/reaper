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

// `findBy*` and `waitFor` run on Testing Library's own `asyncUtilTimeout`, which
// `vite.config.ts`'s `testTimeout` does not reach. It defaults to 1000ms, a budget for waiting
// on a read, and this suite spends it rendering whole panels. The nine-case anchor walk in
// `PolicyEditor.warnings.test.tsx` measured 630-948ms per case on an idle machine, leaving its
// widest case almost no room. Under load, full-suite runs failed here on branches that touched
// none of this code, then passed again on a re-run.
//
// The timeout is raised rather than made deterministic, because nothing here is racing. The
// assertion waits for a first render whose cost belongs to the machine, so there is no timer
// for a fake clock to advance and no event for a deterministic wait to key on.
// `AppStaleRead.test.tsx` and `AppUrl.test.tsx` each warm a `React.lazy` boundary in `beforeAll`
// so a cold transform cannot land inside a test's wait, but that only covers the one boundary
// each names, and the same cost shows up anywhere else a first render is expensive.
//
// 5000ms is well under `testTimeout`'s 15000, so an element that is genuinely missing still
// loses here and prints Testing Library's "unable to find" message along with the DOM it
// searched. Losing to `testTimeout` instead would report only the test's name and nothing else.
// A real hang now costs five seconds, which is cheaper than the re-run a false failure costs.
// This is set per test file, like the rest of this file, so it leaves nothing behind for the
// next worker.
configure({ asyncUtilTimeout: 5000 });

// This reads the value back, for the same reason the warm-up below asserts its own effect.
// `configure` takes a partial object and silently ignores a key it does not recognize, so a
// rename in a major bump of `@testing-library/dom` would leave this file setting nothing, every
// wait back on 1000ms, with no line anywhere to blame. This fails here instead, where the
// message names the cause.
if (getConfig().asyncUtilTimeout !== 5000) {
  throw new Error(
    "Testing Library kept its own asyncUtilTimeout, so `configure` above set nothing and every " +
      "`findBy*`/`waitFor` in the suite is back on the 1000ms default. Check whether the option " +
      "was renamed. See #887.",
  );
}

// This file is `setupFiles`, so it runs for every test file, including the ones carrying an
// `@vitest-environment node` docblock. Those have no DOM at all rather than an empty one, so
// the three writes below are guarded. The console guards further down are not guarded this
// way, since an unanswered mock and a state update outside `act()` are just as real without a
// DOM as with one.
const hasDom = typeof document !== "undefined";

if (hasDom) {
  // jsdom has no layout, so window.scrollTo is unimplemented and logs a noisy "Not
  // implemented" message on every call. ModalShell restores the scroll offset with it when a
  // modal closes, so this makes it a quiet no-op, since nothing in the tests reads a real
  // scroll position.
  window.scrollTo = () => {};

  // This is the same reason, one layer down. jsdom does not implement Element.scrollIntoView at
  // all, so the property is `undefined` rather than a no-op, and calling it throws. Four
  // components scroll a keyboard target into view (the suggester's active option, the docs
  // anchor, the policy warning anchors), and each would take its whole test file down with a
  // TypeError. This is assigned rather than spied, because `vi.spyOn` cannot wrap a method that
  // does not exist.
  Element.prototype.scrollIntoView = () => {};
}

// This pays jsdom's first `getComputedStyle` here, where nothing is timing it.
//
// The first such call in a jsdom instance costs about 52ms building the CSSOM, and every
// `*ByRole` query makes one, since `queryAllByRole` filters inaccessible elements by reading
// computed visibility. Whichever role query runs first in a file pays that cost, and when that
// query is a test's first `await findByRole(...)`, the cost lands inside `findBy`'s own wait
// budget instead of the read it is waiting for. Paying it here up front buys headroom instead.
// See docs/LEARNINGS.md for the measurements.
//
// Vitest gives each test file its own module registry and jsdom, so this runs per file, which
// is exactly the granularity the cost is paid at. It leaves no DOM behind.
//
// The property is read, not just the call made, because jsdom builds the CSSOM on first access
// rather than on the call. The value is then asserted, so a jsdom that stops answering fails
// here loudly, instead of leaving this line silently warming nothing.
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

// A query with no queryFn fails the test rather than warning.
//
// React Query answers a missing queryFn with a console.error and an error state, so a test
// that mocked "../api" without a function some nested hook reads still passes, rendering the
// tree's "could not read it" branch instead. A warning cannot be the signal here. Vitest's
// console interception drops test console output entirely on some Node versions (on Node 26 a
// bare console.error inside a test prints nothing, while CI on Node 24 prints it), so the only
// place the warning would be visible is a CI log nobody reads. Failing the test is the only
// form of this that survives locally.
//
// A state update outside act(...) fails the test for the same reason. It means the test
// asserted on a moment the component had already left, because something it never awaited
// settled behind the assertions. A promise a test forgot to await resolves as the body
// returns, every run.
//
// This judges every such update, with no exemption for the framework. Two fixable causes
// explain updates that might look like machine timing: `refetchOnWindowFocus` refetching the
// queue's own reads whenever user-event clicks something focusable (`src/test/queryClient.ts`),
// and a mid-test `await import(...)` of user-event, which is a bare await outside act for a
// fetch to land in.
//
// An exempted warning would still print, so a green CI run would carry an act warning nobody
// could act on, the same "warning nobody reads" this file exists to delete, now with the
// gate's own blessing. Nothing here warns. It either fails, or it has nothing to say.
let missingQueryFn: string[] = [];
let undefinedData: string[] = [];
let outsideAct: string[] = [];
let duplicateKeys: string[] = [];
const forwardError = console.error.bind(console);
console.error = (...args: unknown[]) => {
  if (typeof args[0] === "string") {
    if (args[0].includes("No queryFn was passed")) missingQueryFn.push(args[0]);
    // The same failure can be reached through an arrow function, which is why the collector
    // above cannot see it. `queryFn: () => api.vocabularyValues(f)` has a queryFn, and the mock
    // gap is inside it instead. React Query files that as an ordinary rejection, the tree
    // paints its could-not-read branch, and a test can assert against that branch believing it
    // is the real app.
    if (args[0].includes("Query data cannot be undefined")) undefinedData.push(args[0]);
    // "An update to %s inside a test was not wrapped in act(...)", the component name second.
    if (args[0].includes("was not wrapped in act(")) {
      outsideAct.push(String(args[1] ?? "a component"));
    }
    // "Encountered two children with the same key, `%s`." React 19 still renders both siblings,
    // so nothing about the page looks wrong, and this console line is the only thing that ever
    // says so. What is actually lost is React's reconciliation guarantee across a re-render,
    // and it is lost silently.
    if (args[0].includes("Encountered two children with the same key")) {
      duplicateKeys.push(String(args[1] ?? "an unnamed key"));
    }
  }
  forwardError(...args);
};

afterEach(() => {
  // The address bar is app state now (navUrl.ts). `App` reads its section from the path, and
  // the queue seeds its filters from the query string, both at mount. jsdom carries one
  // location across a whole file, so a test that leaves `/review/limbo?genre=…` behind would
  // silently open the next test's queue on that lane, filtered. This replaces the location
  // rather than pushing a new entry, so the file's session history is left as it was found.
  if (hasDom) history.replaceState(null, "", "/");
  // This also clears the module-level record of what was last written, or a pop in the next
  // test would re-assert this one's URL over it.
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
