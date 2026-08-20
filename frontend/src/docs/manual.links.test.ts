// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Every cross-reference in the docs points at a page and a section that exist.
//
// A doc string writes `[text](doc-id#section)`. Both surfaces resolve that id themselves --
// the app opens the modal to it, the generator turns it into a path -- so a typo is not a
// broken link on one screen, it is a different wrong thing on each. In the app the button
// opens an empty modal; in the manual the path 404s. Neither shows up in a type error, and
// prose asking authors to check ids does nothing, so this is the gate instead (rule 72's
// "write the gate" clause).

import { describe, expect, it } from "vitest";

import type { Block } from "./blocks";
import { DOCS } from "./registry";

/** `[text](doc-id#section)`, the same shape DocBody's REF and toMdx's resolveRefs read. */
const REF = /\[([^\]\n]+)\]\(([a-z0-9-]+)(?:#([a-z0-9-]+))?\)/g;

/** Every prose string in a block. Diagrams carry no cross-references: their text is drawn
 *  into a flow node, never parsed for inline syntax. */
function strings(b: Block): string[] {
  switch (b.kind) {
    case "h":
    case "p":
    case "callout":
      return [b.text];
    case "list":
      return b.items;
    case "steps":
      return b.items.flatMap((s) => [s.title, s.text]);
    case "table":
      return [...b.head, ...b.rows.flat()];
    case "diagram":
      return [];
  }
}

/** The `id` of every h2/h3 on a page, which is what an anchor may name. */
function sectionIds(docId: string): Set<string> {
  const doc = DOCS.find((d) => d.id === docId);
  return new Set(
    (doc?.body ?? []).flatMap((b) => (b.kind === "h" && b.id !== undefined ? [b.id] : [])),
  );
}

const refs = DOCS.flatMap((doc) =>
  doc.body
    .flatMap(strings)
    .flatMap((text) => [...text.matchAll(REF)])
    .map((m) => ({ from: doc.id, label: m[1] ?? "", target: m[2] ?? "", anchor: m[3] })),
);

describe("doc cross-references", () => {
  it("point at a page that exists", () => {
    const ids = new Set(DOCS.map((d) => d.id));
    const dead = refs.filter((r) => !ids.has(r.target));
    expect(dead.map((r) => `${r.from}: [${r.label}](${r.target}) -- no doc has that id`)).toEqual(
      [],
    );
  });

  it("point at a section that exists", () => {
    const dead = refs.filter((r) => r.anchor !== undefined && !sectionIds(r.target).has(r.anchor));
    expect(
      dead.map(
        (r) =>
          `${r.from}: [${r.label}](${r.target}#${r.anchor}) -- ` +
          `${r.target} has no section with that id`,
      ),
    ).toEqual([]);
  });

  it("never point at their own page without naming a section", () => {
    // Same page WITH an anchor is fine and useful: the modal re-scrolls on a fresh nonce and
    // the manual jumps to the heading. Same page with NO anchor is the no-op -- it reopens
    // what you are already reading.
    const selfies = refs.filter((r) => r.target === r.from && r.anchor === undefined);
    expect(selfies.map((r) => `${r.from}: [${r.label}](${r.target})`)).toEqual([]);
  });
});
