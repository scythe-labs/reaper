// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The five section icons the masthead nav wears when it collapses to the phone's bottom bar.
// Separate from queueIcons.tsx on purpose: that file is the review queue's own furniture, and
// these belong to the app shell.
//
// Drawn to the same standard as queueIcons: a 16px grid, `currentColor` strokes at 1.3, nothing
// filled except where a shape needs mass. None of them carries a width or height -- `.view-ico`
// in index.css sizes all five from one declaration, so the bar's icon size is changed in one
// place (rule 67).
//
// All are `aria-hidden`: each sits inside a button whose accessible name is the section label,
// which the phone bar keeps in the tree via `.visually-hidden` rather than dropping it. A screen
// reader must not read the decoration on top of the name.
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
  return (
    <svg className="view-ico" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <circle cx="8" cy="8" r="2.4" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M8 1.4V3M8 13v1.6M14.6 8H13M3 8H1.4M12.66 3.34 11.53 4.47M4.47 11.53 3.34 12.66M12.66 12.66 11.53 11.53M4.47 4.47 3.34 3.34"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}
