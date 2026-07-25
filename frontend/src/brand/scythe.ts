// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one scythe, as raw geometry. It is NOT the brand mark -- that is the hooded figure in
// ./dissolve, which the app icon, the favicon and the header wear. The scythe's remaining job
// is the review queue's "reaped" glyph, where it means the ACTION: a decided row rests as ∞ for
// spared and this for reaped (agent rules 46 and 49). Keeping the two drawings separate is
// deliberate -- a hooded figure beside an ∞ would say nothing about which way the row went.
//
// `ScytheGlyph` is the only component that draws it. The blade is a hooked curve at top; the
// snath is the straight shaft sweeping down-left. On a 48x48 grid.
export const SCYTHE_VIEWBOX = "0 0 48 48";
export const SCYTHE_BLADE_D = "M31 9C17 9 9 17 9 29c8-8 16-12 26-10-1-5-2-8-4-10Z";
export const SCYTHE_SNATH_D = "M31 9 19 40";
export const SCYTHE_SNATH_WIDTH = 3.5;
