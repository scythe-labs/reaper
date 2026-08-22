// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// The one part of an anchored popover's placement that is arithmetic rather than layout: how far
// it slides to stay on screen. jsdom reports every box as zero-sized, so the measuring the
// component does around this call cannot be exercised there -- these pin the decision it feeds.
//
// The numbers are a phone: a 390px-wide screen and the filter menu at its 260px widest.

import { describe, expect, it } from "vitest";
import { fixedMenuPos, inlineStartOf, popoverShift } from "./popoverFit";

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

// These two are the only places in the app that measure the viewport in raw pixels, so they are
// the only two the browser's own `dir="rtl"` mirroring cannot reach (#861). Everything else
// mirrors because the stylesheet names its sides by reading order.
describe("inlineStartOf", () => {
  it("is the left edge in a left-to-right page and the mirror of the right edge otherwise", () => {
    const rect = { left: 300, right: 360 };
    expect(inlineStartOf(rect, 390, false)).toBe(300);
    // 390 - 360: the same box is 30px from the edge an Arabic reader starts at.
    expect(inlineStartOf(rect, 390, true)).toBe(30);
  });

  it("measures a box against whichever edge the reader starts from, symmetrically", () => {
    // A box against the far edge in one direction is against the near edge in the other, so
    // the two readings of any rect sum to the space the rect does not fill.
    for (const [left, right] of [
      [0, 40],
      [120, 260],
      [350, 390],
    ] as const) {
      const ltr = inlineStartOf({ left, right }, 390, false);
      const rtl = inlineStartOf({ left, right }, 390, true);
      expect(ltr + rtl).toBe(390 - (right - left));
    }
  });
});

describe("fixedMenuPos", () => {
  // A trigger near the RIGHT edge of a 390px phone, which is where the caret sits on a card.
  const nearRightEdge = { left: 330, right: 360, top: 200, bottom: 224 };
  // And one near the left edge, the mirror case.
  const nearLeftEdge = { left: 30, right: 60, top: 200, bottom: 224 };

  it("hangs the menu back from the trigger's far edge, whichever edge that is", () => {
    // Left to right: the menu's right edge meets the trigger's right edge, so it starts
    // 360 - 200 = 160px from the left.
    expect(fixedMenuPos(nearRightEdge, 200, 100, 390, 800, 8, false).start).toBe(160);
    // Right to left: the trigger's far edge is its LEFT one, 390 - 30 = 360px from the reading
    // edge, so the menu starts 160px from the RIGHT. The mirror of the same number.
    expect(fixedMenuPos(nearLeftEdge, 200, 100, 390, 800, 8, true).start).toBe(160);
  });

  it("clamps to the near gutter rather than run the menu off the reading edge", () => {
    // A menu wider than the space before the trigger cannot start at a negative offset.
    expect(fixedMenuPos(nearLeftEdge, 200, 100, 390, 800, 8, false).start).toBe(8);
    expect(fixedMenuPos(nearRightEdge, 200, 100, 390, 800, 8, true).start).toBe(8);
  });

  it("keeps the menu on screen in both directions, whatever the trigger's position", () => {
    for (let left = 0; left <= 360; left += 10) {
      for (const rtl of [false, true]) {
        const { start } = fixedMenuPos(
          { left, right: left + 30, top: 200, bottom: 224 },
          200,
          100,
          390,
          800,
          8,
          rtl,
        );
        expect(start).toBeGreaterThanOrEqual(8);
        expect(start + 200).toBeLessThanOrEqual(390 - 8);
      }
    }
  });

  it("flips above the trigger the same way in either direction", () => {
    // The vertical decision is not a reading-order one, so it must not have picked one up.
    const low = { left: 100, right: 130, top: 760, bottom: 790 };
    for (const rtl of [false, true]) {
      const pos = fixedMenuPos(low, 200, 100, 390, 800, 8, rtl);
      expect(pos.top).toBeUndefined();
      expect(pos.bottom).toBe(800 - 760 + 4);
    }
  });
});
