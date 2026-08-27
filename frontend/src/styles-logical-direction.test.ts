// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
// One invariant, held over the whole stylesheet: a side is named by reading order
// (`margin-inline-start`), never by the screen (`margin-left`).
//
// A browser mirrors a page for Arabic, Hebrew or Persian on its own once `dir="rtl"` is set,
// but only for the properties written in the reading-order form. A page mixing the two comes
// out half-mirrored, which is worse than not mirroring at all: the sidebar moves and the
// padding holding its text off the edge does not, so the text sits under the edge. This test
// keeps a screen-named declaration from arriving again.
//
// Writing `margin-inline-start` costs nothing in a left-to-right page: the browser resolves it
// to `margin-left`, and the rendered pixels are identical.
import { describe, expect, it } from "vitest";

import { blocksOf, siteOf } from "./test/stylesheet";

/** The screen-named properties, longest first so `border-left` is never read as a bare `left`. */
const PHYSICAL =
  /(?:^|[;{\s])(margin-(?:left|right)|padding-(?:left|right)|border-(?:left|right)(?:-(?:color|width|style))?|border-(?:top|bottom)-(?:left|right)-radius|float|clear|left|right)\s*:/g;

/** `text-align` keeps its name and moves its value: `start` and `end`, never `left` and `right`. */
const PHYSICAL_ALIGN = /(?:^|[;{\s])text-align\s*:\s*(left|right)\b/g;

/** The declarations that are RIGHT to leave naming a screen side, as `selector|property`.
 *
 *  Each one is a case where the physical side is not an inline side, so converting it would
 *  make the page wrong rather than portable. A new entry needs the same kind of reason, not
 *  just a passing build. */
const KEEP_PHYSICAL = new Map([
  [".sheet|left", "centered by left:50% with translate(-50%); a logical inset lands it off-center"],
  [".scan-toast|left", "centered the same way as .sheet"],
  [".hist-thresh b|left", "centered the same way as .sheet"],
  [
    ".doc-diagram .dd-varrow|border-left",
    "half of a symmetric transparent pair drawing a DOWNWARD triangle: the axis is vertical, so neither side is an inline side",
  ],
  [".doc-diagram .dd-varrow|border-right", "the other half of that triangle"],
]);

describe("the stylesheet names sides by reading order", () => {
  it("uses no screen-named side property outside the listed exceptions", () => {
    const found: string[] = [];
    for (const block of blocksOf()) {
      for (const m of block.body.matchAll(PHYSICAL)) {
        const prop = m[1] ?? "";
        if (KEEP_PHYSICAL.has(`${block.selector}|${prop}`)) continue;
        found.push(
          `${siteOf(block.at + (m.index ?? 0))}  ${block.selector} { ${prop} } ` +
            `-- write the inline-start/inline-end form, or add it to KEEP_PHYSICAL with the reason`,
        );
      }
    }
    expect(found).toEqual([]);
  });

  it("aligns text to start and end, never to left and right", () => {
    const found: string[] = [];
    for (const block of blocksOf()) {
      for (const m of block.body.matchAll(PHYSICAL_ALIGN)) {
        found.push(
          `${siteOf(block.at + (m.index ?? 0))}  ${block.selector} { text-align: ${m[1]} } ` +
            `-- ${m[1] === "left" ? "start" : "end"} reads the same in a left-to-right page and mirrors in a right-to-left one`,
        );
      }
    }
    expect(found).toEqual([]);
  });

  it("writes no four-value box shorthand whose right and left differ", () => {
    // The property scan's other blind spot, and the costliest one: `padding: a b c d` sets
    // right to `b` and left to `d`, so an asymmetric value is a `padding-left` written where no
    // search for `padding-left` will find it.
    //
    // Only the four-value form can be asymmetric: one, two and three values all give right and
    // left the same thing. The fix is `padding-block` plus `padding-inline: <start> <end>`.
    const BOX = /(?:^|[;{\s])(padding|margin|inset|border-(?:width|color|style))\s*:\s*([^;]+);/g;

    /** Top-level tokens, so `var(--a, 1px)` and `calc(1px + 2px)` each count as one. */
    const valueTokens = (value: string): string[] => {
      const out: string[] = [];
      let buf = "";
      let depth = 0;
      for (const ch of value) {
        if (ch === "(") depth++;
        else if (ch === ")") depth--;
        if (/\s/.test(ch) && depth === 0) {
          if (buf) out.push(buf);
          buf = "";
        } else buf += ch;
      }
      if (buf) out.push(buf);
      return out;
    };

    const found: string[] = [];
    for (const block of blocksOf()) {
      for (const m of block.body.matchAll(BOX)) {
        const parts = valueTokens(m[2] ?? "");
        if (parts.length !== 4) continue;
        const [, right, , left] = parts;
        if (right === left) continue;
        found.push(
          `${siteOf(block.at + (m.index ?? 0))}  ${block.selector} { ${m[1]}: ${parts.join(" ")} } ` +
            `-- right is ${right} and left is ${left}; split it into ${m[1]}-block and ${m[1]}-inline: ${left} ${right}`,
        );
      }
    }
    expect(found).toEqual([]);
  });

  it("fades no gradient sideways on a fixed angle", () => {
    // The blind spot the property scan has: a fixed-angle gradient like `.card-scrim`'s
    // opaque-to-clear fade at 90deg does not flip under `dir="rtl"`, so the title can cross to
    // the wrong side and sit on the see-through half. A sideways gradient must carry its
    // direction in a variable an `[dir="rtl"]` rule flips, the way `--scrim-angle` does.
    //
    // Only the horizontal angles matter: `180deg` fades downward and reads the same either way,
    // and a symmetric stop list (transparent, color, transparent) is its own mirror. Two
    // selectors are symmetric and stay exempt: the scanline shimmer fades transparent at both
    // ends, and the budget bar's dashes repeat identically whichever way they are read.
    const SYMMETRIC = new Set([".scanline-fill::after", ".budget-free"]);
    const found: string[] = [];
    for (const block of blocksOf()) {
      if (SYMMETRIC.has(block.selector)) continue;
      for (const m of block.body.matchAll(
        /linear-gradient\(\s*(90deg|270deg|to\s+(?:right|left))/g,
      )) {
        found.push(
          `${siteOf(block.at + (m.index ?? 0))}  ${block.selector} { linear-gradient(${m[1]}) } ` +
            `-- put the angle in a custom property and flip it under [dir="rtl"], or add it to SYMMETRIC with the reason it reads the same both ways`,
        );
      }
    }
    expect(found).toEqual([]);
  });

  it("keeps every exception real, so the list cannot outlive the rules it excuses", () => {
    // An exception for a selector nobody writes any more is a license sitting open for the
    // next author who happens to reuse the name. Each entry has to still be in the stylesheet.
    const live = new Set<string>();
    for (const block of blocksOf()) {
      for (const m of block.body.matchAll(PHYSICAL)) live.add(`${block.selector}|${m[1] ?? ""}`);
    }
    expect([...KEEP_PHYSICAL.keys()].filter((k) => !live.has(k))).toEqual([]);
  });
});
