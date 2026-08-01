// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The brand mark worn flat, with no shell: the header and the setup wizard. The figure rides
// `currentColor` so the caller's CSS supplies it (`.brand-mark` sets --text), the cowl opening
// is left TRANSPARENT so the page shows through it, and the eyes keep --accent. That is the
// whole reason this exists alongside BrandBadge: dropping the full ink tile into the masthead
// reads as a sticker in light mode and disappears into the page in dark mode, while the badge
// form is right on the login screen, where the app is introducing itself and has room.
//
// It draws the FINISHED shape (./dissolve.generated), not the recipe. Composing the recipe here
// meant cutting the ink parts out of a luminance mask, and a mask is rasterized into an
// offscreen buffer sized to the element's layout box -- so pinch-zoom scaled a bitmap and the
// figure went soft while the eyes, the only part outside the mask, stayed sharp. At
// `.brand-mark`'s 34px that buffer is about a hundred device pixels, which a zoom has nothing
// to work with. Two plain paths carry no buffer and stay vector at any scale.
//
// Edit ./dissolve.ts and run `npm run mark`; appIcon.test.ts fails if the two fall out of step.

import { DISSOLVE_EYE_LEFT_D, DISSOLVE_EYE_RIGHT_D, DISSOLVE_VIEWBOX } from "./dissolve";
import { DISSOLVE_FIGURE_BLOCKS_D, DISSOLVE_FIGURE_HEAD_D } from "./dissolve.generated";

export function BrandMark({
  className,
  size,
  title,
}: {
  className?: string;
  size?: number;
  /** Omitted by default: in the masthead and the wizard the word "Reaper" sits right beside
   *  the mark, so announcing it again is noise. Pass a title only where it stands alone. */
  title?: string;
}) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox={DISSOLVE_VIEWBOX}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      {/* evenodd, because the cowl opening is a hole inside the head rather than a shape beside
          it; the default nonzero would fill it in. */}
      <path d={DISSOLVE_FIGURE_HEAD_D} fill="currentColor" fillRule="evenodd" />
      {/* And nonzero here, just as deliberately: the blocks overlap so no renderer can seam
          their joins, and evenodd would turn every overlap into a hole. */}
      <path d={DISSOLVE_FIGURE_BLOCKS_D} fill="currentColor" />
      <path d={DISSOLVE_EYE_LEFT_D} fill="var(--accent)" />
      <path d={DISSOLVE_EYE_RIGHT_D} fill="var(--accent)" />
    </svg>
  );
}
