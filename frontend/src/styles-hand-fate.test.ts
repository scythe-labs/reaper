// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A hand decision outranks the scan verdict under it, on the one surface that wears both.
//
// Rule 49: a fate-bearing cell colors by the item's fate, and a reap the engine cannot honor yet
// reads dashed red rather than the plain condemned outline beside it. Every selector in that
// language is a single class, so nothing but SOURCE ORDER decides which wins -- and the season
// strip square is where it is decided, because `SeasonStrip` puts a scan-verdict class and an
// override class on the same button. The score badge and the chips take one class each from
// `handFate`, so their tones cannot collide and are not walked here.
//
// What goes wrong without this is quiet. Moving `.strip-ov-reap-refused` above `.strip-condemn`
// leaves a held reap wearing the condemned square's inset ring and solid outline, which is the
// one thing rule 49 says it must never look like: a decision Reaper is acting on.

import { describe, expect, it } from "vitest";

import { CSS, siteOf } from "./test/stylesheet";

/** The scan verdicts a square can be based on. `SeasonStrip` writes `strip-${mark.verdict}`, so
 *  this is the `Verdict` union, and a fourth verdict fails the count below rather than slipping
 *  through unwalked (rule 145). */
const VERDICT_CLASSES = ["strip-condemn", "strip-protect", "strip-abstain"];

/** The hand decisions that paint over one, from `handFate`'s four non-verdict fates. */
const OVERRIDE_CLASSES = [
  "strip-ov-spare",
  "strip-ov-spare-expired",
  "strip-ov-reap",
  "strip-ov-reap-refused",
];

/** Comments blanked, so prose naming a class is not read as a rule. */
const CODE = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));

/** Where each single-class rule for `cls` sits, outside any at-rule.
 *
 *  The forced-colors block restates every one of these classes to swap the border STYLE, which is
 *  a separate ordering with its own comment in the stylesheet. Folding it in here would compare
 *  offsets across two cascades and answer for neither. */
function offsetsOf(cls: string): number[] {
  const out: number[] = [];
  let depth = 0;
  let inAtRule = false;
  for (const m of CODE.matchAll(/([^{}]*)\{|\}/g)) {
    if (m[0] === "}") {
      depth--;
      if (depth === 0) inAtRule = false;
      continue;
    }
    const head = (m[1] ?? "").trim().replace(/\s+/g, " ");
    depth++;
    if (head.startsWith("@")) {
      inAtRule = true;
      continue;
    }
    if (inAtRule) continue;
    if (head.split(",").some((s) => s.trim() === `.${cls}`)) out.push(m.index ?? 0);
  }
  return out;
}

describe("the season strip's hand-override rules", () => {
  it("are all present, one top-level rule each", () => {
    // The population the ordering test walks. A class renamed or merged into a grouped selector
    // leaves that test with nothing to compare and passing on an empty set, so the count is
    // pinned first (rule 145).
    const found = [...VERDICT_CLASSES, ...OVERRIDE_CLASSES].map(
      (c) => `${c}: ${offsetsOf(c).length}`,
    );
    expect(found).toEqual([
      "strip-condemn: 1",
      "strip-protect: 1",
      "strip-abstain: 1",
      "strip-ov-spare: 1",
      "strip-ov-spare-expired: 1",
      "strip-ov-reap: 1",
      "strip-ov-reap-refused: 1",
    ]);
  });

  it("are declared after every scan-verdict base, so a hand decision wins", () => {
    // Source order, because every one of these selectors is a single class and specificity
    // therefore ties. This is the assertion that covers `border` and `background`: jsdom drops a
    // shorthand carrying `var()`, so the computed-style test below cannot see the dashed edge.
    const inverted: string[] = [];
    for (const ov of OVERRIDE_CLASSES) {
      const ovAt = offsetsOf(ov)[0] ?? -1;
      for (const verdict of VERDICT_CLASSES) {
        const verdictAt = offsetsOf(verdict)[0] ?? -1;
        if (ovAt < verdictAt) {
          inverted.push(`.${ov} (${siteOf(ovAt)}) loses to .${verdict} (${siteOf(verdictAt)})`);
        }
      }
    }
    expect(inverted).toEqual([]);
  });

  it("cancel the condemned square's ring on every combination the strip can draw", () => {
    // The computed half. `box-shadow` is the one property here jsdom resolves through, and it is
    // the right one: `.strip-condemn` is the only verdict carrying a ring, and every override
    // sets `box-shadow: none` precisely to drop it. A held reap that kept the ring would read as
    // the settled condemnation beside it.
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);

    const wrong: string[] = [];
    for (const verdict of VERDICT_CLASSES) {
      for (const ov of OVERRIDE_CLASSES) {
        const el = document.createElement("button");
        el.className = `strip-sq ${verdict} ${ov}`;
        document.body.appendChild(el);
        const shadow = getComputedStyle(el).boxShadow;
        if (shadow !== "none") wrong.push(`${el.className} -> box-shadow: ${shadow || "(unset)"}`);
      }
    }
    expect(wrong).toEqual([]);

    // And the base it has to beat really does carry one, or the assertion above passes on a
    // stylesheet where nothing was ever at stake (rule 141).
    const base = document.createElement("button");
    base.className = "strip-sq strip-condemn";
    document.body.appendChild(base);
    expect(getComputedStyle(base).boxShadow).toContain("inset");
  });
});

describe("the score badge's hand-fate tones", () => {
  it("are declared after the scan-verdict tones they replace", () => {
    // Same direction as the strip, one file apart: `.score-condemn` and friends are in
    // 11-queue-chrome.css and the four hand tones are in 23-queue-chips.css, grouped with the
    // chip and status twins that wear them. `handFate` hands `Score` one class, so these cannot
    // collide today; this pins the claim 11-queue-chrome.css's comment makes about where they
    // went and which way round they load.
    const verdictAt = Math.max(
      ...["score-condemn", "score-protect", "score-abstain"].flatMap(offsetsOf),
    );
    const early: string[] = [];
    for (const tone of ["score-reap", "score-spare", "score-refused", "score-spare-expired"]) {
      const at = offsetsOf(tone)[0] ?? -1;
      if (at < verdictAt) {
        early.push(`.${tone} (${siteOf(at)}) loses to the scan verdict (${siteOf(verdictAt)})`);
      }
    }
    expect(early).toEqual([]);
  });

  it("share one rule with the chip and status pill wearing the same tone", () => {
    // The 12 blocks this replaced were four tones written out three times, and the three families
    // are one language: `handFate` picks the tone, and the score badge, the card chip and the
    // status pill render it. A tone split back apart is three places to change one color.
    const pairs: [string, string, string][] = [
      ["score-reap", "chip-hand-reap", "status-hand-reap"],
      ["score-spare", "chip-hand-spare", "status-hand-spare"],
      ["score-spare-expired", "chip-spare-expired", "status-spare-expired"],
      ["score-refused", "chip-reap-refused", "status-reap-held"],
    ];
    const split: string[] = [];
    for (const trio of pairs) {
      const offsets = trio.map((c) => offsetsOf(c)[0] ?? -1);
      if (new Set(offsets).size !== 1) {
        split.push(trio.map((c, i) => `.${c} (${siteOf(offsets[i] ?? 0)})`).join(" / "));
      }
    }
    expect(split).toEqual([]);
  });
});
