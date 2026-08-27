// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What Plex would remove besides the files a reap deletes, read the one way everywhere.

import { useQuery } from "@tanstack/react-query";
import { api, type PlexTrash } from "./api";

/** Whether emptying Plex's trash would take records this reap never touched.
 *
 * Reaper's end-of-run purge empties a library's whole trash. Items already in there when
 * the run started sit on both sides of the executor's before/after count, so they cancel
 * out of its gate and it cannot see them, yet the purge destroys their library records
 * anyway. This read is the only thing that can see them, so the operator gets told before
 * they reap and may go ahead anyway.
 *
 * It hits Plex, so it is not polled: the reap sheet asks once when it opens, and staleness
 * of a few minutes cannot matter for a warning the operator acknowledges by hand. Retries
 * are off for the same reason a failure is not silent here: an unreadable trash is its own
 * warning state (Unknown, never Absent), so failing fast reaches that state sooner than
 * three quiet retries would.
 */
export function usePlexTrash(enabled = true) {
  return useQuery<PlexTrash>({
    queryKey: ["plexTrash"],
    queryFn: api.plexTrash,
    enabled,
    retry: false,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  });
}

/** Does this state need the operator to acknowledge it before they can reap?
 *
 * Two causes, one decision: records that are not from this reap may go. A definite count
 * of at least one, or any library we could not read (unreadable is Unknown, never a
 * reassuring zero). Kept beside the hook so the reap sheet and the plan summary cannot
 * drift on what counts as a warning.
 *
 * A query that failed outright warns too, for the same reason: the page must never read
 * "we could not look" as "there is nothing there".
 */
export function trashWarning(
  data: PlexTrash | undefined,
  isError: boolean,
): { show: boolean; known: number; unreadable: boolean; autoEmpties: boolean } {
  if (isError) return { show: true, known: 0, unreadable: true, autoEmpties: false };
  if (!data || !data.configured)
    return { show: false, known: 0, unreadable: false, autoEmpties: false };
  const unreadable = data.sections_unreadable > 0;
  return {
    show: data.trashed > 0 || unreadable,
    known: data.trashed,
    unreadable,
    // This must never trigger the notice on its own. Plex ships this preference on, and most
    // servers never change it, so warning about it over an empty trash would put a standing
    // amber block in front of every reap and teach the operator to click past the one that
    // matters. It only rides on a trash that already warrants a warning, where it says which
    // mechanism will do the emptying. `null` means the preference could not be read and says
    // nothing, rather than claiming Plex does not empty its own trash.
    autoEmpties: data.empties_after_scan === true,
  };
}
