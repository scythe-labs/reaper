// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Reaper's app icon: the scythe over a lit hard-drive platter in a dark shell ("Deep"). The
// dark shell is fixed; the platter and its glow follow the operator's accent, so the
// browser-tab favicon tracks the same --accent the rest of the UI derives from. This module
// is the single source of that drawing for every raster medium -- the runtime favicon data
// URI, and the committed favicon.svg / apple-touch / manifest PNGs.
//
// The committed files are generated, by `scripts/gen-icons.mjs` (`npm run icons`): favicon.svg
// is `deepIconSvg(DEFAULT_ACCENT)` verbatim, and every PNG in `ICON_TARGETS` below is that SVG
// rasterized at the size and variant the table names. Change anything here or in ./scythe and
// run it again. deepIcon.test.ts is what makes forgetting impossible rather than merely
// discouraged: the generator stamps each PNG with the hash of the exact SVG text it rendered,
// and the test recomputes that hash from this module, so a stale raster fails the suite.
//
// The in-app badge draws the same thing in JSX (BrandBadge.tsx), following --accent live via
// color-mix; keep the two in step. Scythe geometry comes from ./scythe.

import { SCYTHE_BLADE_D, SCYTHE_SNATH_D, SCYTHE_SNATH_WIDTH } from "./scythe";

const SHELL_TOP = "#0f3040";
const SHELL_BOTTOM = "#061620";

type Rgb = [number, number, number];
const WHITE: Rgb = [255, 255, 255];
const BLACK: Rgb = [0, 0, 0];

function clampByte(v: number): number {
  return Math.max(0, Math.min(255, Math.round(v)));
}
function toHex([r, g, b]: Rgb): string {
  return "#" + [r, g, b].map((v) => clampByte(v).toString(16).padStart(2, "0")).join("");
}
function parse(hex: string): Rgb {
  const h = hex.replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}
function mix(hex: string, toward: Rgb, t: number): string {
  const [r, g, b] = parse(hex);
  return toHex([r + (toward[0] - r) * t, g + (toward[1] - g) * t, b + (toward[2] - b) * t]);
}

/** The platter's light/dark gradient stops and glow, derived from an accent so a custom
 *  accent re-tints the disk while the dark shell holds. Mirrors the color-mix in BrandBadge:
 *  light = accent + 10% white, dark = accent x 0.55 (the app's badge-gradient darkening). */
export function deepDiskColors(accent: string): { light: string; dark: string; glow: string } {
  return {
    light: mix(accent, WHITE, 0.1),
    dark: mix(accent, BLACK, 0.45),
    glow: accent,
  };
}

/** The rounded badge's corner radius on the 512 grid. Declared once: it is the default below,
 *  the value `ICON_TARGETS` names, and what the test looks for in the committed SVG. */
export const BADGE_RADIUS = 143;

export interface DeepIconOptions {
  /** Corner radius on the 512 grid. `BADGE_RADIUS` is the rounded badge (browser tab,
   *  in-app); 0 is a full-bleed square for the iOS touch icon and maskable manifest icons,
   *  which apply their own mask over the square. */
  radius?: number;
}

/** Every committed raster, and the variant each takes. iOS and Android mask their own icons,
 *  so those get the full-bleed square and let the platform round it; anything a browser shows
 *  as-is gets the rounded badge. This was prose in the header once, and the prose drifted --
 *  it had apple-touch rounded, which the committed file never was. Now `scripts/gen-icons.mjs`
 *  writes exactly this list and deepIcon.test.ts checks exactly this list, so a new size is
 *  declared here once and both follow. */
export const ICON_TARGETS: readonly { file: string; size: number; radius: number }[] = [
  { file: "favicon-32.png", size: 32, radius: BADGE_RADIUS },
  { file: "apple-touch-icon.png", size: 180, radius: 0 },
  { file: "icon-192.png", size: 192, radius: BADGE_RADIUS },
  { file: "icon-512.png", size: 512, radius: BADGE_RADIUS },
  { file: "icon-maskable-512.png", size: 512, radius: 0 },
];

/** The PNG `tEXt` keyword the generator stamps into every raster it writes, carrying the
 *  sha256 of the exact SVG text that was rasterized. That is the drift check: the test
 *  recomputes the hash from `deepIconSvg`, so a PNG rendered from an older drawing fails even
 *  though its size and its pixels are all perfectly valid. */
export const ICON_SOURCE_KEYWORD = "reaper-source";

/** The Deep icon as a standalone SVG string for a given accent. Used for the runtime favicon
 *  data URI and to generate the committed icon files. */
export function deepIconSvg(accent: string, opts: DeepIconOptions = {}): string {
  const r = opts.radius ?? BADGE_RADIUS;
  const { light, dark, glow } = deepDiskColors(accent);
  const shell =
    r > 0
      ? `<rect width="512" height="512" rx="${r}" fill="url(#s)"/>`
      : `<rect width="512" height="512" fill="url(#s)"/>`;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">` +
    `<defs>` +
    `<linearGradient id="s" x1="0.1" y1="0" x2="0.85" y2="1"><stop offset="0" stop-color="${SHELL_TOP}"/><stop offset="1" stop-color="${SHELL_BOTTOM}"/></linearGradient>` +
    `<radialGradient id="g" cx="0.5" cy="0.46" r="0.5"><stop offset="0" stop-color="${glow}" stop-opacity="0.55"/><stop offset="1" stop-color="${glow}" stop-opacity="0"/></radialGradient>` +
    `<linearGradient id="d" x1="0.15" y1="0" x2="0.85" y2="1"><stop offset="0" stop-color="${light}"/><stop offset="1" stop-color="${dark}"/></linearGradient>` +
    `</defs>` +
    shell +
    `<circle cx="256" cy="238" r="215" fill="url(#g)"/>` +
    `<circle cx="256" cy="256" r="176" fill="url(#d)"/>` +
    `<circle cx="256" cy="256" r="176" fill="none" stroke="#fff" stroke-opacity="0.25" stroke-width="3"/>` +
    `<g fill="none" stroke="#fff" stroke-opacity="0.28"><circle cx="256" cy="256" r="140" stroke-width="4"/><circle cx="256" cy="256" r="100" stroke-width="4"/></g>` +
    `<g transform="translate(256 258) scale(8.2) translate(-22 -24.5)">` +
    `<path d="${SCYTHE_BLADE_D}" fill="#fff"/>` +
    `<path d="${SCYTHE_SNATH_D}" fill="none" stroke="#fff" stroke-width="${SCYTHE_SNATH_WIDTH}" stroke-linecap="round"/>` +
    `</g>` +
    `</svg>`
  );
}

/** The Deep icon as a `data:` URI suitable for a `<link rel="icon">` href. */
export function deepIconDataUri(accent: string): string {
  return "data:image/svg+xml," + encodeURIComponent(deepIconSvg(accent));
}
