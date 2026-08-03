// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one switch. Every on/off STATE in the product renders as this control, so the
// meaning is learned once: a switch changes how Reaper behaves, right now. Checkboxes
// remain only where the user is picking items from a list (filters, bulk selection),
// which is a different gesture and should look like one.

export function Switch({
  checked,
  onChange,
  disabled,
  ariaLabel,
  describedBy,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** Required when the switch is not wrapped in a text label of its own: the row's
   *  visible label does not reach the input element. */
  ariaLabel?: string;
  /** The policy warning(s) this switch is the fix for, so the complaint reaches a reader
   *  standing ON the control instead of only as text rendered beside it (#174's shape).
   *  `QuantityInput` and `Segmented` already take this; the switch needed it once a warning
   *  anchored on one (#224). */
  describedBy?: string | undefined;
}) {
  return (
    <span className="switch">
      <input
        type="checkbox"
        role="switch"
        checked={checked}
        disabled={disabled}
        aria-label={ariaLabel}
        aria-describedby={describedBy}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="slider" aria-hidden="true" />
    </span>
  );
}
