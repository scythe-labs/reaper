// SPDX-License-Identifier: AGPL-3.0-or-later
// One invariant, held over the whole stylesheet: viewport heights are `dvh`, never `vh`.
//
// `vh` is the *largest* viewport -- the height the page would have with the mobile browser's
// toolbar hidden -- so a box sized in `vh` can extend past the part of the screen the operator
// can see, and its bottom edge is where confirm buttons live. Every modal and sheet in this app
// is already sized in `dvh` for that reason; the bug is always a child measured in the other
// unit inside one of them. It has been fixed three times (`.why`, then `.log-console` and
// `.docs-index` below 640px, U-16), which is why it is a test now rather than a fourth comment.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "index.css"), "utf8");

describe("index.css", () => {
  it("sizes every viewport height in dvh, never vh", () => {
    // A digit immediately before the unit, so `100dvh` (digit, then `d`) is not a match and
    // neither is the word "vh" inside prose.
    const bare = css.match(/[\d.]vh\b/g) ?? [];
    expect(bare).toEqual([]);
  });
});
