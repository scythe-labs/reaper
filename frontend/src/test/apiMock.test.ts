// @vitest-environment node
// SPDX-License-Identifier: AGPL-3.0-or-later
// The mock is only "complete" while it matches the module it stands in for.
import { describe, expect, it } from "vitest";

import { api } from "../api";
import { makeApiMock } from "./apiMock";

describe("the complete api mock", () => {
  it("answers exactly the functions the real module exports, both directions", () => {
    // Both directions, because they fail differently and only one of them is loud. A function
    // ADDED to `api.ts` and missing here hands thirty-five component trees `undefined` for a
    // read some nested hook performs, which rule 135 catches one file at a time as each tree
    // happens to reach it. A function REMOVED from `api.ts` and left here is silent forever:
    // the mock answers a call nothing makes, and a test can go on asserting against a function
    // the app no longer has. The set comparison catches the second, which nothing else does.
    expect(new Set(Object.keys(makeApiMock()))).toEqual(new Set(Object.keys(api)));
  });

  it("hands out a fresh set each call, so call counts never cross a file boundary", () => {
    // `vi.fn()` carries its own call history. One shared instance would leak counts between the
    // files that imported it, and the failure would land in whichever file happened to run
    // second under `-n auto` (rule 133).
    const first = makeApiMock();
    const second = makeApiMock();
    expect(first.safety).not.toBe(second.safety);
  });
});
