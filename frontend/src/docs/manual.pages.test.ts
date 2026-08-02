// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What is on disk under `manual/`, audited against what the docs declare.
//
// This is deliberately NOT in `manual.gen.test.ts`. That file WRITES the pages under `-u`, and a
// directory walk sharing a run with the writer reads the tree mid-flush: the first version of
// this check sat beside the snapshots, found nothing, and reported zero pages against five docs.
// A gate that races the thing it audits is not a gate. So the writing lives there and the
// looking lives here, and this file never writes.

import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { DOCS } from "./registry";
import { manualPath } from "./toMdx";

/** Repo-relative, from `frontend/src/docs/`. */
const MANUAL_ROOT = "../../../manual";

/** Every `.mdx` sitting in a directory the docs generate into. */
function generatedPages(): string[] {
  const groups = new Set(DOCS.map((doc) => manualPath(doc).split("/")[0] as string));
  const found: string[] = [];
  for (const group of groups) {
    const dir = join(__dirname, MANUAL_ROOT, group);
    for (const name of readdirSync(dir)) {
      if (name.endsWith(".mdx") && statSync(join(dir, name)).isFile()) {
        found.push(`${group}/${name}`);
      }
    }
  }
  return found;
}

describe("the manual's generated pages", () => {
  // Rule 64: retiring a doc has to retire its page too. The generator only ever visits docs that
  // still exist, so a renamed or deleted doc leaves its old page behind and the site goes on
  // serving it, with nothing else looking.
  it("leaves no page behind for a doc that no longer exists", () => {
    const expected = new Set(DOCS.map((doc) => manualPath(doc)));
    expect(generatedPages().filter((p) => !expected.has(p))).toEqual([]);
  });

  // Rule 145: the check above passes vacuously on an empty walk, which is the one way it can be
  // wrong and silent. Pin the count the walk collects: if you added a doc, this number moves with
  // it; if you did not, a page dropped out of the walk.
  it("finds exactly one page per doc", () => {
    expect(generatedPages()).toHaveLength(DOCS.length);
    expect(DOCS).toHaveLength(5);
  });

  // Every page the app can deep-link to has to exist on the site under the same anchor, or a
  // "read more" that works in the app dead-ends on the web.
  it("files each doc under its group's directory", () => {
    for (const doc of DOCS) {
      expect(manualPath(doc)).toMatch(/^[a-z-]+\/[a-z-]+\.mdx$/);
    }
  });
});
