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

import { useId, useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";
import i18next from "../i18n";

export interface Unit {
  label: string;
  factor: number; // how many base units one of these is (GB -> 1e9 bytes)
  /** The word for exactly one of these, where the unit inflects at all. Omitted by the size
   *  units, which do not: "1 GB" is already right and "1 GBs" would not be. */
  singular?: string;
}

/** Decimal, matching every other place a rule says GB (presets, coercion, rule
 *  descriptions all use 1e9). Mixing conventions showed the same cap as two numbers.
 *
 *  A function, not a constant: this module is in the eager bundle, so a string resolved in its
 *  body would stay English for the life of the page (`i18n-module-scope.test.ts`). */
export const sizeUnits = (): Unit[] => [
  { label: i18next.t("shell.quantityInput.units.mb"), factor: 1e6 },
  { label: i18next.t("shell.quantityInput.units.gb"), factor: 1e9 },
  { label: i18next.t("shell.quantityInput.units.tb"), factor: 1e12 },
];

/** The time units, late-resolved for the same reason `sizeUnits` is. */
export const timeUnits = (): Unit[] => [
  {
    label: i18next.t("shell.quantityInput.units.days"),
    factor: 1,
    singular: i18next.t("shell.quantityInput.units.day"),
  },
  {
    label: i18next.t("shell.quantityInput.units.weeks"),
    factor: 7,
    singular: i18next.t("shell.quantityInput.units.week"),
  },
  {
    label: i18next.t("shell.quantityInput.units.months"),
    factor: 30,
    singular: i18next.t("shell.quantityInput.units.month"),
  },
  {
    label: i18next.t("shell.quantityInput.units.years"),
    factor: 365,
    singular: i18next.t("shell.quantityInput.units.year"),
  },
];

/** How a unit is WORDED beside a given quantity, as against `label`, which is its name and
 *  its stored value. A dormancy floor of 365 days is drawn by `bestUnit` below as exactly 1
 *  of the largest unit it clears, so "1 years" was not an edge case but the shape every round
 *  policy default takes: 365, 30, 7 (#415). */
function wordedFor(unit: Unit, quantity: number): string {
  return quantity === 1 && unit.singular ? unit.singular : unit.label;
}

/** The friendliest unit to show a base value in: the largest one where the value is still
 *  at least 1 of them (500 GB stays GB; 14 days becomes 2 weeks). Falls back to the first. */
function bestUnit(value: number, units: Unit[]): Unit {
  const sorted = [...units].sort((a, b) => b.factor - a.factor);
  return sorted.find((u) => value >= u.factor) ?? units[0]!;
}

/** How many decimals of its unit the box draws. Everything about the precision of this control
 *  derives from this one number -- the smallest value it can show, the rounding on the way out,
 *  and the cut on the way in -- rather than each site spelling "two decimals" its own way
 *  (rule 67). */
const SHOWN_DECIMALS = 2;

/** The smallest positive number the box can draw in the unit it is showing: anything under this
 *  renders as "0", which is what the blur clamp below is floored to, so a *clamped* box can
 *  never show a number it is not storing. That is the clamp alone. What a box does with a TYPED
 *  number carrying more decimals than it draws is `cutToShown`. */
const SHOWN_MIN = 10 ** -SHOWN_DECIMALS;

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
  const scale = 10 ** SHOWN_DECIMALS;
  return Math.round(n * scale) / scale;
}

/** A typed number cut to the decimals the box actually draws, dropping the rest rather than
 *  rounding them. Typing 1.234 in a GB box used to emit 1_234_000_000 while the box redrew
 *  "1.23", so the cap in force was 4 MB above the one on screen, on the control whose whole
 *  job is to state a bound. Cutting rather than rounding is rule 31: `trim` rounds to nearest,
 *  which on 1.236 would raise the cap to 1.24 GB and permit deletion the operator never
 *  authorized. Every box here has a positive floor, so cutting toward zero is always downward.
 *
 *  Both halves of this are binary floating point traps, and each one on its own turns a typed
 *  0.29 into a stored 0.28 -- this same defect pointing the other way, and a whole display step
 *  wide rather than a hidden digit. Arithmetic cannot do it: `Math.floor(0.29 * 100) / 100` is
 *  0.28, because 0.29 * 100 is 28.999999999999996. Nor can the exact decimal text:
 *  `(0.29).toFixed(20)` is "0.28999999999999998002", which is the binary value and not the one
 *  anybody typed. So the number is first read back at a precision past anything a person types
 *  into this box and short of where the representation error lives, and cut from there. */
