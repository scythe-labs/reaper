// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one line a settings panel shows when its own read failed but its form is still up.

/** Says the values on screen may be out of date, for a panel whose read failed after a good load.
 *
 *  A settings panel only trades its form for "couldn't load these settings" when NOTHING ever
 *  landed (`!data`). A refetch that fails afterwards must keep the form, because React Query
 *  still holds the last good row and the operator may have a draft typed into it that the panel
 *  is reporting upward: replacing the form there would take the boxes, the Save and the Discard
 *  off screen while the guard went on demanding a discard for edits with nowhere left to put
 *  them (rule 146). Keeping the form silently is the other half of that mistake, though, since
 *  the panel then presents state it knows is stale as current. Rule 17/36 wants both the pending
 *  and the failed read handled explicitly, so this is what the failed one says.
 *
 *  One component for every panel, so the wording cannot drift (rule 144). `GeneralPanel`,
 *  `PlexPanel` and `SecurityPanel` keep their form through a failed refetch; a fourth panel that
 *  starts doing so takes this line with it, rather than writing its own. Security is the one with
 *  a clock behind it: `useSafety` refetches every 15 seconds, so its form met this state without
 *  the operator doing anything at all. */
export function StaleReadNotice() {
  return (
    <p className="notice notice-warn">
      Couldn't check these settings just now, so what's below may be out of date. Reload to try
      again.
    </p>
  );
}
