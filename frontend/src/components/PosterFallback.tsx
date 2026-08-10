// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The film-strip mark a title falls back to when it has no poster, or the image fails to load.
// Three surfaces draw it: the queue's poster tile, the Scales person panel, and the
// not-in-scan list, which has no poster to try in the first place.
//
// It has its own module because two of those surfaces are `ScalesPanel` and `UnmatchedList`,
// which render each other's rows: the panel embeds the list, and the list borrowed this one
// component back out of the panel, which was a whole import cycle for twelve lines of path
// data. The queue held a hand-copied second drawing of the same paths. A leaf both sides
// already sit above answers both.
//
// It rides `currentColor` and carries no class, so each caller's own wrapper supplies the
// color and the box; `size` is the drawn square in px, which is the one thing the three
// callers genuinely disagree about.

export function PosterFallback({ size = 18 }: { size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none">
      <path d="M4 5h16v14H4z" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M8 5v14M16 5v14M4 9h4M16 9h4M4 15h4M16 15h4"
        stroke="currentColor"
        strokeWidth="1.2"
      />
    </svg>
  );
}
