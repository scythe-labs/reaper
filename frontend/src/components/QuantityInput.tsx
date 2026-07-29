// SPDX-License-Identifier: AGPL-3.0-or-later
//
// THE number-with-a-unit control, the only shape a quantity takes anywhere in the app.
// Two variants share one chrome:
//   - a unit picker ("500 [GB]", "2 [weeks]"): the value handed to the parent is always
//     in the base unit (bytes, or days) — the dropdown is only how you say it. Pick the
//     unit and type a round number; switching units keeps the same real value, just
//     shown differently.
//   - a fixed suffix ("30 days", "3 people", "6.5 / 10"): the unit cannot change, so it
//     renders as a quiet suffix inside the same box instead of a dropdown.

import { useId, useRef, useState, type ChangeEvent } from "react";

export interface Unit {
  label: string;
  factor: number; // how many base units one of these is (GB -> 1e9 bytes)
}

// Decimal, matching every other place a rule says GB (presets, coercion, rule
// descriptions all use 1e9). Mixing conventions showed the same cap as two numbers.
export const SIZE_UNITS: Unit[] = [
  { label: "MB", factor: 1e6 },
  { label: "GB", factor: 1e9 },
  { label: "TB", factor: 1e12 },
];

export const TIME_UNITS: Unit[] = [
  { label: "days", factor: 1 },
  { label: "weeks", factor: 7 },
  { label: "months", factor: 30 },
  { label: "years", factor: 365 },
];

/** The friendliest unit to show a base value in: the largest one where the value is still
 *  at least 1 of them (500 GB stays GB; 14 days becomes 2 weeks). Falls back to the first. */
function bestUnit(value: number, units: Unit[]): Unit {
  const sorted = [...units].sort((a, b) => b.factor - a.factor);
  return sorted.find((u) => value >= u.factor) ?? units[0]!;
}

/** The smallest positive number the box can draw in the unit it is showing. `trim` keeps two
 *  decimals, so anything under this renders as "0" -- which is what the blur clamp below is
 *  floored to, so a clamped box can never show a number it is not storing. Change one of these
 *  and you change the other (rule 67). */
const SHOWN_MIN = 0.01;

/** The smallest base value ANY of these units can draw at two decimals. A clamp stops
 *  here rather than at the current unit's own smallest step: flooring in the shown unit
 *  meant a byte cap typed as 0 in a TB box became 0.01 TB, ten gigabytes, a cap that
 *  permits real deletion where the operator asked for none. Rule 31 says a precision
 *  reduction on a field that can add deletion pressure takes the bound producing LESS
 *  pressure, so the floor drops to the smallest unit on offer and the box switches to it. */
function drawableFloor(units: Unit[]): number {
  return Math.min(...units.map((u) => u.factor)) * SHOWN_MIN;
}

