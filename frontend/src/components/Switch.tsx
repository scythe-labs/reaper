// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one switch. Every on/off state in the product renders as this control, so the
// meaning is learned once: a switch changes how Reaper behaves, right now. Checkboxes
// remain only where the user is picking items from a list (filters, bulk selection),
// which is a different gesture and should look like one.

import { REMOVES_ITS_ROW } from "../focus";

export function Switch({
  checked,
  onChange,
  disabled,
  ariaLabel,
  describedBy,
  removesRow,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** Required when the switch is not wrapped in a text label of its own: the row's
   *  visible label does not reach the input element. */
  ariaLabel?: string;
  /** The policy warning(s) this switch is the fix for, so the complaint reaches a reader
   *  standing on the control instead of only as text rendered beside it. `QuantityInput`
   *  and `Segmented` already take this. The switch needed it once a warning anchored on
   *  one. */
  describedBy?: string | undefined;
  /** This switch takes its own row out of the list when it is turned off, so
   *  `useRemovalFocus` can find where to leave focus once the row it was standing on is
   *  gone. Only the policy editor's leftover protection is one: every other switch has two
   *  positions and removes nothing, and marking one that does not would send focus somewhere
   *  no row was removed from.
   *
   *  Spelled with the explicit `undefined` for the same reason `describedBy` above is: under
   *  `exactOptionalPropertyTypes` a caller reading the flag off an optional field can only
   *  pass it through if the target accepts it. */
  removesRow?: boolean | undefined;
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
        {...(removesRow ? REMOVES_ITS_ROW : {})}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="slider" aria-hidden="true" />
    </span>
  );
}
