// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The stylesheet as the browser receives it, for the tests that walk it looking for a rule that
// loses on source order.
//
// `index.css` is now a barrel of `@import`s. A test that reads that one file therefore reads no
// rules at all -- and PASSES, having checked nothing, which is the failure this module exists to
// prevent. `viewport-units.test.ts` did exactly that for the length of one commit. So the tests
// read `CSS` from here: every imported file concatenated in the order the barrel lists them,
// which is the order Vite inlines them and thus the cascade itself.
//
// `siteOf` is the other half, and it is not a convenience. Two of those tests print where a
// losing declaration sits, and an offset into this concatenation is not a line in any file on
// disk: reporting `index.css:8213` would name a line that exists nowhere, and a confidently
// wrong citation is worse than none (rule 144). An offset resolves to the file it came from.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");
const BARREL = "index.css";

const barrelText = readFileSync(join(SRC, BARREL), "utf8");

/** Comments blanked, so an `@import` quoted inside prose is not mistaken for a real one. */
const barrelCode = barrelText.replace(/\/\*[\s\S]*?\*\//g, "");

/** The imported files, in load order. The order is the cascade; see the barrel's own comment. */
export const FILES: readonly string[] = [...barrelCode.matchAll(/@import\s+"\.\/([^"]+)"/g)]
  .map((m) => m[1])
  .filter((f): f is string => f !== undefined);

if (FILES.length === 0) throw new Error(`${BARREL} imports nothing; the walk would check nothing`);

// The barrel carries the design charter and the load order, and nothing else. A style rule
// written directly into it would be invisible to every test below, so refuse rather than
// under-check: a `{` surviving the comment strip is a rule (no at-rule here opens a block).
const stray = barrelCode.replace(/@import\s+"[^"]+"\s*;/g, "").trim();
if (stray.includes("{")) {
  throw new Error(
    `${BARREL} holds a style rule, not just @imports -- move it into styles/ so the ` +
      `stylesheet tests can see it. Stray text: ${stray.slice(0, 120)}`,
  );
}

const segments: { file: string; at: number }[] = [];
let text = "";
for (const file of FILES) {
  segments.push({ file, at: text.length });
  text += readFileSync(join(SRC, file), "utf8");
}

/** Every imported file, concatenated in load order: what the browser ends up with. */
export const CSS = text;

/** One rule block: its selector, its declarations as written, and where the body starts.
 *
 *  `at` is an offset into `CSS`, for `siteOf`. */
export interface Block {
  selector: string;
  body: string;
  at: number;
}

/** Every rule block in the stylesheet, in load order, comments stripped.
 *
 *  The match is innermost-first. A `@media` head never matches, since `[^{}]*` cannot span the
 *  nested brace, so a rule inside one is returned as itself. A flat at-rule like `@font-face`
 *  does match, and the `@` check drops it. Keyframe steps (`from`, `0%`) are returned as ordinary
 *  blocks.
 *
 *  `styles-scales.test.ts` and `styles-control-standard.test.ts` both walk blocks through here,
 *  so a parse fixed for one is fixed for the other. The scales file still reads `CSS` directly
 *  for its whole-text checks, which is not this walk.
 *
 *  A caller splitting `body` on `;` is safe only while no declaration carries one inside a value.
 *  There is no `url(` and no `data:` anywhere in `styles/`, so none does today. A data URI would
 *  break that split, and this is the line that says so. */
export function blocksOf(): Block[] {
  // Blanked rather than deleted, so every offset still resolves to its real line (`siteOf`).
  const code = CSS.replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "));
  const out: Block[] = [];
  for (const m of code.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = (m[1] ?? "").trim().replace(/\s+/g, " ");
    if (selector.startsWith("@")) continue;
    out.push({
      selector,
      body: m[2] ?? "",
      at: (m.index ?? 0) + (m[1] ?? "").length + 1,
    });
  }
  return out;
}

/** Where an offset in `CSS` actually lives, as `styles/21-queue-cards.css:431`. */
export function siteOf(offset: number): string {
  let seg = segments[0];
  // Unreachable while the barrel imports anything, which is checked above; stated so the
  // resolver cannot silently answer for a stylesheet it never read.
  if (seg === undefined) throw new Error("the stylesheet walk found no files to resolve against");
  for (const s of segments) {
    if (s.at > offset) break;
    seg = s;
  }
  return `${seg.file}:${CSS.slice(seg.at, offset).split("\n").length}`;
}
