// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What is on disk under `manual/`, audited against what the docs declare.
//
// This is deliberately NOT in `manual.gen.test.ts`. That file WRITES the pages under `-u`, and a
// directory walk sharing a run with the writer reads the tree mid-flush: the first version of
// this check sat beside the snapshots, found nothing, and reported zero pages against five docs.
// A gate that races the thing it audits is not a gate. So the writing lives there and the
// looking lives here, and this file never writes.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { describe, expect, it } from "vitest";
import { DOCS } from "./registry";
import { GENERATED_MARKER, manualPath } from "./toMdx";

/** Repo-relative, from `frontend/src/docs/`. */
const MANUAL_ROOT = join(__dirname, "../../../manual");

/** Every `.mdx` under the manual, hand-written and generated alike, as manual-relative paths
 *  with forward slashes so the comparison is the same on every platform. */
function allPages(): string[] {
  const out: string[] = [];
  const walk = (dir: string): void => {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full);
      else if (name.endsWith(".mdx")) out.push(relative(MANUAL_ROOT, full).split(sep).join("/"));
    }
  };
  walk(MANUAL_ROOT);
  return out;
}

/** The pages that carry the generator's banner. A hand-written page in the same directory is
 *  not one of these, which is the whole reason the marker exists rather than a path convention. */
function generatedPages(): string[] {
  return allPages().filter((p) =>
    readFileSync(join(MANUAL_ROOT, p), "utf8").includes(GENERATED_MARKER),
  );
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
  it("finds exactly one generated page per doc", () => {
    expect(generatedPages()).toHaveLength(DOCS.length);
    expect(DOCS).toHaveLength(5);
  });

  // Rule 147: the marker is a source-text matcher, so pin the population it scans as well as the
  // population it selects. These two numbers moving together is the normal case; only one moving
  // means a hand-written page grew a banner, or a generated one lost it.
  it("counts the hand-written pages that sit beside them", () => {
    expect(allPages()).toHaveLength(13);
    expect(allPages().length - generatedPages().length).toBe(8);
  });

  it("files each generated doc under its group's directory", () => {
    for (const doc of DOCS) {
      expect(manualPath(doc)).toMatch(/^[a-z-]+\/[a-z-]+\.mdx$/);
    }
  });
});
