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
// tracking now come from `test/appLockup`, which reads them out of the app.

import { describe, expect, it } from "vitest";
import { appIconSvg } from "./appIcon";
import { DEFAULT_ACCENT } from "../accent";
import { BRAND_WEIGHT, BRAND_WORD, signInCardSpellsTheWord, trackingAt } from "../test/appLockup";

/** Repo-relative, from `frontend/src/brand/`. */
const MEDIA = "../../../docs/media";

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
      ` role="img" aria-label="${BRAND_WORD}">`,
    `<title>${BRAND_WORD}</title>`,
    mark,
    `<text x="100" y="63" font-family="${FONT}" font-size="${SIZE}" font-weight="${BRAND_WEIGHT}"` +
      ` letter-spacing="${trackingAt(SIZE)}" fill="${INK[theme]}">` +
      `${BRAND_WORD}</text>`,
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
    expect(
      signInCardSpellsTheWord(),
      `the sign-in card no longer introduces the app as "${BRAND_WORD}"; this banner reproduces ` +
        `that card, so the two have to agree`,
    ).toBe(true);
  });

  it("keeps the mark identical to the one the app renders", () => {
    // The banner must embed the real mark, not a copy of its paths. If someone inlines the
    // path data here, this fails: the app's own string would no longer be a substring.
    const appMark = appIconSvg(DEFAULT_ACCENT);
    const inner = appMark.slice(appMark.indexOf(">") + 1);
    expect(banner("dark")).toContain(inner.replace("</svg>", ""));
  });
});
