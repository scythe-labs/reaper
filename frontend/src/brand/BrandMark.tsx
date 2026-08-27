// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The brand mark worn flat, with no shell: the header and the setup wizard. The figure rides
// `currentColor` so the caller's CSS supplies it (`.brand-mark` sets --text), the cowl opening
// is left transparent so the page shows through it, and the eyes keep --accent. That is why
// this exists alongside BrandBadge: dropping the full ink tile into the masthead reads as a
// sticker in light mode and disappears into the page in dark mode, while the badge form is
// right on the login screen, where the app is introducing itself and has room.
//
// It draws the finished shape (./dissolve.generated), not the recipe. Composing the recipe
// here would mean cutting the ink parts out of a luminance mask, and a mask is rasterized into
// an offscreen buffer sized to the element's layout box. Pinch-zoom would scale that bitmap, so
// the figure would go soft while the eyes, the only part outside the mask, stayed sharp. At
// `.brand-mark`'s 34px that buffer is about a hundred device pixels, which a zoom has nothing
// to work with. A plain path carries no buffer and stays vector at any scale.
//
// The figure is one path for a second reason, found the same way: two shapes that share an
// edge are antialiased apart and composited one over the other, so the join can show as a
// hairline at the zoom levels where it falls between device pixels, as it did on iOS.
//
// Edit ./dissolve.ts and run `npm run mark`. appIcon.test.ts fails if the two fall out of step.

import { DISSOLVE_EYE_LEFT_D, DISSOLVE_EYE_RIGHT_D, DISSOLVE_VIEWBOX } from "./dissolve";
import { DISSOLVE_FIGURE_D } from "./dissolve.generated";

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
      {/* One path, and no fill rule: the figure is a single contour plus blocks that only
          overlap it, which nonzero unions. Do not split this back into two. */}
      <path d={DISSOLVE_FIGURE_D} fill="currentColor" />
      <path d={DISSOLVE_EYE_LEFT_D} fill="var(--accent)" />
      <path d={DISSOLVE_EYE_RIGHT_D} fill="var(--accent)" />
    </svg>
  );
}
