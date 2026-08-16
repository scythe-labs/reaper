// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The review queue's inline icons and glyphs, in one file.
//
// Twelve of these sat between the pieces of logic they decorate in ReviewQueue.tsx, so a
// reviewer of a two-line rule change scrolled past a few hundred lines of path data to find it
// (R-1). None of them takes anything but a prop or two; none of them belongs to the queue in
// particular. Every one is exported so the file that draws with it can import it by name.
//
// All of them are `aria-hidden`: each sits beside a label, or inside a control that carries its
// own accessible name, so a screen reader must not read the decoration twice.

import { ScytheGlyph } from "./ScytheGlyph";

export function LayersIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path
        d="M8 2l6 3-6 3-6-3 6-3z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
      <path
        d="M2 8l6 3 6-3M2 11l6 3 6-3"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function FunnelIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path
        d="M2 3h12l-4.5 5.5V13L6.5 11V8.5L2 3z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function SortIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path
        d="M3 4h10M3 8h6M3 12h3"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function GenreIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path
        d="M8 2.2l1.5 4.3L13.8 8l-4.3 1.5L8 13.8 6.5 9.5 2.2 8l4.3-1.5z"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function OverrideIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <rect x="2" y="5" width="12" height="6" rx="3" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="11" cy="8" r="1.8" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

/** The Plex library (section) glyph -- a small shelf of spines, distinct from the media-type
 *  layers icon so a library never reads as a media type. Shared by the library filter and the
 *  library chip. */
export function LibraryIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <rect
        x="2.5"
        y="3"
        width="2.4"
        height="10"
        rx="0.6"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <rect
        x="5.8"
        y="3"
        width="2.4"
        height="10"
        rx="0.6"
        stroke="currentColor"
        strokeWidth="1.2"
      />
      <path
        d="M9.6 4l2.4.6-1.9 8.2-2.4-.6"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Two spines and a leaning third, for a Plex collection -- distinct from the library shelf's
 *  own spines-and-book glyph (the two sit side by side on a card's facts line, rule 21's "never
 *  mix two meanings on one glyph" read straight through to icons). Sized down (12px) to sit
 *  inside the collection chip, the chip family's own smaller cousin of the 14px set above. */
export function CollectionIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" fill="none" aria-hidden="true">
      <rect x="2" y="3" width="3.4" height="10" rx="1" stroke="currentColor" strokeWidth="1.4" />
      <rect x="6.6" y="3" width="3.4" height="10" rx="1" stroke="currentColor" strokeWidth="1.4" />
      <path d="M11.6 4.2l2.3 9.1" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

export function PlusIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

export function CaretIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="11"
      height="11"
      fill="none"
      aria-hidden="true"
      className="fchip-caret"
    >
      <path
        d="M4 6l4 4 4-4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <path
        d="M3 8.5l3 3 7-7"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** The reap glyph: the scythe (see ScytheGlyph) as a reap ACTION. It is not the app's mark --
 *  that is the hooded figure in brand/dissolve -- and it should not follow the brand: what it
 *  has to say is which way a row went, paired against ∞ for spared. A heavier snath (5.5 vs the
 *  glyph's default 3.5) holds the shape's weight at button size, where the stroke would
 *  otherwise thin to a hairline. Only reap actions wear it -- close buttons keep ✕. */
export function ScytheIcon() {
  return <ScytheGlyph className="scythe" width={13} height={13} strokeWidth={5.5} />;
}

/** A small clock, the dormancy pill's shape reused -- here in the spare's green to mean "kept,
 *  for now". It marks a TIMED spare, where ∞ marks a forever one. */
export function ClockGlyph({
  dashed = false,
  size = 13,
}: { dashed?: boolean; size?: number } = {}) {
  return (
    <svg viewBox="0 0 16 16" width={size} height={size} fill="none" aria-hidden="true">
      {/* `dashed` marks a spare whose clock has PASSED: the same dial, drawn the way this app
          draws every decision whose effect is pending a scan (the dashed .status-reap-held /
          .score-refused family). The hands stay solid so it still reads as a clock. */}
      <circle
        cx="8"
        cy="8"
        r="6.2"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeDasharray={dashed ? "2.2 1.8" : undefined}
      />
      <path d="M8 4.6V8l2.4 1.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

/** The Spare button's leading glyph: ∞ when a plain press keeps forever, the clock when it
 *  keeps for a set time -- so the button says what it will do before you open the menu. */
export function SpareGlyph({ days }: { days: number }) {
  return days > 0 ? (
    <ClockGlyph />
  ) : (
    <span className="infinity" aria-hidden="true">
      ∞
    </span>
  );
}

export function CaretDownGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="11" height="11" fill="none" aria-hidden="true">
      <path
        d="M4 6l4 4 4-4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** The mark that says a link leaves Reaper, worn by the why panel's title and the Scales
 *  panel's heading. Decoration: the destination is already in the link's words, so it is
 *  `aria-hidden` like everything else here (rule 21's middot clause, same reasoning). The two
 *  sites style it differently, which is the only thing `className` carries. */
export function ExternalMark({ className }: { className: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 16 16"
      width="13"
      height="13"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M6 3h7v7M13 3L4 12"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function PenGlyph() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
      <path
        d="M11 2.5l2.5 2.5L6 12.5 3 13l.5-3z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** The selection tick a card wears in Select mode: an empty ring until picked, a filled check
 *  once it is. Replaces the raw checkbox -- it reads as part of the card. */
export function SelectTick({ selected }: { selected: boolean }) {
  return (
    <span className={`select-tick ${selected ? "on" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 16 16" width="11" height="11">
        <path
          d="M3.5 8.5l3 3 6-7"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.3"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}

export function CheckSquareIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <rect x="2" y="2" width="12" height="12" rx="3" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M5 8.2l2 2 4-4.4"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
