// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What a finished scan refreshes, and the one place that fires it.
//
// A scan replaces the snapshot every override-aware surface reads, so the moment one ends
// half the app is quoting numbers from the scan before it. This used to live inside the scan
// bar, which is mounted on Settings alone -- so a scan started anywhere else (the Reap page's
// "Scan now", the scheduler, another device) finished with nothing refreshing the page the
// operator was actually looking at. It belongs on the shell, the one component a scan cannot
// unmount, exactly as the finished-reap refresh does (see App's run-settled effect).

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

/** The caches a new snapshot actually changes, each with the surface it feeds.
 *
 *  Named, not a bare `invalidateQueries()`: that refetched EVERY mounted query at once -- the
 *  logs, safety, the profile, every settings panel, every instance test -- against a server
 *  that had just finished a full scan (P-5). A cache added later that reads from the scan
 *  belongs in this list; one that does not, does not. */
export const SCAN_SETTLED_KEYS: string[][] = [
  ["snapshot"], // the scan's own totals, in the header and on the Reap page
  ["candidates"], // the review queue, every loaded page
  ["candidates-unfiltered"], // the "how many the filters are hiding" count beside it
  ["candidate"], // an open item panel
  ["group"], // an open show panel, and every expanded season list
  ["reap-breakdown"], // the Reap page's ledger, including its expired-spares line
  ["run"], // one plan's counts, re-derived against the new condemned set
  ["fairness"], // Scales matches requests to the last scan
  ["season-shape"], // the policy editor's "from your last scan" advisory
  ["simulate"], // "What this would do", re-decided against the new snapshot
  ["vocabulary-values"], // the genre/library value lists, drawn from the scan's items
  ["schedule"], // the scan job's own last-run line
];

/** Refresh everything that hangs off the snapshot when a scan finishes.
 *
 *  The running -> not-running transition is the signal, so a mount that arrives after a scan
 *  has already ended re-invalidates nothing it just fetched. Call this once, from the shell.
 *  `scanning` comes from the shared `["scanStatus"]` poll rather than a second subscription
 *  here, so this hook adds no request of its own.
 *
 *  It only marks caches stale; it never reads them. A surface that must capture pre-scan
 *  state (the scan bar's before/after delta) can still do so from its own effect -- an
 *  invalidation triggers an async refetch and leaves the cached data in place meanwhile, so
 *  a synchronous `getQueryData` in the same tick still sees the old snapshot whichever
 *  effect ran first. */
export function useScanSettled(scanning: boolean): void {
  const queryClient = useQueryClient();
  const wasScanning = useRef(false);
  useEffect(() => {
    if (wasScanning.current && !scanning) {
      for (const queryKey of SCAN_SETTLED_KEYS) void queryClient.invalidateQueries({ queryKey });
    }
    wasScanning.current = scanning;
  }, [scanning, queryClient]);
}
