// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment jsdom
//
// The chip ✕ renders the same shape it did before its declarations were shared.
//
// Six controls draw this shape. Five are a chip's ✕ and four of those now read
// `04-buttons.css`'s shared rule instead of carrying their own copy. Moving a declaration to a
// file that loads EARLIER is a cascade change: anything of equal specificity in between now
// wins where it used to lose. That is invisible in a diff and invisible in a green suite, so
// the shape is read back off the cascade rather than argued.
//
// The values below were captured from the tree before the shared rule existed. They are a
// baseline, not a specification: a change here is a change to what the operator sees, so
// updating a number means having looked at the control and decided the new one is right.
//
// Two limits of reading a stylesheet in jsdom, both stated rather than worked around:
//
//   - `var()` is not resolved, so a value reads back as the token that was written. That is
//     what this test wants: it is asking which DECLARATION won, and a token is a stabler
//     answer than the pixels it expands to.
//   - `border-width` is reported from the keyword rather than from `border-style: none`, where
//     a browser computes 0px either way, so `border: none` and `border: 0` disagree here and
//     agree on screen. `border-style` is pinned instead: with no style there is no border to
//     paint, whatever the width says.
//
// The two controls outside the shared rule are in the table too, so an exclusion is a row a
// reader can check rather than a claim (rule 145: a member that drops out of a walk has to be
// visible). `.bar-x` is a chip ✕ that keeps its own border and hover; `.nudge-x`
// dismisses a notice and was the sixth candidate the measurement pass looked at.

import { describe, expect, it } from "vitest";

import { CSS, siteOf } from "./test/stylesheet";

/** Each control in the ancestry it actually renders in, since color and font-size inherit. */
const CONTROLS: Record<string, string> = {
  ".filter-chip button": `<span class="filter-chip">a<button type="button">x</button></span>`,
  ".fchip-x":
    `<span class="filter-anchor"><span class="fchip">` +
    `<button type="button" class="fchip-body">a</button>` +
    `<button type="button" class="fchip-x">x</button></span></span>`,
  ".tag-chip button":
    `<div class="tag-editor"><div class="tag-chips"><span class="tag-chip">a` +
    `<button type="button">x</button></span></div></div>`,
  ".inst-chip .chip-x":
    `<span class="inst-chip"><span class="tick">t</span>` +
    `<button type="button" class="chip-edit">a</button>` +
    `<button type="button" class="chip-x"><span>x</span></button></span>`,
  ".bar-x":
    `<div class="bar-line"><span class="bar-src">a</span><span class="bar-set">b</span>` +
    `<button type="button" class="bar-x">x</button></div>`,
  ".nudge-x":
    `<div class="scan-nudge"><span class="nudge-text">a</span><span class="nudge-actions">` +
    `<button type="button" class="primary sm">Show latest</button>` +
    `<button type="button" class="nudge-x">x</button></span></div>`,
};

/** Everything any of the six declares, plus what it inherits. A property left out of this list
 *  is a property this test would not notice moving. */
const PROPS = [
  "display",
  "align-items",
  "justify-content",
  "place-items",
  "width",
  "height",
  "min-width",
  "min-height",
  "background-color",
  "background-image",
  "border-top-style",
  "border-radius",
  "box-shadow",
  "color",
  "cursor",
  "font-family",
  "font-size",
  "font-weight",
  "line-height",
  "padding-top",
  "padding-right",
  "padding-bottom",
  "padding-left",
  "opacity",
  "overflow-wrap",
] as const;

const SHARED = {
  display: "inline-flex",
  "align-items": "center",
  "justify-content": "center",
  "place-items": "",
  width: "auto",
  height: "auto",
  "min-width": "var(--tap-min)",
  "min-height": "var(--tap-min)",
  "background-color": "rgba(0, 0, 0, 0)",
  "background-image": "none",
  "border-top-style": "none",
  "border-radius": "var(--radius-pill)",
  "box-shadow": "",
  cursor: "pointer",
  "font-family": 'system-ui, -apple-system, "Segoe UI", sans-serif',
  "font-size": "var(--text-lg)",
  "font-weight": "var(--weight-semi)",
  "line-height": "1",
  "padding-top": "0px",
  "padding-right": "0px",
  "padding-bottom": "0px",
  "padding-left": "0px",
  opacity: "1",
  "overflow-wrap": "normal",
};

