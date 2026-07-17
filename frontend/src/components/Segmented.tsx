// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one either-or control. A choice between a few visible options renders as this
// segmented group, so both options are always readable at a glance; dropdowns are for
// open lists (sources, fields, units), never for a binary the user should see whole.

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  /** [value, visible text] pairs, in display order. */
  options: readonly (readonly [T, string])[];
  onChange: (next: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          className={value === v ? "seg active" : "seg"}
          aria-pressed={value === v}
          onClick={() => onChange(v)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}
