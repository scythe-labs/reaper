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

/** The trigger that lights the whole chip, by state. Written out rather than matched loosely: a
 *  matcher accepting anything with "coll-chip" and ":hover" would also accept a rule that lights
 *  only one half, which is the defect this file exists for (rule 147). */
const LIT = {
  hover: ".coll-chip:has(button:hover)",
  press: ".coll-chip:has(button:active)",
};

describe("the collection chip's lit state", () => {
  it("repaints on hover only where a pointer can actually hover", () => {
    const guarded = hoverGuardedRegions().join("\n");
    expect(guarded).not.toBe("");
    // Present in the sheet at all, so a deleted rule cannot pass by being absent.
    expect(code).toContain(LIT.hover);
    // ...and every occurrence sits inside a hover-capable block.
    const total = code.split(LIT.hover).length - 1;
    expect(guarded.split(LIT.hover).length - 1).toBe(total);
  });

  // The half this file was first written without, which left a tap with NO feedback: hover is
  // pointer-only above, and `:focus-visible` never matches a tap at all, since Safari reserves
  // it for keyboard focus. Press is the state a touch device actually has.
  it("lights on press, on every device, so a tap is never silent", () => {
    const guarded = hoverGuardedRegions().join("\n");
    expect(code).toContain(LIT.press);
    expect(guarded).not.toContain(LIT.press);
  });

  // The caret's expanded state is NOT a pointer's, so it must stay outside the guard: a picker
  // left open on a touch device would otherwise draw no accent at all on the chip it belongs to.
  it("keeps the open picker's accent on every device", () => {
    const guarded = hoverGuardedRegions().join("\n");
    const expanded = '.coll-chip-caret[aria-expanded="true"]';
    expect(code).toContain(expanded);
    expect(guarded).not.toContain(expanded);
  });

  // The lit state belongs to the CHIP. Lighting one half was the reported defect: the name went
  // accent and the caret sat dark inside an already-accent border. Both triggers therefore hang
  // off `.coll-chip`, never off a half, so neither can paint one side on its own.
  it("is triggered from the chip, never from one half of it", () => {
    for (const half of [".coll-chip-main", ".coll-chip-caret"]) {
      for (const state of [":hover", ":active"]) {
        expect(code).not.toContain(`${half}${state}`);
      }
    }
  });
});
