// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one scythe, as raw geometry. It is NOT the brand mark -- that is the hooded figure in
// ./dissolve, which the app icon, the favicon and the header wear. The scythe's remaining job
// is the review queue's "reaped" glyph, where it means the ACTION: a decided row rests as ∞ for
// spared and this for reaped (agent rules 46 and 49). Keeping the two drawings separate is
// deliberate -- a hooded figure beside an ∞ would say nothing about which way the row went.
//
// `ScytheGlyph` is the only component that draws it. On a 48x48 grid: the blade is a long
// crescent sweeping LEFT from the head, and the snath is the straight shaft running down-left
// from that same head, so the two leave the head in clearly different directions.
//
// The first version had the blade as a short filled wedge pointing the same way the snath
// ran, which at icon size collapsed into one solid diagonal mass -- it read as a pickaxe or
// an arrow, never a scythe. It survived in the queue only because context carried it (a red
// row, beside a button labeled Reap); put in the section nav with no label, it said the wrong
// thing outright. The blade is now long enough, and angled far enough off the snath, to read
// on its own at 13px.
export const SCYTHE_VIEWBOX = "0 0 48 48";
export const SCYTHE_BLADE_D = "M41 10C33 3 15 6 6 17C15 12 28 13 38 16Z";
export const SCYTHE_SNATH_D = "M38 12 21 42";
// The snath's natural weight, and `ScytheGlyph`'s default. Small callers pass a heavier one
// (rule 67 -- this is the single declaration, imported, not re-typed at the default).
export const SCYTHE_SNATH_WIDTH = 3.5;