const TYPED_DECIMALS = 10;

function cutToShown(n: number): number {
  const [whole = "0", decimals = ""] = n.toFixed(TYPED_DECIMALS).split(".");
  return Number(`${whole}.${decimals.slice(0, SHOWN_DECIMALS)}`);
}

/** Whether `n` fits in the decimals the field BEHIND the box can hold. `undefined` decimals is
 *  a box that takes any precision, which is the changeable-unit twin and nothing else.
 *
 *  Read off the decimal text at `TYPED_DECIMALS` for the same reason `cutToShown` does: the
 *  arithmetic form cannot tell a typed 0.29 from binary floating point's version of it. */
function fitsDecimals(n: number, decimals: number | undefined): boolean {
  if (decimals === undefined) return true;
  const rest = n.toFixed(TYPED_DECIMALS).split(".")[1] ?? "";
  return /^0*$/.test(rest.slice(decimals));
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
  bounds: {
    min?: number | undefined;
    max?: number | undefined;
    /** Decimals the field behind this box can hold. Omit only where the field really does take
     *  any precision; a whole-number field passes 0, and gets a box that cannot emit 1.5. */
    decimals?: number | undefined;
  } = {},
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
      if (!Number.isFinite(n)) return;
      // A number carrying more decimals than the field can hold is the same kind of text as
      // "7." -- on the way to a value, not a value -- so it is left alone exactly the same way,
      // and the box goes back to showing the stored number when it is left. Nothing is rounded,
      // because rounding needs a direction and this control has no one direction: half its call
      // sites are caps, where DOWN is the bound with less deletion pressure (rule 31), and half
      // are protections, where down is the one with more. What it stores instead is the last
      // number the operator actually typed in the field's own units.
      //
      // Withholding is the whole fix. The browser will not do it: a bare `<input type=number>`
      // has an implicit step of 1, and Chrome duly marks a typed 1.5 `stepMismatch` -- and then
      // hands the change handler "1.5" regardless, because step is checked at form validation
      // and this control never submits a form. Driven in Chrome 150; an explicit `step={1}`
      // behaves identically, which is why this is not fixed with an attribute (#296).
      if (!fitsDecimals(n, bounds.decimals)) return;
      emit(n);
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
  describedBy,
  invalid,
}: {
  value: number;
  onChange: (base: number) => void;
  units: Unit[];
  min?: number;
  ariaLabel?: string;
  /** Ids of the message(s) explaining what is wrong with this box's value. Same contract as
   *  `FixedQuantity`'s (rule 72); there is no unit id to join here, for the reason below. */
  describedBy?: string | undefined;
  /** True while the form REFUSES this value -- the action the box feeds is disabled and the
   *  press is a no-op, as a backwards ramp disables Add. Not for a value that merely draws a
   *  warning: `aria-invalid` states a refusal, so a setting the app will accept must not carry
   *  it. `FixedQuantity` has no such prop for exactly that reason (rule 72). */
  invalid?: boolean | undefined;
}) {
  const { t } = useTranslation();
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
  // The number is cut to the decimals the box draws BEFORE it is scaled, so the box cannot
  // keep precision it declines to show (`cutToShown`). Where 0.01 of the shown unit is a whole
  // number of base units the two then agree exactly, which is every size unit -- 0.01 MB is
  // 10,000 bytes. Where it is not, the base unit is the coarser grid and wins: 0.01 weeks is
  // under a tenth of a day and `grace_days` is whole days, so the value settles on a day and
  // the box redraws the conversion of it (9 days as "1.29 weeks"). That residual is the unit
  // dropdown's own arithmetic, not kept precision, and picking "days" shows the value exactly.
  const typed = useTypedNumber(
    shown,
    (n) => {
      const base = Math.max(floor, Math.round(cutToShown(n) * unit.factor));
      mine.current = base;
      onChange(base);
    },
    // No `decimals` here, unlike the fixed-suffix twin below (rule 72), and it is not a
    // deferral. A fraction of the SHOWN unit is exactly what this box is for -- 0.5 GB is a real
    // cap -- and the number it hands the parent is already whole in the BASE unit, because the
    // emit above rounds it there. Its precision is bounded twice over by `cutToShown` and by
    // that rounding, so there is no value it can emit that the field behind it cannot hold.
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
        aria-describedby={describedBy}
        aria-invalid={invalid ? true : undefined}
        {...typed}
        onBlur={onBlur}
      />
      <select
        value={unit.label}
        aria-label={
          ariaLabel
            ? t("shell.quantityInput.unitLabelWithName", { ariaLabel })
            : t("shell.quantityInput.unitLabel")
        }
        onChange={(e) => {
          const next = units.find((u) => u.label === e.target.value)!;
          setUnit(next); // keep the same real value, just show it in the new unit
        }}
      >
        {/* Worded from the drawn value, so the closed box reads "1 year" and not "1 years". A
            native select has only one string per option -- what it shows closed IS the selected
            option's text -- so the whole list inflects together and the open list reads
            "day, week, month, year" beside a 1. That is the coherent half of the choice:
            inflecting the selected one alone would leave the list mixed. The `value` stays the
            plural `label`, so what the change handler matches on never moves. */}
        {units.map((u) => (
          <option key={u.label} value={u.label}>
            {wordedFor(u, Number(shown))}
          </option>
        ))}
      </select>
    </span>
  );
}

