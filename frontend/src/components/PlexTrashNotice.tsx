// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one warning that Plex's trash holds records this reap never touched.

import { Notice } from "./Notice";

/** Warns that emptying Plex's trash takes more than this reap deletes.
 *
 * One component for both surfaces so the wording cannot drift: the plan summary shows it
 * to inform (Execute only opens the sheet, so nothing irreversible is one click away), and
 * the reap sheet shows it with `onAck`, where it must be ticked before Reap enables.
 *
 * Three shapes, and the unreadable one never reports a reassuring zero: a library Reaper
 * could not read is Unknown, never Absent, and "we could not look" is exactly when the
 * operator most needs telling.
 *
 * `autoEmpties` adds a sentence rather than a fourth shape. It never brings this notice up on
 * its own: Plex ships that preference on and most servers leave it, so it would stand in front
 * of every reap and train the operator past the warning that matters. Here it answers the
 * question the warning above raises, which is who does the emptying.
 */
export function PlexTrashNotice({
  known,
  unreadable,
  autoEmpties,
  acked,
  onAck,
}: {
  known: number;
  unreadable: boolean;
  autoEmpties?: boolean;
  acked?: boolean;
  onAck?: (next: boolean) => void;
}) {
  const items = known === 1 ? "1 item" : `${known} items`;
  return (
    // `standing`: both surfaces mount this on a Plex read, never on a press. In the sheet it also
    // holds the checkbox that gates Reap, so it has to be navigated to rather than shouted, and
    // `ReapConfirm` already withholds its focus move so the notice is met in document order.
    <Notice tone="warn" standing>
      {known > 0 ? (
        <>
          <strong>Plex may also remove records for items Reaper didn't delete.</strong> Its trash
          already holds {unreadable ? `at least ${items}` : items}
          {unreadable ? ", and some libraries couldn't be read" : ""}. Emptying it removes their
          watch history, ratings, and collections. No files are deleted.
        </>
      ) : (
        <>
          <strong>Reaper couldn't read Plex's trash.</strong> It can't say what's in there, and
          emptying it removes the watch history, ratings, and collections of everything that is. No
          files are deleted.
        </>
      )}
      {autoEmpties && (
        <>
          {" "}
          Plex also empties its own trash after every scan, so Reaper's refresh clears it even when
          Reaper's own checks would have left it alone.
        </>
      )}
      {onAck && (
        <label className="trash-ack">
          <input type="checkbox" checked={!!acked} onChange={(e) => onAck(e.target.checked)} />
          <span>I understand, continue anyway</span>
        </label>
      )}
    </Notice>
  );
}