const EXPECTED: Record<string, Record<string, string>> = {
  // Two chips in the queue's filter row, in the accent tone they inherit from the pill.
  ".filter-chip button": { ...SHARED, color: "var(--accent-text)" },
  ".fchip-x": { ...SHARED, color: "var(--accent-text)", opacity: "0.75" },
  // The list modal's tag chip: protect tone, and the chip wraps an operator-typed tag.
  ".tag-chip button": { ...SHARED, color: "var(--protect)", "overflow-wrap": "anywhere" },
  // The setup wizard's instance chip: bold and muted at the chip's own text size.
  ".inst-chip .chip-x": {
    ...SHARED,
    color: "var(--muted)",
    "font-size": "var(--text-sm)",
    "font-weight": "var(--weight-bold)",
    "overflow-wrap": "anywhere",
  },
  // Outside the shared rule, and the padding no longer says so: it sizes off --tap-min
  // directly and resets 04-buttons.css's `button` padding itself, so the box is the 24x24 its
  // own `width` declares (#736). It was 29.2px wide against the 24 it named, because a
  // specified width under padding plus border floors at padding plus border. It stays out of
  // the shared rule for the border and the red hover, which that rule would take.
  ".bar-x": {
    ...SHARED,
    width: "var(--tap-min)",
    height: "var(--tap-min)",
    "min-width": "auto",
    "min-height": "auto",
    color: "var(--muted)",
    "font-weight": "normal",
    "padding-top": "0px",
    "padding-right": "0px",
    "padding-bottom": "0px",
    "padding-left": "0px",
  },
  // Outside it too, and further out: a grid, a square corner, an SVG rather than a glyph.
  ".nudge-x": {
    ...SHARED,
    display: "inline-grid",
    "align-items": "normal",
    "justify-content": "normal",
    "place-items": "center",
    "border-radius": "var(--radius-sm)",
    "box-shadow": "none",
    color: "var(--muted)",
    "font-size": "15px",
    "font-weight": "normal",
    "line-height": "0",
    "padding-top": "4.8px",
    "padding-right": "4.8px",
    "padding-bottom": "4.8px",
    "padding-left": "4.8px",
  },
};

function shapeOf(html: string): Record<string, string> {
  const host = document.createElement("div");
  host.id = "root";
  host.innerHTML = html;
  document.body.appendChild(host);
  const buttons = host.querySelectorAll("button");
  const target = buttons[buttons.length - 1];
  if (target === undefined) throw new Error("the markup under test renders no button");
  const style = getComputedStyle(target);
  return Object.fromEntries(PROPS.map((p) => [p, style.getPropertyValue(p)]));
}