/** How many decimals the field behind a fixed-suffix box can hold, read off the box's own
 *  `step` so the two cannot disagree (rule 67) -- there is no second prop to keep in step with
 *  the first, and no call site has to restate in a `decimals` what it already said in a `step`.
 *
 *  **No step means whole numbers**, which is not an invention: HTML already defines `step` as 1
 *  when it is absent, so `min={1} max={1000}` with nothing else declares integers, and the seven
 *  policy boxes written that way are all backed by an `int` in `engine/policy.py`.
 *
 *  `step` is read as a PRECISION, never as a ladder the value must land on. `min_votes` ships
 *  `step={100}` so the spinner moves in hundreds, and 250 is still a perfectly legal vote floor:
 *  snapping to the grid would rewrite the operator's own number, which is the deletion-path
 *  version of the bug this is fixing. Only the decimals are taken.
 *
 *  Exported for the two ramp bounds, which rule 40 leaves as plain boxes: they declare a `step`
 *  the same way and must read it the same way, rather than restating the tenths test twice. */
export function decimalsOfStep(step: number | "any" | undefined): number | undefined {
  if (step === "any") return undefined;
  if (step === undefined) return 0;
  const text = String(step);
  // An exponent form would need parsing this does not do, and reading one wrong would WIDEN the
  // box rather than narrow it, so it declines to guess rather than guess loosely. No call site
  // uses one; if one ever does, it gets the old any-precision behavior and not a wrong bound.
  if (text.includes("e") || text.includes("E")) return undefined;
  return text.split(".")[1]?.length ?? 0;
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
  describedBy,
  autoFocus,
  onKeyDown,
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
  /** Ids of the message(s) explaining what is wrong with this box's value -- a policy warning
   *  beside the control that fixes it. Joined with the unit below rather than replacing it:
   *  the number still needs its unit while it is also being complained about. */
  describedBy?: string | undefined;
  // No `invalid` here, unlike `QuantityInput` above (rule 72). Every caller of this one is a
  // policy control, and a policy warning of either severity still saves, so there is no state
  // this box could honestly report as invalid.
  /** For a box that opens focused, like the spare menu's custom length. */
  autoFocus?: boolean;
  /** For a box that submits on Enter. Runs BEFORE the typing handler below sees the key, which
   *  is what a submit needs -- and it is why this is a prop rather than something a caller can
   *  reach past the component: the spare menu had rebuilt this box by hand to get it, and a
   *  hand-built copy does not inherit the unit's accessible binding below. */
  onKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void;
}) {
  const typed = useTypedNumber(String(value), onChange, {
    min,
    max,
    decimals: decimalsOfStep(step),
  });
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
        // Unit first, then whatever is wrong with the value: "40, titles, that is more than
        // the run cap allows" reads in the order the operator needs it.
        aria-describedby={describedBy ? `${unitId} ${describedBy}` : unitId}
        disabled={disabled}
        autoFocus={autoFocus}
        onKeyDown={onKeyDown}
        {...typed}
      />
      <span className="qty-suffix" id={unitId}>
        {suffix}
      </span>
    </span>
  );
}
