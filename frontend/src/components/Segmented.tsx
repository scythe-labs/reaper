// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one either-or control. A choice between a few visible options renders as this
// segmented group, so both options are always readable at a glance. Dropdowns are for
// open lists (sources, fields, units), never for a binary the user should see whole.

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
  fill = false,
  variant = "pill",
  describedBy,
  disabled = false,
}: {
  value: T;
  /** [value, visible text] pairs, in display order. */
  options: readonly (readonly [T, string])[];
  onChange: (next: T) => void;
  label: string;
  /** Give every segment equal width, so short and long labels center in the track
   *  instead of the active pill hogging its side. Off by default. */
  fill?: boolean;
  /** The chrome, not the behavior. `pill` is the raised track the queue and policy screens
   *  wear. `flat` is one bordered box split by a hairline, with the chosen half tinted, which
   *  is what the approved mockup settled for the list forms. This is a variant rather than a
   *  second component: a second either-or control here would duplicate this file's semantics
   *  and CSS, right down to the `data-label` strut. */
  variant?: "pill" | "flat";
  /** Ids of the message(s) explaining what is wrong with this choice. On the group, not the
   *  segments: the complaint is about which option is in force, not about the button under
   *  the cursor. No `invalid` companion, because `aria-invalid` on a `role="group"` is not a
   *  state ARIA defines, and a policy warning does not refuse the value anyway. */
  describedBy?: string | undefined;
  /** True while a save carrying this choice is in flight. Without it this group is the one
   *  control in a settings row still pressable during the save: the press paints as taken,
   *  then the re-seed from the response puts it back, and the save bar clears in the same
   *  flush, so nothing on screen says the press was dropped.
   *
   *  Off by default because the policy page's consumers pass nothing. No control there
   *  carries a pending gate, and its save re-seeds the whole draft, so gating these segments
   *  alone would make them the odd one out on a page that is one question, not nine. */
  disabled?: boolean;
}) {
  return (
    <div
      className={["segmented", fill ? "fill" : "", variant === "flat" ? "flat" : ""]
        .filter(Boolean)
        .join(" ")}
      role="group"
      aria-label={label}
      aria-describedby={describedBy}
    >
      {options.map(([v, text]) => (
        <button
          key={v}
          type="button"
          className={value === v ? "seg active" : "seg"}
          // Reserve the bold (active) width so choosing a segment never shifts the track.
          data-label={text}
          aria-pressed={value === v}
          disabled={disabled}
          onClick={() => onChange(v)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}
