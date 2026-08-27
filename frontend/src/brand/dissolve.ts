// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Reaper's brand mark, as raw geometry: a hooded figure whose head holds while the body breaks
// into blocks and falls away: a picture of files being removed. On a 64x64 grid.
//
// Everything that draws the mark imports these constants, so the drawing is defined in exactly
// one place: the app icon and favicon (./appIcon), the in-app badge (./BrandBadge), and the
// header/setup mark (./BrandMark). Do not re-type a path string anywhere else.
//
// The mark is two fixed colors plus one variable: the shell is ink, the figure is bone, and the
// eyes take the operator's accent, which is what ties the icon to the color chosen in Settings.
// The scythe (./scythe) is the review queue's "reaped" glyph, not the brand mark: it stands for
// the action, not the app.
//
// Paint order matters and is not obvious. The hood is drawn whole, then an ink band erases the
// body below the shoulders, then the upper blocks land on that band, then the face cavity is
// cut, then the eyes, then the lower blocks. Drawing the blocks in one pass instead of two puts
// the upper ones under the face cavity, which swallows them.

export const DISSOLVE_VIEWBOX = "0 0 64 64";

/** The fixed shell color. Dark in both themes: the mark carries its own ground. */
export const DISSOLVE_INK = "#14161C";
/** The fixed figure color. It matches `--text`'s dark value rather than being a cream of its
 *  own: a dedicated cream at #EDE7DA (hue 41 degrees) read visibly yellow beside the app's
 *  other near-whites, and added a fourth near-white to the three already in use: --text,
 *  --surface, and white. */
export const DISSOLVE_BONE = "#edeef1";

/** The cowl: head and shoulders in one silhouette. */
export const DISSOLVE_HOOD_D =
  "M32 6c11 0 15 13 14 26 8 4 13 16 13 32H5c0-16 5-28 13-32C17 19 21 6 32 6Z";
/** The cavity where a face would be, cut out of the hood. */
export const DISSOLVE_FACE_D =
  "M32 14c8 0 11 9 10 19-1 9-4 14-4 31H26c0-17-3-22-4-31-1-10 2-19 10-19Z";
/** The two eyes, and the only part of the mark that follows --accent. */
export const DISSOLVE_EYE_LEFT_D = "M23.5 27.5 30 31.5v6.5l-6.5-4Z";
export const DISSOLVE_EYE_RIGHT_D = "M40.5 27.5 34 31.5v6.5l6.5-4Z";

/** The band that cuts the body off below the shoulders, leaving the blocks to carry it down. */
export const DISSOLVE_CUT = { x: 0, y: 40, width: 64, height: 24 } as const;

/** A falling block: `[x, y, size]` on the 64 grid. */
export type DissolveBlock = readonly [number, number, number];

/** Blocks painted over the cut band, before the face cavity, so the cavity cuts them. What
 *  each one contributes is its part outside the cavity. Two consequences worth knowing before
 *  moving one: a block that sits wholly inside the cavity draws nothing at all, and the left
 *  column's right edge is the cavity's edge rather than any block's. */
export const DISSOLVE_BLOCKS_UPPER: readonly DissolveBlock[] = [
  [19, 40, 7],
  [25, 47, 7],
  // Starts one unit above where the block ends above it (y=46, not y=47), so the two overlap
  // instead of just touching: sharing an edge exactly leaves a one-unit hole across the left
  // column. It is a unit bigger too, so the column still ends where it did. The extra width
  // falls inside the face cavity and draws nothing. (Coordinates stay out of this prose
  // deliberately: a bracketed triple in a comment reads as a block to anything scanning this
  // file.)
  [19, 46, 8],
  [45, 40, 6],
  // Positioned to overlap both the block above it and the one beside it by a unit each,
  // rather than clearing them by a unit and touching nothing.
  [40, 45, 7],
];

/** Blocks painted last, scattering toward the bottom edge. Some run past the grid on purpose:
 *  the mark is always clipped to its square, so they read as decay leaving the frame. */
export const DISSOLVE_BLOCKS_LOWER: readonly DissolveBlock[] = [
  [33, 55, 6],
  [21, 56, 5],
  [46, 53, 5],
  [12, 49, 5],
  [40, 62, 4],
  [52, 46, 4],
  [27, 63, 4],
  [53, 58, 3],
];
