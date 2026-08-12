// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// `.notice` is a shared primitive, so its rules load before the feature files.
//
// They were written in `16-simulator.css` and stayed there long after `.notice` stopped being a
// simulator control, two thirds of the way down a 35-file load order (#779). Nothing was broken
// while they sat there, because no other file declared a `.notice` rule to conflict with. That
// is what makes the position worth pinning rather than leaving to whoever moves next: the first
// `.notice` variant added to an earlier file would have lost at equal specificity, and a rule
// losing on source order is invisible in a diff and invisible in a green suite.

import { describe, expect, it } from "vitest";

import { CSS, siteOf } from "./test/stylesheet";

/** Where a bare `.notice` rule may be declared. Both load before every feature file. */
const PRIMITIVE_FILES = ["styles/01-base.css", "styles/04-buttons.css"];

/** Every rule in the sheet, as its selector list and where the list starts. */
const RULE = /([^{}]+)\{[^{}]*\}/g;

/** A bare notice selector: the whole thing, not `.savebar .notice`. */
const BARE_NOTICE = /^\.notice(?:-[a-z]+)?$/;

/**
 * Comments blanked to the same length, so a `.notice {` written inside prose is not read as a
 * rule while every offset still resolves to its real line.
 */
const code = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));

describe("the notice rules", () => {
  it("are declared in a shared-primitive file, never a feature file", () => {
    // A feature file narrowing the primitive is fine and is the point of the ordering:
    // `.savebar .notice` outranks the base rule on specificity and is meant to. What must not
    // happen is a second BARE `.notice` rule, which wins or loses purely on which file loads
    // last -- the failure this position exists to prevent.
    const strays = [...code.matchAll(RULE)].flatMap((m) => {
      const list = m[1] ?? "";
      const at = m.index + (m[0].length - m[0].trimStart().length);
      return list
        .split(",")
        .map((s) => s.trim())
        .filter((s) => BARE_NOTICE.test(s))
        .map((selector) => ({ selector, site: siteOf(at) }));
    });

    const outside = strays.filter(
      ({ site }) => !PRIMITIVE_FILES.some((f) => site.startsWith(`${f}:`)),
    );

    expect(
      outside,
      `A bare .notice rule is declared outside the shared primitives: ${outside
        .map(({ selector, site }) => `${selector} at ${site}`)
        .join(", ")}. .notice renders across the app, so its base rules belong in ` +
        `${PRIMITIVE_FILES.join(" or ")}, early enough that a feature file can still narrow ` +
        `one deliberately. See #779.`,
    ).toEqual([]);

    // The walk has to be reading something, or it passes having checked nothing.
    expect(strays.length, "found no bare .notice rule anywhere").toBeGreaterThan(0);
  });
});
