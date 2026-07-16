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

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, type Snapshot } from "../api";
import { bytes, count, date } from "../format";

//: Friendly names for the scan's internal phases, so the status line reads in English.
const PHASE_LABELS: Record<string, string> = {
  starting: "Starting",
  history: "Reading watch history",
  lists: "Refreshing protection lists",
  gathering: "Gathering your library",
  scoring: "Scoring",
  done: "Finishing up",
  complete: "Done",
};

function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] ?? phase;
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

  // When a scan finishes, refresh everything that hangs off the snapshot (the queue, the
  // reap plan, the freshness line). The transition running -> not-running is the signal.
  useEffect(() => {
    if (wasScanning.current && !scanning) void queryClient.invalidateQueries();
    wasScanning.current = scanning;
  }, [scanning, queryClient]);

  async function start() {
    // Seed the cache with the returned status so polling begins immediately.
    const started = await api.startScan();
    queryClient.setQueryData(["scanStatus"], started);
  }

  const pct = status && status.total > 0 ? Math.round((status.done / status.total) * 100) : null;

  return (
    <section className="scanbar">
      <div className="scanbar-main">
        <button className="primary" onClick={start} disabled={scanning}>
          {scanning ? "Scanning…" : "Scan library"}
        </button>

        {snapshot && !scanning && (
          <p className="muted">
            Last scan {date(snapshot.created_at)} &middot; {count(snapshot.item_count)} items
            &middot; <strong>{count(snapshot.condemned)}</strong> would be deleted, freeing{" "}
            <strong>{bytes(snapshot.reclaimable_bytes)}</strong>
          </p>
        )}

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
          <div className="bar-fill" style={{ width: pct !== null ? `${pct}%` : "100%" }} />
        </div>
      )}

      {status?.error && <p className="error">{status.error}</p>}

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
