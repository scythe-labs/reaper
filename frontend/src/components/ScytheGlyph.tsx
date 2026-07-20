// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one scythe. Reaper's brand mark and its reap glyph are the same drawing at two
// sizes, so they are the same SVG: a curved blade over a straight snath sweeping
// down-left. The header, login, and setup wear it large via `.brand-mark`; the review
// queue's reap actions wear it small as `.scythe`. Everything rides `currentColor`, so
// each caller's CSS supplies the size and color. The snath thins to a hairline when
// shrunk, so small callers pass a heavier `strokeWidth` to hold the same visual weight
// as the label beside them (the header keeps the default 3.5; the reap glyph uses 5.5).

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
      viewBox="0 0 48 48"
      width={width}
      height={height}
      fill="none"
      aria-hidden="true"
    >
      <path d="M31 9C17 9 9 17 9 29c8-8 16-12 26-10-1-5-2-8-4-10Z" fill="currentColor" />
      <path d="M31 9 19 40" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
    </svg>
  );
}
