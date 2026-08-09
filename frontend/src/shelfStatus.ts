// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Which record answers for the Leaving Soon shelf right now.
//
// Two records describe it and only one of them is current: `last`, the last pass that
// completed, and `last_skip`, a scan that finished without updating the shelf at all.
// `leaving_soon.after_scan` writes the skip and deliberately leaves the completed pass
// alone, because a skipped pass wrote nothing to Plex -- the shelf still holds what the
// last completed pass put there, and those counts are the only true ones anybody has.
// Nothing ever clears a skip, so what retires it is a later pass carrying a later
// timestamp, which makes this comparison the whole mechanism.
//
// Written once because two surfaces make it (rule 104): the Jobs row, which turns it red
// and names the reason, and the Plex panel's shelf status line, which for its whole life
// read `last` alone and so reported a shelf that had silently stopped updating as a
// confident current verdict, on the screen an operator goes to when they suspect exactly
// that.

import type { LeavingSoonSettings } from "./api";

/** Is a scan that skipped the shelf newer than the last completed pass?
 *
 *  False when there is no skip, and false when a pass has completed since one. True with a
 *  skip and no completed pass at all: the shelf has been broken since the day it was
 *  switched on, which is when silence misleads most.
 */
export function shelfSkipIsCurrent(shelf: LeavingSoonSettings | undefined): boolean {
  const skip = shelf?.last_skip;
  if (!skip) return false;
  const last = shelf?.last;
  return !last || new Date(skip.at).getTime() > new Date(last.at).getTime();
}
