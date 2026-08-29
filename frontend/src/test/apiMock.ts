// SPDX-License-Identifier: AGPL-3.0-or-later
// A mock that answers every api function, for tests that replace `../api` wholesale.
//
// A hand-picked subset of api functions is easy to get wrong: it depends on what the component
// tree reads, that changes whenever a nested hook starts reading something new, and the file
// that needs updating is not the one anyone is looking at. An unmocked function used as a query
// returns `undefined`, which `src/test/setup.ts` fails the test run for. Used inside a hook's
// own function (`() => api.foo(x)`), it throws instead, and React Query renders the tree's
// ordinary failed-read branch, so nothing points at the missing mock. Mocking every function up
// front removes the subset to get wrong in the first place.
//
// A test that means to reject a read still says so with `mockRejectedValue`. An absent mock was
// never the right way to ask for that.
//
// This also covers mutations, which query-completeness checks do not: React Query never flags a
// mutation with no `mutationFn`. `removeApiKey` went missing from one panel's mock with no
// warning until a test that pressed Remove failed on an unrelated undefined call.
//
// The function names are read off `api` itself, so adding or removing one from `api.ts` is
// answered here automatically, with nothing else to keep in sync.
import { vi } from "vitest";

import { api } from "../api";

/** Every api function as a `vi.fn()`, typed against the real one so a call site that mocks the
 *  wrong shape is a compile error rather than a passing test. */
export type ApiMock = { [K in keyof typeof api]: ReturnType<typeof vi.fn> };

/** A fresh set per file. Never share one across files. `vi.fn()` carries call history, and
 *  vitest gives each test file its own module registry, so a shared instance would leak call
 *  counts between them. */
export function makeApiMock(): ApiMock {
  return Object.fromEntries(Object.keys(api).map((name) => [name, vi.fn()])) as ApiMock;
}
