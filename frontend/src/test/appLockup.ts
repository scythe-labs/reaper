// SPDX-License-Identifier: AGPL-3.0-or-later
//
// How the app writes its own name: the word, the weight, and the tracking of the badge-and-word
// lockup. Read from the app rather than retyped, for the two assets that draw that lockup
// OUTSIDE the app and cannot ask the DOM for it -- the README banner and the social preview
// card.
//
// Both had drifted, and in different directions: the banner read "reaper" at semibold, the card
// read "REAPER" tracked out to 8.5, and the app read "Reaper" at bold. Three surfaces, three
// spellings, each written by someone reading a different one (rule 144). Whichever of the three
// you looked at, the other two were wrong.
//
// This lives here, not in `brand/`, because it reads the filesystem: `brand/appIcon.ts` and
// `brand/socialCard.ts` are imported by the app and by a rasterizer with no `node:fs`. Only
// tests import this. `stylesheet.ts` is the precedent and supplies the CSS.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { CSS } from "./stylesheet";

/** Repo root, from `frontend/src/test/`. */
const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

/** The masthead. Its `h1.brand-word` is the app's only one, so it reads unambiguously --
 *  `Login.tsx` renders two, the app's name and "Recovery". */
const MASTHEAD = "frontend/src/App.tsx";

/** One capture, or a failure naming where it went. A miss is the drift this module exists to
 *  catch, so it throws rather than falling back to a literal that would then be unchecked. */
function capture(source: string, pattern: RegExp, what: string, where: string): string {
  const hit = source.match(pattern);
  // On the group, not the match: a pattern edited into matching with nothing captured would
  // otherwise hand back `undefined` and be rendered into an asset as the string "undefined".
  if (hit?.[1] === undefined) {
    throw new Error(`${where} no longer declares ${what}, which the brand assets read from it`);
  }
  return hit[1];
}

/** The name, as the masthead spells it. */
export const BRAND_WORD = capture(
  readFileSync(join(REPO, MASTHEAD), "utf8"),
  /<h1 className="brand-word">([^<]+)<\/h1>/,
  "an h1.brand-word",
  MASTHEAD,
);

/** Bold, from the weight scale `.brand-word` resolves through (`00-tokens.css`,
 *  `02-masthead.css`). */
export const BRAND_WEIGHT = Number(
  capture(CSS, /--weight-bold:\s*(\d+)/, "--weight-bold", "the stylesheet"),
);

/** Tracking in em, from the AUTH card rather than the masthead. That card is the lockup both
 *  assets reproduce -- mark, word, a line under it, the app introducing itself with room to do
 *  it -- and it tracks tighter than the masthead's inline form (`07-auth.css`). */
export const BRAND_TRACKING_EM = Number(
  capture(
    CSS,
    /\.auth-card \.brand-word\s*\{[^}]*?letter-spacing:\s*(-?[\d.]+)em/,
    "a letter-spacing on .auth-card .brand-word",
    "the stylesheet",
  ),
);

/** That tracking as an SVG `letter-spacing`, which is a length rather than a ratio, so it is
 *  resolved against the size the asset draws the word at. */
export function trackingAt(fontSize: number): number {
  return +(BRAND_TRACKING_EM * fontSize).toFixed(2);
}

/** The OTHER surface drawing this lockup inside the app. `BRAND_WORD` comes from the masthead,
 *  so anything built on it agrees with the masthead by construction and can say nothing about
 *  the sign-in card -- which is the surface both assets actually reproduce. A derived value
 *  vouching for a sibling nobody checked is how the lowercase banner survived review, so the
 *  sibling is read here by name (rule 144, rule 72). */
export function signInCardSpellsTheWord(): boolean {
  const login = readFileSync(join(REPO, "frontend/src/components/Login.tsx"), "utf8");
  return login.includes(`<h1 className="brand-word">${BRAND_WORD}</h1>`);
}
