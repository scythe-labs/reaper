// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The barrel and the walk agree with the disk.
//
// Two ways the split stylesheet can go quietly wrong, and neither shows up in the app:
//
//   1. A file in `styles/` that nothing imports. It is dead CSS the browser never loads and no
//      test ever reads, and it looks exactly like working code in the editor.
//   2. `siteOf` naming the wrong place. Three tests print it in their failure messages, so a
//      drifting resolver sends the next reader to a line that has nothing to do with the
//      problem -- and it does that only when a test is already failing, which is the worst
//      moment to be lied to (rule 144). So it is checked against the files themselves rather
//      than trusted.

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { CSS, FILES, siteOf } from "./test/stylesheet";

const SRC = dirname(fileURLToPath(import.meta.url));

/** The whole line `offset` falls on. */
function lineAt(text: string, offset: number): string {
  const start = text.lastIndexOf("\n", Math.max(0, offset - 1)) + 1;
  const end = text.indexOf("\n", offset);
  return text.slice(start, end === -1 ? undefined : end);
}

describe("the styles barrel", () => {
  it("imports every file on disk, exactly once", () => {
    const onDisk = readdirSync(join(SRC, "styles"))
      .filter((f) => f.endsWith(".css"))
      .map((f) => `styles/${f}`)
      .sort();
    expect([...FILES].sort()).toEqual(onDisk);
    expect(new Set(FILES).size).toBe(FILES.length);
  });

  it("loads them in the order their numeric prefixes declare", () => {
    // The prefixes exist so a directory listing cannot imply the order is negotiable. If the
    // barrel ever needs to depart from them, renumber the files rather than silently disagree.
    expect([...FILES]).toEqual([...FILES].sort());
  });
});

describe("siteOf", () => {
  it("names the file and line an offset really came from", () => {
    const disk = new Map(
      FILES.map((f) => [f, readFileSync(join(SRC, f), "utf8").split("\n")] as const),
    );

    // Sampled across the whole concatenation, so every file is probed many times over.
    const wrong: string[] = [];
    for (let offset = 0; offset < CSS.length; offset += 419) {
      // Split on the LAST colon: the file name is the part before it, the line the part after.
      const site = siteOf(offset);
      const colon = site.lastIndexOf(":");
      const actual = disk.get(site.slice(0, colon))?.[Number(site.slice(colon + 1)) - 1];
      if (actual !== lineAt(CSS, offset)) {
        wrong.push(
          `offset ${offset} -> ${siteOf(offset)}: got ${actual}, want ${lineAt(CSS, offset)}`,
        );
      }
    }
    expect(wrong).toEqual([]);
  });

  it("puts the first byte of the stylesheet on line 1 of the first file", () => {
    expect(siteOf(0)).toBe(`${FILES[0]}:1`);
  });
});
