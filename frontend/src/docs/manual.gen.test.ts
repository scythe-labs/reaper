// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The manual's generator and its drift gate, deliberately the same file.
//
// `toMatchFileSnapshot` writes the page when run with `-u` (that is `npm run gen-manual`) and
// fails when the committed page no longer matches what the blocks produce. Generating and
// checking through one call means the two cannot diverge, which is the failure a hand-written
// generator paired with a hand-written checker eventually has: the checker is the thing nobody
// updates. Rule 68 wants a committed, runnable generator and a drift test covering every
// generated artifact; this is both, over all of DOCS rather than one page.
//
// `blockToMdx`'s switch is exhaustive with no default, so a block kind added to `blocks.ts`
// fails to compile here before it can silently drop out of the manual. That is a stronger
// guarantee than a test could give, and it is why no test below enumerates the kinds.

import { describe, expect, it } from "vitest";
import { DOCS } from "./registry";
import { docToMdx, manualPath } from "./toMdx";

/** Repo-relative, from `frontend/src/docs/`. */
const MANUAL_ROOT = "../../../manual";

describe("the manual is generated from the in-app docs", () => {
  it.each(DOCS.map((doc, i) => [doc.id, doc, i + 1] as const))(
    "writes %s, and fails when it drifts",
    async (_id, doc, position) => {
      await expect(docToMdx(doc, position)).toMatchFileSnapshot(
        `${MANUAL_ROOT}/${manualPath(doc)}`,
      );
    },
  );
});

// Rule 147: the escaper is a source-text matcher, so it is proven against the spellings it
// accepts AND the ones it must leave alone, not only against the happy path.
describe("MDX escaping", () => {
  const armingDoc = DOCS.find((d) => d.id === "arming");

  it("escapes braces and angle brackets in prose", () => {
    const doc = {
      ...(armingDoc as NonNullable<typeof armingDoc>),
      body: [{ kind: "p" as const, text: "Set {this} to <that>" }],
    };
    expect(docToMdx(doc)).toContain("Set \\{this\\} to \\<that\\>");
  });

  it("leaves a code span alone, where MDX does not interpolate", () => {
    const doc = {
      ...(armingDoc as NonNullable<typeof armingDoc>),
      body: [{ kind: "p" as const, text: "Set `{this}` and {that}" }],
    };
    const out = docToMdx(doc);
    expect(out).toContain("`{this}`");
    expect(out).toContain("\\{that\\}");
  });

  // A quoted phrase in a step title is not hypothetical: understanding-policy ships one, and it
  // is what the first site build died on. `title="… \"why\" …"` is invalid JSX, because a
  // quote-delimited attribute has no backslash escape; the expression form is what parses.
  it("writes a title holding quotes as an expression, not a quoted literal", () => {
    const doc = {
      ...(armingDoc as NonNullable<typeof armingDoc>),
      body: [
        {
          kind: "steps" as const,
          items: [{ title: 'Read a few "why" panels.', text: "Then decide." }],
        },
      ],
    };
    const out = docToMdx(doc);
    expect(out).toContain('<Step title={"Read a few \\"why\\" panels."}>');
    expect(out).not.toContain('title="Read a few');
  });

  it("escapes a pipe inside a table cell, which would otherwise end the column", () => {
    const doc = {
      ...(armingDoc as NonNullable<typeof armingDoc>),
      body: [{ kind: "table" as const, head: ["A"], rows: [["x | y"]] }],
    };
    expect(docToMdx(doc)).toContain("| x \\| y |");
  });
});
