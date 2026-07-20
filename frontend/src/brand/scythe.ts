// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one scythe, as raw geometry. `ScytheGlyph` draws it as a component; the Deep app icon
// draws it into a badge (BrandBadge.tsx) and into the favicon (deepIcon.ts). All of them
// import these path strings, so the brand mark is defined in exactly one place -- see the
// "draw every scythe from one brand-mark SVG" rule. The blade is a hooked curve at top; the
// snath is the straight shaft sweeping down-left. On a 48x48 grid.
export const SCYTHE_VIEWBOX = "0 0 48 48";
export const SCYTHE_BLADE_D = "M31 9C17 9 9 17 9 29c8-8 16-12 26-10-1-5-2-8-4-10Z";
export const SCYTHE_SNATH_D = "M31 9 19 40";
export const SCYTHE_SNATH_WIDTH = 3.5;
