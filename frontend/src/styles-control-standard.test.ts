// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Rule 40's one control standard, held over the whole stylesheet.
//
// The standard is written out in `styles/00-tokens.css` beside `--control-pad`. Ten blocks
// re-type its six declarations, and nothing checked they agree. Two had already stopped.
// `.set-row .set-control input, .set-row .set-control select` and `.log-search` set `font-size`
// and no `font`. A form control does not inherit its family, so those boxes rendered in the
// browser's default form font while their labels rendered in the app's. The block declares five
// of the six fields correctly. The symptom reads as a size difference, not a wrong typeface.
//
// A block is ON the standard when it pads with `--control-pad`. That is the token's own
// definition ("a control that wants different padding is not on the standard") and it needs no
// exemption list: `.qty` and `.hex-join` declare no padding, and `.tag-chips` pads with
// `--space-sm`, so all three joined wrappers fall out of the walk on their own.
//
// WHAT THIS WALK CANNOT SEE (rule 147):
//   - A control written entirely in literals, which never mentions `--control-pad` and so is
//     never collected. The literal ban below covers the one spelling that has happened, the
//     padding pair typed out. No other spelling is matched.
//   - `.qty`, `.hex-join` and `.tag-chips`, which put the border and fill on a wrapper and the
//     padding on its children. Deliberate. `.qty`'s reason is in `00-tokens.css`,
//     `.hex-join`'s at `27-settings-rows.css:233`.
//   - A block reaching the same padding through `padding-block` / `padding-inline`. Those cannot
//     carry the token, and their two values are `0.42rem` and `0.6rem` separately, which the ban
//     does not match either.
//   - The focus ring. It is not gated here because a missing one falls back to `01-base.css`'s
//     `:focus-visible` and is visible; the font falls back silently, which is why it drifted.
//   - A `@keyframes` step is a block like any other, so `from` and `0%` are collected too. None
//     of the nine sets pads with the token.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { blocksOf, siteOf } from "./test/stylesheet";

const PAD = "var(--control-pad)";

/** The five declarations that come with the padding, from `00-tokens.css`. */
const STANDARD: readonly (readonly [string, string])[] = [
  ["border", "1px solid var(--border-strong)"],
  ["border-radius", "var(--radius-sm)"],
  ["background", "var(--bg)"],
  ["color", "var(--text)"],
  ["font", "inherit"],
];

// Reconciled by hand at this tip, one line per block (rule 145):
//   08-sheet.css        .local-form input
//   14-policy-editor    .rule-control input[type="number"]
//   14-policy-editor    .rule-control select, .bar-foot select
//   14-policy-editor    .condition-add select, .condition-add input
//   20-queue-toolbar    .search-input
//   26-settings         .settings-picker select
//   26-settings         .field-sm input, .field-sm select
//   27-settings-rows    .set-row .set-control input, .set-row .set-control select
//   28-settings-log     .log-search
//   29-setup            .manual-row input
// A pass/fail assertion cannot tell a block that complies from one that fell out of the walk,
// so the count is pinned. An eleventh block fails here and has to be looked at before the
// number moves.
const ON_THE_STANDARD = 10;

/** A block's declarations, last write winning, as CSS itself resolves them. */
function declarations(body: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const d of body.split(";")) {
    const at = d.indexOf(":");
    if (at === -1) continue;
    out.set(
      d.slice(0, at).trim(),
      d
        .slice(at + 1)
        .trim()
        .replace(/\s+/g, " "),
    );
  }
  return out;
}

function onTheStandard() {
  return blocksOf()
    .map((b) => ({ ...b, decls: declarations(b.body) }))
    .filter((b) => b.decls.get("padding") === PAD);
}

describe("rule 40's control standard", () => {
  it("is carried by the blocks reconciled by hand, and no others", () => {
    const found = onTheStandard().map((b) => `${siteOf(b.at)}  ${b.selector}`);
    expect(found.length, `blocks padding with ${PAD}:\n${found.join("\n")}`).toBe(ON_THE_STANDARD);
  });

  it("declares every field of the standard at every one of them", () => {
    // A block on the standard states all six, or it is not on the standard and should not be
    // padding with the token.
    const wrong: string[] = [];
    for (const b of onTheStandard()) {
      for (const [prop, value] of STANDARD) {
        const got = b.decls.get(prop);
        if (got === value) continue;
        wrong.push(
          `${siteOf(b.at)}  ${b.selector}  ${prop}: ${got ?? "(absent)"}  want ${prop}: ${value}`,
        );
      }
    }
    expect(wrong).toEqual([]);
  });

  it("has no site spelling the padding out instead of reading the token", () => {
    // `--control-pad` exists because the pair was typed out at twelve sites. `.search-input`
    // was the last site still spelling it. It wrote the pair as the first two values of a
    // four-value padding, to add its icon clearance. `padding-left` after the token says the
    // same thing and stays on the token.
    //
    // The match is on the FIRST TWO components, which is what re-states the pair. Not on the
    // two values appearing anywhere: `.qty input[type="number"]` pads
    // `0.42rem 0.35rem 0.42rem 0.6rem` and is not the pair. Its sides are 0.35 and 0.6,
    // lopsided so the number sits against its unit. A substring test failed on it, which is
    // how this line came to exist.
    const literal: string[] = [];
    for (const b of blocksOf()) {
      for (const [prop, value] of declarations(b.body)) {
        if (prop === "--control-pad") continue;
        if (/^0\.42rem\s+0\.6rem(\s|$)/.test(value)) {
          literal.push(`${siteOf(b.at)}  ${b.selector}  ${prop}: ${value}`);
        }
      }
    }
    expect(literal).toEqual([]);
  });

  it("is worded the same in 00-tokens.css and 01-base.css", () => {
    // Rule 144: the standard is written out twice besides the declarations. `00-tokens.css`
    // carries the standard itself. `01-base.css`'s iOS-zoom comment rests on it to explain why
    // that guard only has to hold the SIZE. If `font: inherit` stops being the standard, the
    // reasoning behind the no-zoom floor is wrong too, and nothing else says so. This test
    // fails when either sentence stops saying it, so the prose cannot drift out from under the
    // declarations above.
    for (const [file, sentence] of [
      ["styles/00-tokens.css", "font: inherit"],
      ["styles/01-base.css", "the control standard is font: inherit"],
    ] as const) {
      // Whitespace collapsed: both sentences sit inside wrapped comments, so the phrase spans
      // a line break at one of them and a raw substring test misses it.
      const text = readFileSync(new URL(`./${file}`, import.meta.url), "utf8").replace(/\s+/g, " ");
      expect(text, `${file} no longer states the control standard's font`).toContain(sentence);
    }
  });
});

describe("the control-standard walk itself", () => {
  it("reads a stylesheet with rules in it", () => {
    // Rule 118's shape for a scanner: both empty-list assertions above pass against an empty
    // walk. The count assertion does not, being the one that reads a number rather than a set.
    expect(blocksOf().length).toBeGreaterThan(1000);
    expect(onTheStandard().length).toBeGreaterThan(0);
  });
});
