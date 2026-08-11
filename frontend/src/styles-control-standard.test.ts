// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Rule 40's one control standard, held over the whole stylesheet.
//
// The standard is written out in `styles/00-tokens.css` beside `--control-pad`, and until this
// file nothing checked it: ten blocks re-typed the same six declarations and agreed only because
// each author copied the last one. Two of them had already stopped agreeing. `.set-row
// .set-control input` and `.log-search` set `font-size` and no `font`, and an <input> does not
// inherit its family, so every box in the Settings control column and the Logs search box
// rendered in the browser's own form font while the labels beside them rendered in the app's.
// That is invisible in review twice over: the block declares five of the six fields correctly,
// and the symptom reads as a size difference rather than a wrong typeface.
//
// A block is ON the standard when it pads with `--control-pad`. That is the token's own
// definition ("a control that wants different padding is not on the standard") and it needs no
// exemption list: the joined wrappers put no padding on themselves, so `.qty`, `.hex-join` and
// `.tag-chips` drop out on their own rather than being named here.
//
// WHAT THIS WALK CANNOT SEE (rule 147):
//   - A control written entirely in literals, which never mentions `--control-pad` and so is
//     never collected. The literal ban below covers the one spelling that has actually happened,
//     the padding pair typed out; the rest is unreachable from source text.
//   - `.qty` and `.hex-join`, which split the chrome across a wrapper and its children on
//     purpose, so the number and its unit read as one box. Deliberate, and named in
//     `00-tokens.css`.
//   - A block reaching the same padding through `padding-block` / `padding-inline`. Those cannot
//     carry the token (it is a pair), so such a block must spell the literal, which the ban
//     catches.
//   - The focus ring. It is not gated here because a missing one falls back to `01-base.css`'s
//     `:focus-visible` and is visible; the font falls back silently, which is why it drifted.

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
// A count alone cannot tell a block that complies from one that fell out of the walk, so an
// eleventh block fails here and has to be looked at before the number moves.
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
  it("is carried by exactly ten blocks", () => {
    const found = onTheStandard().map((b) => `${siteOf(b.at)}  ${b.selector}`);
    expect(found.length, `blocks padding with ${PAD}:\n${found.join("\n")}`).toBe(ON_THE_STANDARD);
  });

  it("declares every field of the standard at every one of them", () => {
    // The whole gate. A block on the standard states all six, or it is not on the standard and
    // should not be padding with the token.
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
    // `--control-pad` exists because the pair was typed out at twelve sites. A thirteenth
    // undoes it, and `.search-input` was the last survivor: it wrote the pair as the first two
    // values of a four-value padding to add its icon clearance, where `padding-left` after the
    // token says the same thing and stays on the token.
    //
    // The match is on the FIRST TWO components, which is what re-states the pair, not on the
    // two rem values appearing anywhere in the value. `.qty input[type="number"]` pads
    // `0.42rem 0.35rem 0.42rem 0.6rem`, which contains that text and is not the pair: its
    // sides are 0.35 and 0.6, deliberately lopsided so the number sits against its unit. A
    // substring test failed on it, which is how this line came to exist.
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

  it("is stated in prose at both places that state it, in the same words", () => {
    // Rule 144: the standard is written out twice besides the declarations, and each copy was
    // written by someone reading the other. `00-tokens.css` carries the standard itself.
    // `01-base.css`'s iOS-zoom comment rests on it to explain why that guard only has to hold
    // the SIZE -- if `font: inherit` ever stops being the standard, the reasoning behind the
    // no-zoom floor is wrong too, and nothing else would say so. Both are named here so a
    // failure above tells a reader which sentences it just falsified.
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
    // Rule 118's shape for a scanner: every assertion above passes against an empty walk.
    expect(blocksOf().length).toBeGreaterThan(1000);
    expect(onTheStandard().length).toBeGreaterThan(0);
  });
});