describe("the chip dismiss button", () => {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.appendChild(style);

  // Pinned so a control that stops matching the walk fails here, rather than quietly leaving
  // the comparison and taking its proof with it (rule 145).
  it("is measured at all six sites of that shape", () => {
    expect(Object.keys(CONTROLS)).toHaveLength(6);
    expect(Object.keys(EXPECTED).sort()).toEqual(Object.keys(CONTROLS).sort());
  });

  for (const [name, html] of Object.entries(CONTROLS)) {
    it(`renders ${name} at the shape it had before the rule was shared`, () => {
      expect(shapeOf(html)).toEqual(EXPECTED[name]);
    });
  }

  it("declares the shared shape once, ahead of every file that overrides it", () => {
    // Two of the four keep a smaller rule later in the cascade, the two the loop below reads.
    // Declared after either, the shared rule would win at equal specificity and flatten the
    // differences the table above pins.
    //
    // Each override is searched for PAST the shared block's closing brace. `.inst-chip
    // .chip-x {` matches the shared group's own last selector first, so a search from zero
    // compares the block against itself and holds even with 29-setup's override deleted.
    const shared = CSS.indexOf(".tag-chip button,");
    expect(shared).toBeGreaterThan(-1);
    // `.tag-chip button,` with the comma is the shared group's third selector and appears
    // nowhere else; 34-lists.css is left with `:hover` alone.
    expect(CSS.indexOf(".tag-chip button,", shared + 1)).toBe(-1);
    const afterShared = CSS.indexOf("}", shared);
    expect(afterShared).toBeGreaterThan(shared);
    for (const later of [".fchip-x {", ".inst-chip .chip-x {"]) {
      expect(CSS.indexOf(later, afterShared)).toBeGreaterThan(afterShared);
    }
  });

  // --- each chip's own hover tint has to beat the generic button hover (#740) ------------
  //
  // The shared rule above owns the RESTING shape. Hover is deliberately not shared: each of
  // these four paints its own tone -- accent on the two filter chips, protect on the tag chip,
  // condemn on the instance chip -- so there is nothing to fold them into. What they do share
  // is an opponent, `04-buttons.css`'s `button:hover:not(:disabled)`, which sets the generic
  // gray on every button in the app.
  //
  // `.fchip-x:hover` lost to it: (0,2,0) against (0,2,1), so the gray won on specificity
  // whatever the load order and the accent line never painted. Two of its siblings only TIE
  // and win on order, which is why this reads both numbers rather than either alone.
  //
  // Read off the selector text rather than off a computed style, because jsdom's
  // `getComputedStyle` does not apply `:hover` at all -- a rendered test here would pass
  // against every one of these rules deleted (rule 118).

  /** (id, class, type) counts, enough for the selectors this file reads. */
  function specificity(selector: string): [number, number, number] {
    const ids = selector.match(/#[\w-]+/g)?.length ?? 0;
    // Classes, attribute selectors and pseudo-CLASSES share the middle column. `:not()` adds
    // nothing itself; its argument counts, which is why it is unwrapped rather than stripped.
    const flat = selector.replace(/:not\(([^)]*)\)/g, "$1");
    const classes = flat.match(/\.[\w-]+|\[[^\]]+\]|:[\w-]+(?!\()/g)?.length ?? 0;
    // Pseudo-ELEMENTS count as types, and none of these selectors carries one.
    const types = flat.replace(/[.#:[][^\s>+~]*/g, " ").match(/\b[a-z]+\b/g)?.length ?? 0;
    return [ids, classes, types];
  }

  const GENERIC_HOVER = "button:hover:not(:disabled)";
  /** Every chip dismiss with a hover of its own, and the tone each one paints. */
  const HOVERS: Record<string, string> = {
    ".filter-chip button:hover": "--accent",
    ".fchip .fchip-x:hover": "--accent",
    ".tag-chip button:hover": "--protect",
    ".inst-chip .chip-x:hover": "--condemn",
  };

  it("counts the generic button hover the way the browser does", () => {
    // The opponent's own number, asserted rather than assumed: every comparison below is
    // against it, so a specificity function that read it wrong would pass all four (rule 119).
    expect(specificity(GENERIC_HOVER)).toEqual([0, 2, 1]);
    expect(specificity(".fchip-x:hover")).toEqual([0, 2, 0]);
    expect(specificity(".fchip .fchip-x:hover")).toEqual([0, 3, 0]);
  });

  for (const [selector, tone] of Object.entries(HOVERS)) {
    it(`paints ${selector} in ${tone}, not the generic gray`, () => {
      const at = CSS.indexOf(`${selector} {`);
      expect(at, `${selector} is not in the stylesheet`).toBeGreaterThan(-1);

      const generic = CSS.indexOf(`${GENERIC_HOVER} {`);
      expect(generic).toBeGreaterThan(-1);

      const mine = specificity(selector);
      const theirs = specificity(GENERIC_HOVER);
      const wins =
        mine[0] > theirs[0] ||
        (mine[0] === theirs[0] && mine[1] > theirs[1]) ||
        (mine[0] === theirs[0] && mine[1] === theirs[1] && mine[2] > theirs[2]);
      const ties = mine[0] === theirs[0] && mine[1] === theirs[1] && mine[2] === theirs[2];

      expect(
        wins || (ties && at > generic),
        `${selector} is ${mine.join(",")} against ${GENERIC_HOVER}'s ${theirs.join(",")} ` +
          `at ${siteOf(at)}, so the generic gray paints instead of ${tone}`,
      ).toBe(true);

      // And it really is the tone, not just a rule that wins with something else in it.
      const body = CSS.slice(at, CSS.indexOf("}", at));
      expect(body).toContain(tone);
    });
  }
});
