// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one scythe. Reaper's brand mark and its reap glyph are the same drawing at two
// sizes, so they are the same SVG: a curved blade over a straight snath sweeping
// down-left. The header and setup wear it via `.brand-mark`; the review queue's reap
// actions wear it small as `.scythe`. Everything rides `currentColor`, so each caller's CSS
// supplies the size and color. The snath thins to a hairline when shrunk, so small callers
// pass a heavier `strokeWidth` to hold the same visual weight as the label beside them (the
// header keeps the default 3.5; the reap glyph uses 5.5). The path geometry is the shared
// brand mark in ../brand/scythe, so the Deep app icon and favicon draw the same scythe.

import { SCYTHE_BLADE_D, SCYTHE_SNATH_D, SCYTHE_VIEWBOX } from "../brand/scythe";

export function ScytheGlyph({
  className,
  strokeWidth = 3.5,
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
      <path d={SCYTHE_SNATH_D} stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
    </svg>
  );
}
