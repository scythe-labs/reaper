// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
// One invariant, held over the whole stylesheet: a rule that lifts a surface clear of the phone's
// section bar is declared AFTER that surface's own `bottom`, never before it.
//
// The bar is `position: fixed; bottom: 0` under 900px, so every other surface anchored to the foot
// of the screen has to clear it, and each does that with a `bottom` reading `--navbar-h`. A media
// query adds NO specificity: at equal specificity the later declaration wins, so an override
// written above the base rule loses to it and does nothing at all. Nothing about it looks wrong --
// the `calc()` is right there, citing the right custom property.
//
// This is not hypothetical. All three overrides shipped in the block that declares the bar, which
// sits ~4,000 lines above `.bulk-bar`'s base rule, so all three were inert. The bulk-action bar
// then sat behind the nav icons on a phone: the only way to spare or reap several items at once,
// with its buttons unreachable and a tap landing on a nav icon instead, which changes view and
// drops the selection. `.savebar` was the same shape with a worse ending -- it is the only save
// affordance on the policy page (rule 43), so the tap that should save a draft navigated away and
// discarded it silently.
//
// A test rather than a fourth comment, because the next surface to grow a bottom anchor will be
// written the same way (rule 72: the fix lands on every twin, including ones not yet added).
import { describe, expect, it } from "vitest";

import { CSS as css, siteOf } from "./test/stylesheet";

type Rule = { selector: string; body: string; at: number; inPhoneMedia: boolean };

/** Walk the stylesheet brace by brace, so nested at-rules are tracked rather than guessed at.
 *  Returns every style rule with the offset it is declared at and whether a max-width media
 *  query at or below the bar's breakpoint encloses it. */
function parse(src: string): Rule[] {
  const clean = src.replace(/\/\*[\s\S]*?\*\//g, (m) => " ".repeat(m.length));
  const rules: Rule[] = [];
  const stack: { phone: boolean }[] = [];
  let preludeStart = 0;
  for (let i = 0; i < clean.length; i++) {
    const ch = clean[i];
    if (ch === "{") {
      const prelude = clean.slice(preludeStart, i).trim();
      if (prelude.startsWith("@")) {
        // A max-width at or under 900px puts its contents on top of the bar.
        const w = prelude.match(/max-width:\s*(\d+)px/);
        stack.push({ phone: w !== undefined && w !== null && Number(w[1]) <= 900 });
      } else {
        const close = matchBrace(clean, i);
        rules.push({
          selector: prelude,
          body: clean.slice(i + 1, close),
          at: preludeStart,
          inPhoneMedia: stack.some((s) => s.phone),
        });
        i = close;
      }
      preludeStart = i + 1;
    } else if (ch === "}") {
      stack.pop();
      preludeStart = i + 1;
    } else if (ch === ";" && stack.length === 0) {
      preludeStart = i + 1;
    }
  }
  return rules;
}

function matchBrace(src: string, open: number): number {
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return i;
  }
  return src.length;
}

const declaresBottom = (body: string) => /(^|[;{\s])bottom\s*:/.test(body);

describe("the stylesheet: bottom-bar clearance", () => {
  const rules = parse(css);
  const lifts = rules.filter(
    (r) => r.inPhoneMedia && declaresBottom(r.body) && r.body.includes("--navbar-h"),
  );

  // Guard against the vacuous pass: if the parse ever stops finding these, the ordering
  // assertion below would hold over an empty set and report a green gate for no checking at all.
  it("finds the surfaces that have to clear the bar", () => {
    const selectors = lifts.map((r) => r.selector);
    expect(selectors).toEqual(expect.arrayContaining([".bulk-bar", ".scan-toast", ".savebar"]));
  });

  it("declares every lift after the base rule it has to beat", () => {
    const losing = lifts.flatMap((lift) => {
      const base = rules.filter(
        (r) => !r.inPhoneMedia && r.selector === lift.selector && declaresBottom(r.body),
      );
      // Same specificity, so only source order decides: the lift must come last.
      return base
        .filter((b) => b.at > lift.at)
        .map((b) => `${lift.selector}: lift at ${siteOf(lift.at)}, base at ${siteOf(b.at)}`);
    });
    expect(losing).toEqual([]);
  });
});
