// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The scan, its progress, and the state of the snapshot it produced.
//
// The scan runs as a background job on the server (see api/scan.py), detached from the
// request that starts it -- so closing the tab or switching screens does not stop it. This
// component starts a scan and then *polls* its progress, which means it also picks up a scan
// that is already in flight when you return to the page.
//
// A scan is read-only: it reads from the *arr and Tautulli, scores, and writes rows to
// Reaper's own database. GuardedTransport would refuse a mutating call even if one were tried.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type Snapshot } from "../api";
import { bytes, count, date, totalBytes } from "../format";

//: Friendly names for the scan's internal phases, so the status line reads in English.
//  Exported because the first-run wizard shows the same progress line; one table keeps
//  the two surfaces from drifting into raw phase ids.
export const PHASE_LABELS: Record<string, string> = {
  starting: "Starting",
  history: "Reading watch history",
  lists: "Refreshing protection lists",
  gathering: "Gathering your library",
  scoring: "Scoring",
  done: "Finishing up",
  complete: "Done",
};

export function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] ?? phase;
}

/** What the totals did between the scan that just finished and the one before it.
 *
 *  A scan replaces the snapshot underneath the whole page, so the queue and the totals
 *  change with nothing to compare them against. Null when nothing moved is deliberate:
 *  a line saying "no change" every time would be noise. */
function scanDelta(
  before: { condemned: number; freeable: number },
  after: Snapshot,
): string | null {
  const items = after.condemned - before.condemned;
  const size = after.reclaimable_bytes - before.freeable;
  if (items === 0 && size === 0) return null;

  const parts: string[] = [];
  if (items !== 0) {
    parts.push(`${count(Math.abs(items))} ${items > 0 ? "more" : "fewer"} to remove`);
  }
  if (size !== 0) {
    parts.push(`${bytes(Math.abs(size))} ${size > 0 ? "more" : "less"} to free`);
  }
  return `Compared with the scan before: ${parts.join(", ")}.`;
}

export function ScanBar({ snapshot }: { snapshot: Snapshot | undefined }) {
  const queryClient = useQueryClient();

  const { data: status } = useQuery({
    queryKey: ["scanStatus"],
    queryFn: api.scanStatus,
    // Poll only while a scan is actually running; otherwise sit quiet.
    refetchInterval: (query) => (query.state.data?.running ? 1000 : false),
  });

  const scanning = status?.running ?? false;
  const wasScanning = useRef(false);

  // The totals from the scan before this one, kept across the swap so the new numbers
  // have something to be read against. Cleared when the next scan starts.
  const [before, setBefore] = useState<{
    id: number;
    condemned: number;
    freeable: number;
  } | null>(null);

  // When a scan finishes, refresh everything that hangs off the snapshot (the queue, the
  // reap plan, the freshness line). The transition running -> not-running is the signal.
  // The old snapshot is read out of the cache *before* the invalidation replaces it.
  useEffect(() => {
    if (wasScanning.current && !scanning) {
      const previous = queryClient.getQueryData<Snapshot>(["snapshot"]);
      setBefore(
        previous
          ? {
              id: previous.id,
              condemned: previous.condemned,
              freeable: previous.reclaimable_bytes,
            }
          : null,
      );
      void queryClient.invalidateQueries();
    }
    if (!wasScanning.current && scanning) setBefore(null);
    wasScanning.current = scanning;
  }, [scanning, queryClient]);

  // A mutation, not a fire-and-forget async onClick: a start that fails must say so,
  // or the button appears to do nothing at all.
  const start = useMutation({
    mutationFn: () => api.startScan(),
    // Seed the cache with the returned status so polling begins immediately.
    onSuccess: (started) => queryClient.setQueryData(["scanStatus"], started),
  });

  // The server hands us a monotonic 0-100 that rises smoothly across the scan's phases.
  // (The old done/total math read 100% before any work began, because the early phases
  // report total=0, then jumped as the denominator changed meaning between phases.)
  const pct = status ? status.percent : null;

  // Only against a *different* snapshot: the first scan ever has nothing to compare to,
  // and a scan that wrote no new snapshot must not report a change of zero.
  const delta =
    before && snapshot && snapshot.id !== before.id ? scanDelta(before, snapshot) : null;

  return (
    <section className="scanbar">
      <div className="scanbar-main">
        <button
          className="primary"
          onClick={() => start.mutate()}
          disabled={scanning || start.isPending}
        >
          {scanning ? "Scanning…" : start.isPending ? "Starting…" : "Scan library"}
        </button>

        {snapshot && !scanning && (
          <p className="muted">
            Last scan {date(snapshot.created_at)} &middot; {count(snapshot.item_count)} items
            &middot; <strong>{count(snapshot.condemned)}</strong> would be removed, freeing{" "}
            <strong>{totalBytes(snapshot.reclaimable_bytes, snapshot.unknown_size_items)}</strong>
          </p>
        )}

        {delta && !scanning && <p className="muted">{delta}</p>}

        {!snapshot && !scanning && (
          <p className="muted">No scan has run yet. A scan only reads. It cannot delete.</p>
        )}

        {scanning && (
          <p className="muted">
            {phaseLabel(status!.phase)}
            {status!.detail && ` · ${status!.detail}`}
            {pct !== null && ` · ${pct}%`}
            {" · you can leave this page; it keeps running."}
          </p>
        )}
      </div>

      {scanning && (
        <div className="bar">
          <div className="bar-fill" style={{ width: `${pct ?? 0}%` }} />
        </div>
      )}

      {start.error && (
        <p className="notice notice-error">The scan didn't start: {start.error.message}</p>
      )}
      {status?.error && (
        <p className="notice notice-error">The scan hit a problem: {status.error}</p>
      )}

      {snapshot?.degraded && (
        <p className="warn">
          <strong>This scan came back incomplete.</strong> {snapshot.degraded_reason} You can still
          look at it, but Reaper won't act on it. A scan that missed a source could show a list
          that looks complete when it isn't.
        </p>
      )}
    </section>
  );
}
