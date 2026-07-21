// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Reaper's app icon: the scythe over a lit hard-drive platter in a dark shell ("Deep"). The
// dark shell is fixed; the platter and its glow follow the operator's accent, so the
// browser-tab favicon tracks the same --accent the rest of the UI derives from. This module
// is the single source of that drawing for every raster medium -- the runtime favicon data
// URI, and the committed favicon.svg / apple-touch / manifest PNGs. favicon.svg is
// `deepIconSvg(DEFAULT_ACCENT)`; the PNGs are rasterized from the two SVG variants at the
// default accent (the rounded rx=143 form for favicon-32 / apple-touch / icon-192 / icon-512,
// the full-bleed `{radius:0}` form for icon-maskable-512). deepIcon.test.ts is the
// enforcement: it fails if favicon.svg drifts from `deepIconSvg`, or if any PNG is missing or
// the wrong size, so a brand change can't ship a stale asset. The in-app badge draws the same
// thing in JSX (BrandBadge.tsx), following --accent live via color-mix; keep the two in step.
// Scythe geometry comes from ./scythe.

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

export interface DeepIconOptions {
  /** Corner radius on the 512 grid. 143 is the rounded badge (browser tab, in-app); 0 is a
   *  full-bleed square for the iOS touch icon and maskable manifest icons, which apply their
   *  own mask over the square. */
  radius?: number;
}

/** The Deep icon as a standalone SVG string for a given accent. Used for the runtime favicon
 *  data URI and to generate the committed icon files. */
export function deepIconSvg(accent: string, opts: DeepIconOptions = {}): string {
  const r = opts.radius ?? 143;
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
