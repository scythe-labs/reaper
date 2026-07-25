// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Reaper's app icon: the hooded figure dissolving into blocks, in a fixed ink shell. The shell
// and the figure are fixed; the EYES follow the operator's accent, so the browser-tab favicon
// tracks the same --accent the rest of the UI derives from.
//
// This module is the single source of that drawing for every raster medium -- the runtime
// favicon data URI, and the committed favicon.svg / apple-touch / manifest PNGs. favicon.svg is
// `appIconSvg(DEFAULT_ACCENT)`; the PNGs are rasterized from the two variants at the default
// accent by `frontend/scripts/gen-icons.mjs` (`npm run icons`), which also writes the
// icons.generated.json manifest. appIcon.test.ts is the enforcement: it fails if favicon.svg
// drifts from `appIconSvg`, and -- via that manifest's source hashes -- if the PNGs were built
// from a drawing that has since changed. The in-app badge draws the same thing in JSX
// (BrandBadge.tsx), following --accent live; keep the two in step. Geometry comes from
// ./dissolve.
//
// The eyes are the accent VERBATIM, with no contrast correction against the shell. An operator
// who picks a near-black accent gets near-invisible eyes; that is their color, shown honestly,
// and lifting it would put a hue on the icon they never chose. The old platter icon behaved the
// same way.

import {
  DISSOLVE_BLOCKS_LOWER,
  DISSOLVE_BLOCKS_UPPER,
  DISSOLVE_BONE,
  DISSOLVE_CUT,
  DISSOLVE_EYE_LEFT_D,
  DISSOLVE_EYE_RIGHT_D,
  DISSOLVE_FACE_D,
  DISSOLVE_HOOD_D,
  DISSOLVE_INK,
  DISSOLVE_VIEWBOX,
  dissolveBlockRects,
} from "./dissolve";

export interface AppIconOptions {
  /** Corner radius on the 64 grid. 14 is the rounded badge (browser tab, in-app); 0 is a
   *  full-bleed square for the iOS touch icon and maskable manifest icons, which apply their
   *  own mask over the square. */
  radius?: number;
}

/** The default corner radius, as a share of the grid: the mark's own drawing. */
export const APP_ICON_RADIUS = 14;

/** The app icon as a standalone SVG string for a given accent. Used for the runtime favicon
 *  data URI and to generate the committed icon files.
 *
 *  The clip path carries a fixed id, which is safe because every consumer renders this string
 *  as its own document (a `data:` URI or a `.svg` file). Anything inlining the mark into the
 *  page must use BrandBadge/BrandMark instead, which scope their ids with `useId`. */
export function appIconSvg(accent: string, opts: AppIconOptions = {}): string {
  const r = opts.radius ?? APP_ICON_RADIUS;
  const { x, y, width, height } = DISSOLVE_CUT;
  const shell = `<rect width="64" height="64" rx="${r}" fill="${DISSOLVE_INK}"/>`;
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${DISSOLVE_VIEWBOX}">` +
    `<clipPath id="r"><rect width="64" height="64" rx="${r}"/></clipPath>` +
    shell +
    `<g clip-path="url(#r)">` +
    `<path d="${DISSOLVE_HOOD_D}" fill="${DISSOLVE_BONE}"/>` +
    `<rect x="${x}" y="${y}" width="${width}" height="${height}" fill="${DISSOLVE_INK}"/>` +
    dissolveBlockRects(DISSOLVE_BLOCKS_UPPER, DISSOLVE_BONE) +
    `<path d="${DISSOLVE_FACE_D}" fill="${DISSOLVE_INK}"/>` +
    `<path d="${DISSOLVE_EYE_LEFT_D}" fill="${accent}"/>` +
    `<path d="${DISSOLVE_EYE_RIGHT_D}" fill="${accent}"/>` +
    dissolveBlockRects(DISSOLVE_BLOCKS_LOWER, DISSOLVE_BONE) +
    `</g>` +
    `</svg>`
  );
}

/** The app icon as a `data:` URI suitable for a `<link rel="icon">` href. */
export function appIconDataUri(accent: string): string {
  return "data:image/svg+xml," + encodeURIComponent(appIconSvg(accent));
}
