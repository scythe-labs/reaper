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

import { useState } from "react";

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

function trim(n: number): number {
  return Math.round(n * 100) / 100;
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
  const shown = trim(value / unit.factor);

  return (
    <span className="qty">
      <input
        type="number"
        min={min / unit.factor}
        step="any"
        value={shown}
        aria-label={ariaLabel}
        onChange={(e) => onChange(Math.round((Number(e.target.value) || 0) * unit.factor))}
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
  return (
    <span className={width === "narrow" ? "qty qty-narrow" : "qty"}>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        aria-label={ariaLabel}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value) || 0)}
      />
      <span className="qty-suffix" aria-hidden="true">
        {suffix}
      </span>
    </span>
  );
}
