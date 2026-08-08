// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// The one part of an anchored popover's placement that is arithmetic rather than layout: how far
// it slides to stay on screen. jsdom reports every box as zero-sized, so the measuring the
// component does around this call cannot be exercised there -- these pin the decision it feeds.
//
// The numbers are a phone: a 390px-wide screen and the filter menu at its 260px widest.

import { describe, expect, it } from "vitest";
import { popoverShift } from "./popoverFit";

describe("popoverShift", () => {
  // Written from what the placement promises, never read off its branches (rule 119). Each row
  // is [what it promises, anchor's left edge, popover width, screen width, pixels to slide].
  const cases: [string, number, number, number, number][] = [
    ["leaves a popover with room to spare exactly where its anchor puts it", 24, 260, 1440, 0],
    ["leaves the last popover that still fits alone, to the pixel", 122, 260, 390, 0],
    ["slides a popover one pixel over the edge by that one pixel", 123, 260, 390, -1],
    ["pulls a right-edge anchor's popover fully back onto a phone screen", 300, 260, 390, -178],
    ["stops at the near gutter rather than run a too-wide popover off the left", 40, 400, 320, -32],
    ["never pushes a popover right of its anchor, however little fits", 2, 400, 320, 0],
  ];

  it.each(cases)("%s", (_promise, anchorLeft, width, screen, want) => {
    expect(popoverShift(anchorLeft, width, screen)).toBe(want);
  });

  // The two invariants above stated over the whole space rather than at six points: whatever the
  // geometry, the slide is a pull leftward, and it never drags the popover past the gutter.
  it("is never a push right, and never crosses the left gutter", () => {
    for (let anchorLeft = 0; anchorLeft <= 400; anchorLeft += 7) {
      for (const width of [120, 190, 260, 400]) {
        for (const screen of [320, 390, 768, 1440]) {
          const shift = popoverShift(anchorLeft, width, screen);
          expect(shift).toBeLessThanOrEqual(0);
          expect(anchorLeft + shift).toBeGreaterThanOrEqual(Math.min(anchorLeft, 8));
        }
      }
    }
  });
});
