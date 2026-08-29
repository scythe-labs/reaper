// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
// This file checks one invariant over the whole stylesheet: a rule that lifts a surface clear of
// the phone's bottom section bar must be declared AFTER that surface's own `bottom` rule, never
// before it.
//
// The bar is `position: fixed; bottom: 0` under 900px, so every other surface anchored to the
// foot of the screen has to clear it, each with its own `bottom` reading `--navbar-h`. A media
// query adds no specificity of its own. At equal specificity, the later declaration in the file
// wins, so an override written above the bar's base rule loses to it and does nothing, even
// though the `calc()` looks correct and cites the right custom property.
//
// A rule declared in the wrong order looks fine in the CSS itself. If it lifts the bulk-action
// bar or the policy page's only save button, that control sits behind the phone's nav icons
// instead, so a tap lands on a nav icon and changes view instead of saving or selecting
// anything.
//
// A test catches this rather than a comment, because the next surface to grow a bottom anchor
// will be written the same way, and the fix has to land on every one of them, including ones
// not yet added.
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

  // This guards against a vacuous pass. If the parse ever stops finding these surfaces, the
  // ordering assertion below would hold over an empty set and report a green gate that checked
  // nothing.
  it("finds the surfaces that have to clear the bar", () => {
    const selectors = lifts.map((r) => r.selector);
    expect(selectors).toEqual(expect.arrayContaining([".bulk-bar", ".scan-toast", ".savebar"]));
  });

  it("declares every lift after the base rule it has to beat", () => {
    const losing = lifts.flatMap((lift) => {
      const base = rules.filter(
        (r) => !r.inPhoneMedia && r.selector === lift.selector && declaresBottom(r.body),
      );
      // Same specificity, so only source order decides. The lift must come last.
      return base
        .filter((b) => b.at > lift.at)
        .map((b) => `${lift.selector}: lift at ${siteOf(lift.at)}, base at ${siteOf(b.at)}`);
    });
    expect(losing).toEqual([]);
  });
});
