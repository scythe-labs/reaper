// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What a finished scan refreshes, and the one place that fires it.
//
// A scan replaces the snapshot every override-aware surface reads, so the moment one ends
// half the app is quoting numbers from the scan before it. This lives on the shell, the one
// component a scan cannot unmount, so a scan started anywhere (the Reap page's "Scan now",
// the scheduler, another device) always refreshes the page the operator is actually looking
// at, exactly as the finished-reap refresh does (see App's run-settled effect).

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { announce } from "./announce";
import type { ReasonKey } from "./api";
import i18next from "./i18n";

/** The caches a new snapshot actually changes, each with the surface it feeds.
 *
 *  Named, not a bare `invalidateQueries()`: that would refetch every mounted query at once,
 *  the logs, safety, the profile, every settings panel, every instance test, against a
 *  server that had just finished a full scan. A cache added later that reads from the scan
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
  // A scan syncs watch history first, so it moves the reach the popularity-window warning is
  // computed from. The fresh 0 in the simulator and the sentence explaining that 0 arrive from
  // the same event, and refreshing only the former would show the count without the reason.
  // The key is structurally unchanged across a scan, so nothing else re-keys this query; it
  // must be invalidated from this one list.
  ["validate"], // the policy editor's warnings, re-derived against the new watch history
  // A scan refreshes every protection list before it gathers (`snapshot.sync_protection_lists`),
  // so it moves each one's stored count, its last-checked time, and whether it is failing at
  // all. Without this a list that just started failing kept its green "Working, protecting N
  // titles" for as long as the panel stayed mounted, which is the reassuring direction.
  ["lists"],
  // A scan pushes the Plex "Leaving Soon" shelves before it returns
  // (`scan_runner._run_scan_locked` -> `leaving_soon.after_scan`, in the progress band 95-99,
  // still inside the running window this hook watches), which moves the pass's counts, its
  // timestamp and its result line. Without this entry, only the Jobs row's own "Update now"
  // would invalidate it, and with `staleTime: 30_000` and no focus refetch the row would keep
  // the previous pass's "N movies and M seasons on the shelves" under a heading reading "Runs
  // after every scan".
  ["leaving-soon-settings"], // the Jobs shelf row, and Plex's shelf status line
  // A scan syncs watch history and records what it could not read
  // (`Snapshot.watch_blind_items`), which are both numbers `/settings/watch-evidence` returns.
  // The sentence built from them names the last scan outright ("The last scan couldn't read the
  // plays for N items"), so a stale read here attributes an older count to a scan that has just
  // finished with a different one.
  ["watch-evidence"], // Plex's recorded-watch-history line
  // The threshold curve is built off the newest snapshot's candidates, so a finished scan
  // moves the delete-threshold slider's consequence sentence. The save-time half of its
  // staleness (a changed coverage floor takes effect at save, before any scan) is
  // invalidated by the policy save itself in `PolicyEditor`.
  ["threshold-curve"], // the policy editor's "N titles would be Condemned" readout
];

/** Refresh everything that hangs off the snapshot when a scan finishes.
 *
 *  The running -> not-running transition is the signal, so a mount that arrives after a scan
 *  has already ended re-invalidates nothing it just fetched. Call this once, from the shell.
 *  `scanning` comes from the shared `["scanStatus"]` poll rather than a second subscription
 *  here, so this hook adds no request of its own.
 *
 *  It only marks caches stale; it never reads them. A surface that must capture pre-scan
 *  state (the scan bar's before/after delta) can still do so from its own effect. An
 *  invalidation triggers an async refetch and leaves the cached data in place meanwhile, so
 *  a synchronous `getQueryData` in the same tick still sees the old snapshot whichever
 *  effect ran first. */
export function useScanSettled(scanning: boolean, error: ReasonKey | null = null): void {
  const queryClient = useQueryClient();
  const wasScanning = useRef(false);
  // Read at the transition, never as a dependency: `error` arrives in the same poll that turns
  // `running` off (api/scan.py sets both, the reason before `running = False` in its
  // `finally`), and a later change to it must not re-fire the effect. `useJobFlash` reads its
  // result through a ref for the same reason. Only its presence matters here, never its
  // words: this hook picks between two sentences of its own, it never renders the reason.
  const latestError = useRef(error);
  latestError.current = error;
  useEffect(() => {
    if (wasScanning.current && !scanning) {
      // Invalidated on both branches. A crashed scan writes no snapshot, so these refetches
      // are expected to come back unchanged. "Expected" is not "guaranteed" for a run that
      // stopped somewhere unknown, though, and a refetch showing current truth is never
      // wrong, where skipping one can leave a stale number on screen.
      for (const queryKey of SCAN_SETTLED_KEYS) void queryClient.invalidateQueries({ queryKey });
      // Every cache in the list above goes stale on this line, which moves most of the
      // numbers in the app at once. It can be triggered by the scheduler, by another device,
      // or by another tab, so there is no press to attach a sentence to and no control that
      // changed. An operator using a screen reader has no way to learn any of it unless this
      // hook says so out loud, since the transition is otherwise entirely visual.
      //
      // It says the numbers moved rather than naming a figure, because this hook
      // deliberately does not read the caches it marks stale, and the refetches have not
      // landed yet, so any count said here would be the previous scan's. The review queue's
      // own nudge and toast speak separately and say something narrower, which is right: this
      // is the shell, and it is the only voice on every other page.
      //
      // Being the only voice is also why it has to branch. `api/scan.py` clears `running`
      // in a `finally`, so a scan that crashed reaches this same edge, where a plain finished
      // sentence would report an update that never happened, on every page except the one
      // mounting the scan bar, the only surface rendering the error. `ScanBar` already
      // withholds its own "Finished" chip on `status.error_reason`; this is that same guard
      // applied at its sibling. A crashed scan writes no snapshot, which is what lets the
      // second sentence say the numbers are unchanged rather than only declining to claim
      // they moved.
      announce(
        latestError.current === null
          ? i18next.t("shell.scanSettled.finishedAnnounce")
          : i18next.t("shell.scanSettled.stoppedAnnounce"),
      );
    }
    wasScanning.current = scanning;
  }, [scanning, queryClient]);
}
