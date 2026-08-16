// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// A touch screen has no hover to leave. iOS Safari applies `:hover` to the last element TAPPED
// and holds it there until something else is tapped, so a hover rule that repaints a control
// reads on an iPad as a state the operator selected rather than one a pointer is resting on.
// The collection chip was found that way on a real library: after the tap that opened the
// collection it kept accent text and an accent border, on a card the operator had merely
// visited, beside a genuinely selected card wearing the same accent.
//
// **Scoped to the collection chip on purpose, and that is the uncomfortable half.** Every other
// `:hover` in these 34 files has the same shape and no guard, so this pins one component while
// the tree around it is unguarded. Widening it is a repo-wide change with its own issue; a
// guard that fails for the whole sheet today would fail on arrival and get deleted, which is
// worse than one that holds the ground this PR actually took.
import { describe, expect, it } from "vitest";

import { CSS } from "./test/stylesheet";

/** Comments blanked to the same length, so prose naming a selector is never read as a rule
 *  while every offset still resolves to its real position. */
const code = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));

/** Each `@media (hover: hover)` block's body, found by balancing braces from its own `{`. */
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

describe("the collection chip's hover", () => {
  // The two spellings the chip's own rules use. Written out rather than matched loosely,
  // because a matcher that accepts anything containing "coll-chip" and ":hover" would also
  // accept the `:has()` arm moving somewhere it does not belong (rule 147).
  const HOVER_RULES = [".coll-chip-main:hover", ".coll-chip-caret:hover"];

  it("repaints only where a pointer can actually hover", () => {
    const guarded = hoverGuardedRegions().join("\n");
    expect(guarded).not.toBe("");
    for (const rule of HOVER_RULES) {
      // Present in the sheet at all, so a deleted rule cannot pass this by being absent.
      expect(code).toContain(rule);
      // ...and every occurrence of it sits inside a hover-capable block.
      const total = code.split(rule).length - 1;
      const inside = guarded.split(rule).length - 1;
      expect(inside).toBe(total);
    }
  });

  // The caret's expanded state is NOT a pointer's, so it must stay outside the guard: a picker
  // left open on a touch device would otherwise draw no accent at all on the chip it belongs to.
  it("keeps the open picker's accent on every device", () => {
    const guarded = hoverGuardedRegions().join("\n");
    const expanded = '.coll-chip-caret[aria-expanded="true"]';
    expect(code).toContain(expanded);
    expect(guarded).not.toContain(expanded);
  });

  // The half this file was written without, and it left the chip with NO feedback on a tap:
  // hover is pointer-only above, and `:focus-visible` never matches a tap at all, since Safari
  // reserves it for keyboard focus. Press is the state a touch device actually has, so it must
  // exist and must never move inside the hover guard.
  it("lights on press, on every device, so a tap is never silent", () => {
    const guarded = hoverGuardedRegions().join("\n");
    for (const rule of [".coll-chip-main:active", ".coll-chip-caret:active"]) {
      expect(code).toContain(rule);
      expect(guarded).not.toContain(rule);
    }
  });
});
