// SPDX-License-Identifier: AGPL-3.0-or-later
// One invariant, held over the whole stylesheet: viewport heights are `dvh`, never `vh`, `lvh`
// or `svh`.
//
// `vh` is the *largest* viewport -- the height the page would have with the mobile browser's
// toolbar hidden -- so a box sized in `vh` can extend past the part of the screen the operator
// can see, and its bottom edge is where confirm buttons live. Every modal and sheet in this app
// is already sized in `dvh` for that reason; the bug is always a child measured in the other
// unit inside one of them. It has been fixed three times (`.why`, then `.log-console` and
// `.docs-index` below 640px, U-16), which is why it is a test now rather than a fourth comment.
//
// `lvh` is that same largest viewport, spelled explicitly -- exactly the hazard above -- so it
// is caught too, and `svh` (the smallest) with it: the point is that a height tracks the
// viewport as it actually is, which only `dvh` does.
import { describe, expect, it } from "vitest";

import { CSS as css, FILES } from "./test/stylesheet";

describe("the stylesheet", () => {
  it("reads every file the barrel imports", () => {
    // This test used to read `index.css` alone. When that file became a barrel of @imports it
    // went on passing while scanning no rules at all -- so the population is pinned here, where
    // a stylesheet that stops being loaded fails loudly instead of quietly clearing the ban.
    expect(FILES.length).toBeGreaterThan(25);
    expect(css.length).toBeGreaterThan(200_000);
  });

  it("sizes every viewport height in dvh, never vh, lvh or svh", () => {
    // A digit immediately before the unit (with only `l` or `s` allowed between), so `100dvh`
    // is not a match -- its `d` sits where the digit would have to be -- and neither is the
    // word "vh" inside prose.
    const bare = css.match(/[\d.][ls]?vh\b/g) ?? [];
    expect(bare).toEqual([]);
  });
});
