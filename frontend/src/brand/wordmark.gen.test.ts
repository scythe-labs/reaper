// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The README banner: the app's own mark beside the app's own word.
//
// Generated rather than drawn, so the banner cannot drift from the mark the app actually
// renders. `gen-icons.mjs` was the obvious home and is the wrong one: its ASSETS list is
// declared as "what ships [in the app]", and its test cross-checks every entry against
// `index.html` and `site.webmanifest`. A README image is referenced by neither, so it would
// have to be excused from the one check that makes that list trustworthy.
//
// Writing and checking are the same call here, the same way `manual.gen.test.ts` works:
// `npm run gen-wordmark` writes the files, and an ordinary run fails when they drift.
//
// Two variants, because GitHub renders a README in whichever theme the reader chose and an
// `<img>` cannot see it. The README picks between them with `<picture>` and
// `prefers-color-scheme`.
//
// Only the MARK was generated at first, and the word beside it was typed: the banner shipped
// reading "reaper" at semibold while every surface in the app reads "Reaper" at bold. The
// generated half is what made that survive review -- it is demonstrably the app's drawing, and
// it vouched for a word nothing had checked (rule 144). So the word, its weight and its
// tracking are read from the app below, and each reader throws naming the file it read.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import { appIconSvg } from "./appIcon";
import { DEFAULT_ACCENT } from "../accent";

/** Repo-relative, from `frontend/src/brand/`. */
const MEDIA = "../../../docs/media";

const repoDir = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

/** One value the app declares, or a failure naming where it went. A miss is the drift this
 *  file exists to catch, so it throws rather than falling back to a literal. */
function fromApp(file: string, pattern: RegExp, what: string): string {
  const hit = readFileSync(join(repoDir, file), "utf8").match(pattern);
  // On the group, not the match: a pattern edited into matching with nothing captured would
  // otherwise substitute "undefined" into the banner and snapshot it.
  if (hit?.[1] === undefined) {
    throw new Error(`${file} no longer declares ${what}, which the README banner reads`);
  }
  return hit[1];
}

/** The word itself, from the masthead's `h1` -- the app's only one, so it reads unambiguously
 *  (`Login.tsx` renders two, "Reaper" and "Recovery"). */
const WORD = fromApp(
  "frontend/src/App.tsx",
  /<h1 className="brand-word">([^<]+)<\/h1>/,
  "an h1.brand-word",
);

/** Bold, from the weight scale. `.brand-word` is `var(--weight-bold)` in `02-masthead.css`. */
const WEIGHT = fromApp(
  "frontend/src/styles/00-tokens.css",
  /--weight-bold:\s*(\d+)/,
  "--weight-bold",
);

/** Tracking, in em, from the AUTH card rather than the masthead. That card is the lockup this
 *  banner reproduces -- badge plus word, the app introducing itself with room to do it -- and
 *  it tracks tighter than the masthead's inline form (`07-auth.css`, `02-masthead.css`). */
const TRACKING_EM = fromApp(
  "frontend/src/styles/07-auth.css",
  /\.auth-card \.brand-word\s*\{[^}]*?letter-spacing:\s*(-?[\d.]+)em/,
  "a letter-spacing on .auth-card .brand-word",
);

/** The banner's own size, in the 360x96 box below. Everything else is the app's. */
const SIZE = 58;

/** The wordmark's ink in each theme. Deliberately NOT the accent: the accent is a fill, and
 *  at Reaper's sky blue it fails WCAG AA as text on white (2.03:1). The mark beside it carries
 *  the color, and the word stays legible. */
const INK = { light: "#16181d", dark: "#edeef1" } as const;

/** A system stack, because a README image renders with no webfont available and a `@font-face`
 *  would silently fall back to something unchosen. It opens with the app's own three
 *  (`01-base.css`) so the face a reader gets is the face the app gets, then names two real
 *  families for viewers that resolve neither generic. */
const FONT = "system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif";

function banner(theme: keyof typeof INK): string {
  // The mark is a whole SVG document; nest it and give it a box on the banner's grid.
  const mark = appIconSvg(DEFAULT_ACCENT).replace(
    "<svg ",
    '<svg x="4" y="8" width="80" height="80" ',
  );
  return [
    `<!-- GENERATED FILE. Do not edit.`,
    `     Source:     frontend/src/brand/appIcon.ts, via wordmark.gen.test.ts`,
    `     Regenerate: run "npm run gen-wordmark" from frontend/ -->`,
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 96" width="360" height="96"` +
      ` role="img" aria-label="${WORD}">`,
    `<title>${WORD}</title>`,
    mark,
    `<text x="100" y="63" font-family="${FONT}" font-size="${SIZE}" font-weight="${WEIGHT}"` +
      ` letter-spacing="${+(Number(TRACKING_EM) * SIZE).toFixed(2)}" fill="${INK[theme]}">` +
      `${WORD}</text>`,
    `</svg>`,
    ``,
  ].join("\n");
}

describe("the README banner", () => {
  it.each(["light", "dark"] as const)(
    "writes the %s variant, and fails when it drifts",
    async (theme) => {
      await expect(banner(theme)).toMatchFileSnapshot(`${MEDIA}/wordmark-${theme}.svg`);
    },
  );

  // An XML comment may not contain a double hyphen, and the regenerate line above wanted to
  // read `npm --prefix frontend run ...`. Both files shipped malformed and rendered as a broken
  // image in every viewer, while the snapshot test stayed perfectly green: it compares strings
  // and has no opinion about whether the string is valid XML.
  it.each(["light", "dark"] as const)("emits well-formed XML for %s", (theme) => {
    const doc = new DOMParser().parseFromString(banner(theme), "image/svg+xml");
    expect(doc.querySelector("parsererror")?.textContent ?? null).toBeNull();
    expect(doc.documentElement.tagName).toBe("svg");
  });

  it("spells the word the way every surface of the app spells it", () => {
    // WORD comes from the masthead, so a banner built from it agrees with the masthead by
    // construction and can say nothing about the OTHER surface rendering the same lockup. The
    // sign-in card is the one this banner reproduces -- badge, word, tagline -- and it is read
    // here by name, because a derived value vouching for siblings nobody checked is exactly how
    // the lowercase banner survived (rule 144, rule 72).
    const login = readFileSync(join(repoDir, "frontend/src/components/Login.tsx"), "utf8");
    expect(
      login,
      `the sign-in card no longer introduces the app as "${WORD}"; the README banner follows it`,
    ).toContain(`<h1 className="brand-word">${WORD}</h1>`);
  });

  it("keeps the mark identical to the one the app renders", () => {
    // The banner must embed the real mark, not a copy of its paths. If someone inlines the
    // path data here, this fails: the app's own string would no longer be a substring.
    const appMark = appIconSvg(DEFAULT_ACCENT);
    const inner = appMark.slice(appMark.indexOf(">") + 1);
    expect(banner("dark")).toContain(inner.replace("</svg>", ""));
  });
});
