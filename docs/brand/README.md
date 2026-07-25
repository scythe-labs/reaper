# Brand reference

Design explorations of Reaper's mark that are **kept for reference and are not shipped**.
Nothing here is imported by the app, served to a browser, or covered by the icon drift test.
If you are looking for the mark the app actually draws, it is not in this directory.

## Where the real mark lives

| | |
|---|---|
| Geometry | `frontend/src/brand/dissolve.ts` — the one definition, on a 64 grid |
| Icon / favicon | `frontend/src/brand/appIcon.ts` |
| Login badge | `frontend/src/brand/BrandBadge.tsx` |
| Header / setup | `frontend/src/brand/BrandMark.tsx` — the shell-less cowl |
| Committed assets | `frontend/public/`, written by `npm run icons` |

Never hand-edit a file in `frontend/public/`: change the geometry, then run `npm run icons`.
`appIcon.test.ts` fails by asset name if you skip it.

## dissolve-glow-soft.svg

The mark with lit eyes: a doubled Gaussian bloom on the eye shapes, plus a wider, low-opacity
halo behind them that spills onto the inside of the hood. It reads well large and still holds
at 32px. A stronger version of the same treatment was drawn alongside it — bloom
`stdDeviation` 1.6, halo `stdDeviation` 7 at opacity 0.5, against this file's 1.0 / 5 / 0.32 —
which was the louder of the two.

It was set aside, not rejected on quality. Two things stand between it and shipping, and both
are real work rather than a copy-paste:

- **Its eyes are fixed Plex gold (`#E5A00D`), not the operator's accent.** The shipped mark
  puts `--accent` on the eyes, which is the whole point of the current drawing. This file is
  not a drop-in.
- **The glow does not survive every surface the mark has to appear on.** The header wears the
  cowl with no shell (`BrandMark`), so there is no dark ground for a bloom to sit on, and the
  runtime favicon is built as an SVG string and encoded into a `data:` URI, so the filters
  would have to be threaded through `appIconSvg` as well. A mark that glows in one place and
  not another is two marks.

Picking this up later means solving both, not just swapping a file.
