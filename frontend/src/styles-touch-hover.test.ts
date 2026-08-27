// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// A touch screen has no hover to leave. iOS Safari applies `:hover` to the last element tapped
// and holds it there until something else is tapped. A hover rule that repaints a control then
// reads, on an iPad, as a state the operator chose rather than one a pointer is resting on.
//
// This test guards the collection chip only. Every other `:hover` rule in the stylesheet has
// the same risk and no guard yet. Widening the guard to the whole sheet is separate work.
import { describe, expect, it } from "vitest";

import { CSS } from "./test/stylesheet";

/** Blanks out CSS comments to the same length. This stops a selector named in a comment from
 *  matching as a real rule, while every character offset still lines up with the source. */
const code = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));

/** Returns each `@media (hover: hover)` block's body, found by matching braces from its own
 *  `{`. */
function hoverGuardedRegions(): string[] {
  const out: string[] = [];
  const open = /@media\s*\(\s*hover\s*:\s*hover\s*\)\s*\{/g;
  for (const m of code.matchAll(open)) {
    let depth = 1;
    let i = m.index! + m[0].length;
    const from = i;
    while (i < code.length && depth > 0) {
      if (code[i] === "{") depth++;
      else if (code[i] === "}") depth--;
      i++;
    }
    out.push(code.slice(from, i - 1));
  }
  return out;
}

/** The exact selector that lights the whole chip, by state. Written out in full rather than
 *  matched loosely: a matcher that just checks for "coll-chip" and ":hover" would also accept
 *  a rule that lights only half the chip. */
const LIT = {
  hover: ".coll-chip:has(button:hover)",
  press: ".coll-chip:has(button:active)",
};

describe("the collection chip's lit state", () => {
  it("repaints on hover only where a pointer can actually hover", () => {
    const guarded = hoverGuardedRegions().join("\n");
    expect(guarded).not.toBe("");
    // Confirms the rule exists in the sheet, so a deleted rule cannot pass by being absent.
    expect(code).toContain(LIT.hover);
    // Confirms every occurrence sits inside a hover-capable block.
    const total = code.split(LIT.hover).length - 1;
    expect(guarded.split(LIT.hover).length - 1).toBe(total);
  });

  // A tap gives no hover (guarded above) and no focus-visible either, since Safari reserves
  // that for keyboard focus. Press is the state a touch device actually produces.
  it("lights on press, on every device, so a tap is never silent", () => {
    const guarded = hoverGuardedRegions().join("\n");
    expect(code).toContain(LIT.press);
    expect(guarded).not.toContain(LIT.press);
  });

  // The caret's expanded state comes from opening the picker, not from a pointer resting on it,
  // so it stays outside the hover guard. Otherwise an open picker on a touch device would draw
  // no accent on its chip at all.
  it("keeps the open picker's accent on every device", () => {
    const guarded = hoverGuardedRegions().join("\n");
    const expanded = '.coll-chip-caret[aria-expanded="true"]';
    expect(code).toContain(expanded);
    expect(guarded).not.toContain(expanded);
  });

  // The lit state belongs to the whole chip. Both hover and press triggers attach to
  // `.coll-chip`, never to just the name or just the caret, so one half can never light without
  // the other.
  it("is triggered from the chip, never from one half of it", () => {
    for (const half of [".coll-chip-main", ".coll-chip-caret"]) {
      for (const state of [":hover", ":active"]) {
        expect(code).not.toContain(`${half}${state}`);
      }
    }
  });
});
