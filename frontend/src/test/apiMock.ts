// SPDX-License-Identifier: AGPL-3.0-or-later
// One mock that answers every api function, for the tests that replace `../api` wholesale.
//
// Thirty-five test files hand-declared the subset of the api their tree reads, ranging from one
// function to thirty-two. That list is not the test's own business: it is a property of the
// component tree, it changes whenever a nested hook starts reading something, and the file that
// has to change is one nobody editing the hook is looking at. Rule 135 is the standing answer --
// a mock gap fails the run rather than warning -- and this closes the same gap one step earlier,
// by leaving no subset to get wrong.
//
// **Why this is safe to widen, given rule 135's own risk note.** Supplying a function a file used
// to omit could in principle answer a read the test meant to fail. It cannot, in either shape a
// tree performs the read:
//
//   * `queryFn: api.foo` -- an omitted `foo` is `undefined`, which `src/test/setup.ts` already
//     FAILS the run on. So no passing test relies on that absence today; there are none to break.
//   * `queryFn: () => api.foo(x)` -- an omitted `foo` throws a TypeError inside the arrow, which
//     React Query files as an ordinary rejection and the tree renders its failed-read branch. An
//     unconfigured `vi.fn()` returns `undefined`, which React Query also rejects. Same branch,
//     same silence. This is rule 135's documented blind spot, and it is unchanged either way.
//
// A test that means to reject a read still says so with `mockRejectedValue`, which is what the
// suite already does; absence was never the way to ask for it.
//
// **The mutations are the half nothing was watching.** `setup.ts` fails a query with no
// `queryFn`; it says nothing about a mutation with no `mutationFn`, because React Query does not
// announce that one. `removeApiKey` was missing from the General panel's mock until the Remove
// path needed driving, and it went missing SILENTLY: a test pressing Remove would have failed on
// an undefined call rather than on the thing it was checking. So the completeness here is not a
// tidier spelling of a gate that already existed -- for every mutation in the module, it is the
// only thing standing between a gap and a confusing failure.
//
// There is no key list to check. The names are read off `api` itself, so a function added to or
// removed from `api.ts` is answered here on the next run, and rule 103's drift guard has nothing
// left to guard.
import { vi } from "vitest";

import { api } from "../api";

/** Every api function as a `vi.fn()`, typed against the real one so a call site that mocks the
 *  wrong shape is a compile error rather than a passing test. */
export type ApiMock = { [K in keyof typeof api]: ReturnType<typeof vi.fn> };

/** A fresh set per file. Never share one across files: `vi.fn()` carries call history, and
 *  vitest gives each test file its own module registry, so a shared instance would leak counts
 *  between them (rule 133). */
export function makeApiMock(): ApiMock {
  return Object.fromEntries(Object.keys(api).map((name) => [name, vi.fn()])) as ApiMock;
}
