// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one walk over the SPA's shipped source, for every test that scans the tree. A scan that
// grows its own copy of the walk can silently diverge from the population its siblings cover.
// Paths are resolved from this file, so the walk stays inside whatever checkout the suite is
// running in.

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

/** frontend/src, in the checkout the suite runs in. */
export const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Every `.ts`/`.tsx` the SPA ships, tests excluded. Absolute paths, sorted. */
export function shippedSource(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "test") walk(full);
      } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
        out.push(full);
      }
    }
  };
  walk(SRC);
  return out.sort();
}

/** A shipped file's text, addressed src-relative the way failure messages print it. */
export function sourceText(srcRelative: string): string {
  return readFileSync(join(SRC, srcRelative), "utf8");
}

/** The src-relative spelling of a `shippedSource()` path, for failure messages. */
export function srcRelative(absolute: string): string {
  return relative(SRC, absolute);
}