/** Two decimals of the shown unit -- see SHOWN_MIN. */
function trim(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Where a number lives while it is being typed.
 *
 * A box whose text is re-derived from the stored number on every render gets rewritten
 * under the caret: clear it to retype, the stored value reads the empty box as 0, writes
 * "0" back, and the digits typed next land after it -- select-all + "25" saves 125. So
 * while the box has focus it shows what was typed, nothing else, and the value only moves
 * when the text actually parses: an empty or half-finished box ("", "7.") says nothing and
 * leaves the stored number alone. Leaving the box is the commit -- it goes back to showing
 * the stored value, pulled into range if what was typed was out of it.
 *
 * `shown` and the emitted number are both in whatever unit the box displays; a caller that
 * translates (QuantityInput's dropdown) translates on the way in and out.
 *
 * Exported for the handful of number boxes that carry no unit at all (a rank, a ramp bound),
 * which rule 40 leaves as plain boxes -- they still need this, and there is only one of it.
 */
export function useTypedNumber(
  shown: string,
  emit: (n: number) => void,
  bounds: { min?: number | undefined; max?: number | undefined } = {},
): {
  value: string;
  onFocus: () => void;
  onChange: (e: ChangeEvent<HTMLInputElement>) => void;
  onBlur: () => void;
} {
  const [text, setText] = useState(shown);
  const [typing, setTyping] = useState(false);

  return {
    value: typing ? text : shown,
    onFocus: () => {
      setText(shown);
      setTyping(true);
    },
    onChange: (e) => {
      const raw = e.target.value;
      setText(raw);
      // An empty box is someone midway through retyping, never a zero.
      if (raw.trim() === "") return;
      const n = Number(raw);
      if (Number.isFinite(n)) emit(n);
    },
    onBlur: () => {
      setTyping(false);
      const n = Number(text);
      if (text.trim() === "" || !Number.isFinite(n)) return; // nothing usable typed: keep what was stored
      const { min, max } = bounds;
      if (min != null && n < min) emit(min);
      else if (max != null && n > max) emit(max);
    },
  };
}

export function QuantityInput({
  value,
  onChange,
  units,
  min = 1,
  ariaLabel,
}: {
  value: number;
  onChange: (base: number) => void;
  units: Unit[];
  min?: number;
  ariaLabel?: string;
}) {
  const [unit, setUnit] = useState<Unit>(() => bestUnit(value, units));

  // The unit follows a value replaced from outside -- Discard, a preset, a media-type
  // switch, the re-seed after a save. A grace box left on "months" would otherwise show a
  // staged 7 days as 0.23 months: right, and unreadable. Our own emits are remembered so
  // typing 500 in a GB box never jumps the box to TB mid-keystroke.
  const mine = useRef(value);
  const seen = useRef(value);
  if (value !== seen.current) {
    const fromThisBox = value === mine.current;
    seen.current = value;
    if (!fromThisBox) setUnit(bestUnit(value, units));
  }

  const shown = String(trim(value / unit.factor));
  // The floor the box enforces. `min` is in BASE units and can sit below anything the SHOWN
  // unit can draw: a 1-byte floor in a GB box clamped a typed 0 to 1 byte and rendered it as
  // "0" -- a box reading zero over a stored value the sentence beside it called "1 B".
  //
  // Two things fix that together, and only together. The floor never drops below what the
  // smallest unit can draw (`drawableFloor`), and when the committed value is too small for
  // the CURRENT unit the box switches to one that can show it. Lifting the floor to the
  // current unit alone was the wrong half: it made the display honest by raising the stored
  // number, which on a deletion cap is the direction rule 31 forbids.
  const floor = Math.max(min, drawableFloor(units));
  const shownMin = floor / unit.factor;
  const typed = useTypedNumber(
    shown,
    (n) => {
      const base = Math.max(floor, Math.round(n * unit.factor));
      mine.current = base;
      onChange(base);
    },
    { min: shownMin },
  );
  // The unit drops only on the way OUT of the box, never mid-keystroke. Switching inside the
  // emit above moved the dropdown while someone was still typing: "0.5" passes through "0",
  // which clamps to the floor, and the box jumped to the smallest unit under the caret. The
  // commit is the only honest moment to restate a number in different words.
  const onBlur = () => {
    typed.onBlur();
    if (mine.current / unit.factor < SHOWN_MIN) setUnit(bestUnit(mine.current, units));
  };

  return (
    <span className="qty">
      {/* No unit description here, unlike FixedQuantity's twin below (rule 72). This variant's
          unit is a real control standing next to the box: it names itself `${ariaLabel} unit`
          and announces the unit as its own value, so the pairing is already reachable. Binding
          the number to it as well would say the unit twice on the way through the pair. */}
      <input
        type="number"
        min={shownMin}
        step="any"
        aria-label={ariaLabel}
        {...typed}
        onBlur={onBlur}
      />
      <select
        value={unit.label}
        aria-label={ariaLabel ? `${ariaLabel} unit` : "Unit"}
        onChange={(e) => {
          const next = units.find((u) => u.label === e.target.value)!;
          setUnit(next); // keep the same real value, just show it in the new unit
        }}
      >
        {units.map((u) => (
          <option key={u.label} value={u.label}>
            {u.label}
          </option>
        ))}
      </select>
    </span>
  );
}

/** The fixed-suffix variant: same box, but the unit is a word that cannot change
 *  ("30 days", "3 people", "2 seasons"). Values pass through untranslated. */
export function FixedQuantity({
  value,
  onChange,
  suffix,
  min = 0,
  max,
  step,
  width,
  ariaLabel,
  disabled,
}: {
  value: number | string;
  onChange: (next: number) => void;
  suffix: string;
  min?: number;
  max?: number;
  step?: number | "any";
  /** Digits the box should fit; keeps a 2-digit day box from being sized for 5. */
  width?: "narrow" | "regular";
  ariaLabel?: string;
  disabled?: boolean;
}) {
  const typed = useTypedNumber(String(value), onChange, { min, max });
  // The unit is the other half of the value, and it used to be `aria-hidden`, so the box
  // announced "Points this rule adds, 12" for a number that means 12 points -- the unit was on
  // screen and nowhere else. It is bound as the input's DESCRIPTION rather than folded into its
  // name, for two reasons.
  //
  // A description is read after the value ("12, points"), which is the order the pairing is
  // spoken in anyway; a name is read before it ("Points this rule adds in points, 12"), and
  // eleven of the fifteen call sites already name their unit in `ariaLabel`, so composing one
  // would make most of them stutter -- rule 21 binds a spoken string as hard as a printed one.
  //
  // And it points at the suffix ALREADY on screen, so the word exists once. Composing a name
  // from a table of spoken units would mint a second copy that drifts from the visible one, on
  // a control whose whole job is to pair a number with a unit (rule 144).
  //
  // Every call site passes `ariaLabel`, so this never lands on an unnamed box; the description
  // is bound unconditionally because the suffix is never absent.
  const unitId = useId();

  return (
    <span className={width === "narrow" ? "qty qty-narrow" : "qty"}>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        aria-label={ariaLabel}
        aria-describedby={unitId}
        disabled={disabled}
        {...typed}
      />
      <span className="qty-suffix" id={unitId}>
        {suffix}
      </span>
    </span>
  );
}
