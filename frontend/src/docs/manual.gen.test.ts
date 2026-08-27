// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// Generates the manual pages and checks them for drift in one file.
//
// `toMatchFileSnapshot` writes the page to disk when run with `-u` (`npm run gen-manual`), and
// fails when the committed page no longer matches what the blocks produce. Generating and
// checking through the same call means the generator and the checker can never disagree with
// each other, over every page in DOCS.
//
// `blockToMdx`'s switch statement is exhaustive with no default case, so a new block kind added
// to `blocks.ts` fails to compile here instead of silently dropping out of the manual. That
// catches more than a test could, which is why no test below lists the block kinds by hand.

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

// Tests the escaper against both the text it must escape and the text it must leave alone,
// not just the happy path.
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

  // The "understanding-policy" doc has a step title with a quoted phrase, so this case is real.
  // `title="… \"why\" …"` is invalid JSX: a quote-delimited attribute has no backslash escape.
  // The expression form (`title={"..."}`) does parse.
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

  const tableDoc = (cellText: string) => ({
    ...(armingDoc as NonNullable<typeof armingDoc>),
    body: [{ kind: "table" as const, head: ["A"], rows: [[cellText]] }],
  });

  // `escapeText` leaves a backslash alone inside a code span on purpose, since MDX does not
  // interpolate there and an escape would show up on the operator's screen. A table cell then
  // escapes the pipe on top of that, and the two only combine correctly when the number of
  // backslashes before the pipe is even. The row splits into cells before the code span is
  // parsed, and the splitter consumes exactly one backslash. The counts below were measured
  // against remark-gfm's actual behavior, not derived from the spec.
  it("carries an even run of backslashes before a pipe in a code span", () => {
    expect(docToMdx(tableDoc("`a|b`"))).toContain("| `a\\|b` |");
    expect(docToMdx(tableDoc("`a\\\\|b`"))).toContain("| `a\\\\\\|b` |");
  });

  it("refuses an odd run, which GFM cannot spell and which splits the row in two", () => {
    // Emitted as `a\\|b`, which the splitter reads as an escaped backslash followed by a bare
    // pipe. That splits a one-column table into two, with no warning.
    expect(() => docToMdx(tableDoc("`a\\|b`"))).toThrow(/odd run of backslashes/);
    expect(() => docToMdx(tableDoc("`a\\\\\\|b`"))).toThrow(/odd run of backslashes/);
  });

  it("leaves a backslash alone where no pipe follows it in the span", () => {
    expect(docToMdx(tableDoc("`a\\b`"))).toContain("| `a\\b` |");
    expect(docToMdx(tableDoc("back \\ slash"))).toContain("| back \\\\ slash |");
  });
});
