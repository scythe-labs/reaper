// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one scythe: a long crescent blade over a straight snath sweeping down-left. This is the review
// queue's REAP glyph, not the app's mark -- the brand mark is the hooded figure (../brand/
// BrandMark), and the two are deliberately different drawings, because here the scythe has to
// say which way a row went rather than which app you are in. The queue wears it small as
// `.scythe`. It rides `currentColor`, so the caller's CSS supplies the size and color. The
// snath thins to a hairline when shrunk, so small callers pass a heavier `strokeWidth` to hold
// the same visual weight as the label beside them (the reap glyph uses 5.5). The path geometry
// lives in ../brand/scythe.

import {
  SCYTHE_BLADE_D,
  SCYTHE_SNATH_D,
  SCYTHE_SNATH_WIDTH,
  SCYTHE_VIEWBOX,
} from "../brand/scythe";

export function ScytheGlyph({
  className,
  strokeWidth = SCYTHE_SNATH_WIDTH,
  width,
  height,
}: {
  className?: string;
  strokeWidth?: number;
  width?: number;
  height?: number;
}) {
  return (
    <svg
      className={className}
      viewBox={SCYTHE_VIEWBOX}
      width={width}
      height={height}
      fill="none"
      aria-hidden="true"
    >
      <path d={SCYTHE_BLADE_D} fill="currentColor" />
      <path
        d={SCYTHE_SNATH_D}
        stroke="currentColor"
        strokeWidth={strokeWidth}
        strokeLinecap="round"
      />
    </svg>
  );
}
