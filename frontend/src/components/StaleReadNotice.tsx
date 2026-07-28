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
 *  `PlexPanel`, `SecurityPanel`, `BackupPanel`, `AboutPanel` and `JobsPanel` keep their surface
 *  through a failed refetch; a seventh panel that starts doing so takes this line with it, rather
 *  than writing its own. Security is the one with a clock behind it: `useSafety` refetches every
 *  15 seconds, so its form met this state without the operator doing anything at all. Backup is
 *  the one where the never-loaded sentence was actively harmful rather than merely wrong: it says
 *  to reload, and a reload does not run the restore card's unmount cleanup, so the staged archive
 *  is orphaned by an operator doing what the page told them.
 *
 *  The last two arrived for a different reason and it is worth keeping straight. The first four
 *  keep their surface to protect a DRAFT (rule 146); About and Jobs hold none, and keep theirs
 *  because the last good row is still the best answer available and the never-loaded sentence is
 *  simply false above a page that is right there. Jobs is the one that reads worst stale: its rows
 *  carry next-run times and a running flag, so silence presents a schedule as current that the
 *  panel knows it could not confirm.
 *
 *  `what` names the thing on the page, because this line sits over more than settings now.
 *  A noun slot rather than a second sentence somewhere else: the claim is written once here, and
 *  a caller can only vary what it is about, never what it says. */
export function StaleReadNotice({ what = "these settings" }: { what?: string }) {
  return (
    <p className="notice notice-warn">
      Couldn't check {what} just now, so what's below may be out of date. Reload to try again.
    </p>
  );
}
