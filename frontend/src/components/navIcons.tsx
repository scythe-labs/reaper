// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The five section icons the masthead nav wears when it collapses to the phone's bottom bar.
// Separate from queueIcons.tsx on purpose: that file is the review queue's own furniture, and
// these belong to the app shell.
//
// Drawn to the same standard as queueIcons: a 16px grid, `currentColor` strokes at 1.3, nothing
// filled except where a shape needs mass. None of them carries a width or height -- `.view-ico`
// in styles/02-masthead.css sizes all five from one declaration, so the bar's icon size is changed in one
// place (rule 67).
//
// All are `aria-hidden`: each sits inside a button whose accessible name is the section label,
// which the phone bar keeps in the tree by CLIPPING rather than dropping -- `.view-label` in
// styles/10-layout.css's 900px block, never `display: none`. A screen reader must not read the decoration on
// top of the name.
//
// Reap draws nothing of its own. It wears the app's ONE scythe (ScytheGlyph -> brand/scythe),
// because a second scythe drawn to a different curve is exactly the parallel implementation
// rule 18 exists to prevent.

import { ScytheGlyph } from "./ScytheGlyph";

export function ReviewIcon() {
  // The gaze. The brand mark's one accent detail is its lit eyes, so the section where Reaper
  // looks at your library is the one place that detail is worth echoing.
  return (
    <svg className="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M1.3 8s2.5-4.3 6.7-4.3S14.7 8 14.7 8s-2.5 4.3-6.7 4.3S1.3 8 1.3 8z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <circle cx="8" cy="8" r="1.95" fill="currentColor" />
    </svg>
  );
}

export function PolicyIcon() {
  // The written law it judges by: a sheet with rules on it.
  return (
    <svg className="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M4.6 2h6.8A1.6 1.6 0 0 1 13 3.6v8.8a1.6 1.6 0 0 1-1.6 1.6H4.6A1.6 1.6 0 0 1 3 12.4V3.6A1.6 1.6 0 0 1 4.6 2z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path
        d="M5.7 5.6h4.6M5.7 8h4.6M5.7 10.4h2.9"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function ScalesIcon() {
  // A balance, for the section already named for one.
  return (
    <svg className="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M8 2.4v11M5.5 13.4h5M2.6 5.1h10.8"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
      <path
        d="M.9 8.7h3.4L2.6 5.1zM11.7 8.7h3.4L13.4 5.1z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <circle cx="8" cy="5.1" r="1" fill="currentColor" />
    </svg>
  );
}

export function ReapIcon() {
  // The snath thins to a hairline at this size, so it takes the heavier stroke small callers
  // pass (ScytheGlyph's own comment names 5.5 for the queue's inline mark; the bar sits a
  // little larger, so 4.5 holds the same weight as the shapes beside it).
  return <ScytheGlyph className="view-ico" strokeWidth={4.5} />;
}

export function SettingsIcon() {
  // A gear. Nothing in the reaper's world means "configure this install", and the themed
  // candidates each said something false -- a key reads as access, an hourglass as time, which
  // grace already owns. Recognition wins here.
  //
  // The teeth are a single closed outline (flanks straight, the gaps between them arcs of the
  // root circle), not spokes radiating off a ring: detached rays around a disc are a SUN, which
  // is what the first draft of this icon drew. Six teeth rather than eight because the gaps stay
  // countable at the 24px the bar renders it at, where eight silt up into a bumpy circle.
  return (
    <svg className="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        d="M6.61 3.72L6.8 1.82L9.2 1.82L9.39 3.72A4.5 4.5 0 0 1 11.01 4.66L12.75 3.87L13.96 5.95L12.4 7.06A4.5 4.5 0 0 1 12.4 8.94L13.96 10.05L12.75 12.13L11.01 11.34A4.5 4.5 0 0 1 9.39 12.28L9.2 14.18L6.8 14.18L6.61 12.28A4.5 4.5 0 0 1 4.99 11.34L3.25 12.13L2.04 10.05L3.6 8.94A4.5 4.5 0 0 1 3.6 7.06L2.04 5.95L3.25 3.87L4.99 4.66A4.5 4.5 0 0 1 6.61 3.72Z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <circle cx="8" cy="8" r="2.05" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}
