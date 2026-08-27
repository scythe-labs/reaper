// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one scythe, as raw geometry. It is not the brand mark: that is the hooded figure in
// ./dissolve, which the app icon, the favicon, and the header wear. The scythe's remaining job
// is the review queue's "reaped" glyph, where it stands for the action: a decided row rests as
// ∞ for spared and this for reaped. Keeping the two drawings separate is deliberate. A hooded
// figure beside an ∞ would say nothing about which way the row went.
//
// `ScytheGlyph` is the only component that draws it. On a 48x48 grid: the blade is a long
// crescent sweeping left from the head, and the snath is the straight shaft running down-left
// from that same head, so the two leave the head in clearly different directions.
//
// The blade has to read as a scythe on its own, with no context to help: a short wedge angled
// close to the snath collapses into one solid diagonal mass at icon size, and reads as a
// pickaxe or an arrow instead. The blade stays long, and angled far enough off the snath, to
// read at 13px alone.
export const SCYTHE_VIEWBOX = "0 0 48 48";
export const SCYTHE_BLADE_D = "M41 10C33 3 15 6 6 17C15 12 28 13 38 16Z";
export const SCYTHE_SNATH_D = "M38 12 21 42";
// The snath's natural weight, and `ScytheGlyph`'s default. Small callers pass a heavier one,
// imported from this single declaration rather than re-typed at the default.
export const SCYTHE_SNATH_WIDTH = 3.5;
