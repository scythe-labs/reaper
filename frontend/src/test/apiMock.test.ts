// @vitest-environment node
// SPDX-License-Identifier: AGPL-3.0-or-later
// The mock derives its names from `api` itself, so completeness is not a thing to assert. What
// is left is the one property the derivation does not give for free.
import { describe, expect, it } from "vitest";

import { makeApiMock } from "./apiMock";

describe("the complete api mock", () => {
  it("hands out a fresh set each call, so call counts never cross a file boundary", () => {
    // `vi.fn()` carries its own call history. One shared instance would leak counts between the
    // files that imported it, and the failure would land in whichever file happened to run
    // second under `-n auto`.
    const first = makeApiMock();
    const second = makeApiMock();
    expect(first.safety).not.toBe(second.safety);
  });
});
