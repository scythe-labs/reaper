// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What Plex's trash holds besides this reap's titles, read the one way everywhere.

import { useQuery } from "@tanstack/react-query";
import { api, type PlexTrash } from "./api";

/** What Plex's trash holds before a reap.
 *
 * Reaper empties a library's trash only when everything in it came from the reap. Plex's own
 * empty-after-scan preference has no such check, and Reaper's rescan triggers it. So items
 * already in the trash lose their library records when Plex empties it, and stay when Plex
 * does not.
 *
 * It hits Plex, so it is not polled. The reap sheet asks once when it opens. Retries are off,
 * because an unreadable trash is its own warning state (Unknown, never Absent).
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

/** What the reap sheet says about Plex's trash, and whether the operator must tick first.
 *
 * `show` is true for a count of at least one, for any library that could not be read, and for
 * a query that failed outright. The page must never read "we could not look" as "there is
 * nothing there".
 *
 * `plexEmpties` is Plex's empty-after-scan preference. `null` means it could not be read. It
 * never raises the notice on its own, because Plex ships it on and it would then stand in
 * front of every reap.
 *
 * `needsAck` is true unless Plex is known to leave its trash alone. Only then do no other
 * records go.
 */
export function trashWarning(
  data: PlexTrash | undefined,
  isError: boolean,
): {
  show: boolean;
  known: number;
  unreadable: boolean;
  plexEmpties: boolean | null;
  needsAck: boolean;
} {
  if (isError) return { show: true, known: 0, unreadable: true, plexEmpties: null, needsAck: true };
  if (!data || !data.configured)
    return { show: false, known: 0, unreadable: false, plexEmpties: null, needsAck: false };
  const unreadable = data.sections_unreadable > 0;
  const show = data.trashed > 0 || unreadable;
  return {
    show,
    known: data.trashed,
    unreadable,
    plexEmpties: data.empties_after_scan,
    needsAck: show && data.empties_after_scan !== false,
  };
}
