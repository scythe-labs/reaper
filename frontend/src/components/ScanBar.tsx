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
import { announce } from "../announce";
import { api, type ScheduledJob, type Snapshot } from "../api";
import { bytes, count, totalBytes } from "../format";
import { JobStatus, useJobFlash } from "./JobStatus";
import { Notice } from "./Notice";

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
  shelves: "Updating shelves",
  complete: "Done",
};

export function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] ?? phase;
}

/** What the wait is called: the progress bar's name, and the lead of the sentence said when a
 *  scan starts. One string for both (rule 144). */
const SCANNING_LABEL = "Scanning your library";

/** The line under the bar, shown and said. For an operation that can run for many minutes, the
 *  fact that walking away does not cancel it is the most useful thing an operator can be told,
 *  and it was on screen only. */
const KEEPS_RUNNING = "You can leave this page; it keeps running.";

/** What the totals did between the scan that just finished and the one before it.
 *
 *  A scan replaces the snapshot underneath the whole page, so the queue and the totals
 *  change with nothing to compare them against. Null when nothing moved is deliberate:
 *  a line saying "no change" every time would be noise. */
/** Scan-to-scan movement. Both byte figures cover only the items that had a size, and
 *  the two scans can have different unmeasured populations -- an item measured last time
 *  and not this time leaves the total on its own, which reads as progress that did not
 *  happen. So when either scan is carrying unknowns the difference is qualified rather
 *  than dropped: a line the operator is used to must never quietly change meaning. */
function scanDelta(
  before: { condemned: number; freeable: number; unknownSize: number },
  after: Snapshot,
): string | null {
  const items = after.condemned - before.condemned;
  const size = after.reclaimable_bytes - before.freeable;
  const unknowns = Math.max(before.unknownSize, after.unknown_size_items);
  if (items === 0 && size === 0) return null;

  const parts: string[] = [];
  if (items !== 0) {
    parts.push(`${count(Math.abs(items))} ${items > 0 ? "more" : "fewer"} to remove`);
  }
  if (size !== 0) {
    const qualifier =
      unknowns > 0 ? `, ${count(unknowns)} ${unknowns === 1 ? "size" : "sizes"} unknown` : "";
    parts.push(`${bytes(Math.abs(size))} ${size > 0 ? "more" : "less"} to free${qualifier}`);
  }
  return `Compared with the scan before: ${parts.join(", ")}.`;
}

/** The library-scan row on the Jobs page: the marquee run action, its last-scan stats and
 *  live progress, and an Edit that opens the scan's schedule. It owns the scan lifecycle
 *  (start, poll, delta, degraded) exactly as before; the title, description and schedule
 *  line are handed in so the copy lives in one place with the other jobs. */
export function ScanRow({
  snapshot,
  scanJob,
  title,
  desc,
  scheduleText,
  onEdit,
  canEdit,
}: {
  snapshot: Snapshot | undefined;
  /** The scan's own schedule entry. Carries `last_run_at`/`last_ok`/`last_result` only for
   *  a SCHEDULED scan that crashed outright and wrote no snapshot (see `get_schedule`); a
   *  successful run is read from `snapshot` below instead. */
  scanJob: ScheduledJob | undefined;
  title: string;
  desc: string;
  scheduleText: string;
  onEdit: () => void;
  canEdit: boolean;
}) {
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
    unknownSize: number;
  } | null>(null);

  // The totals from the scan that just ended, captured on the running -> not-running edge so
  // the new ones have something to be read against. The refresh that edge also triggers is
  // NOT here: it belongs on the shell, which a scan cannot unmount (`useScanSettled`), or a
  // scan started from any other screen finished with nothing refreshing the page in front of
  // the operator. This reads the old snapshot straight out of the cache, which an
  // invalidation elsewhere in the same tick leaves in place while its refetch is in flight.
  useEffect(() => {
    if (wasScanning.current && !scanning) {
      const previous = queryClient.getQueryData<Snapshot>(["snapshot"]);
      setBefore(
        previous
          ? {
              id: previous.id,
              condemned: previous.condemned,
              freeable: previous.reclaimable_bytes,
              unknownSize: previous.unknown_size_items,
            }
          : null,
      );
    }
    if (!wasScanning.current && scanning) setBefore(null);
    wasScanning.current = scanning;
  }, [scanning, queryClient]);

  // A mutation, not a fire-and-forget async onClick: a start that fails must say so,
  // or the button appears to do nothing at all.
  const start = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => {
      // Seed the cache with the returned status so polling begins immediately.
      queryClient.setQueryData(["scanStatus"], started);
      // The press disables its own button and swaps the schedule line for a progress bar, both
      // of them visual. Focus is at `<body>` from the disable, the bar announces nothing on its
      // own, and the next thing said is the finish -- which for a library scan is minutes away
      // (#177). Said at `onSuccess`, so it reports a scan that actually started rather than one
      // still being asked for (rule 85); a start that fails speaks through the `Notice` below.
      announce(`${SCANNING_LABEL}. ${KEEPS_RUNNING}`);
    },
  });

  // The server hands us a monotonic 0-100 that rises smoothly across the scan's phases.
  // (The old done/total math read 100% before any work began, because the early phases
  // report total=0, then jumped as the denominator changed meaning between phases.)
  const pct = status ? status.percent : null;

  // Only against a *different* snapshot: the first scan ever has nothing to compare to,
  // and a scan that wrote no new snapshot must not report a change of zero.
  const delta =
    before && snapshot && snapshot.id !== before.id ? scanDelta(before, snapshot) : null;

  // The scan's live phase, shown in the shared status slot while it runs.
  const runLabel = status
    ? `${phaseLabel(status.phase)}${status.detail ? `, ${status.detail}` : ""}${
        pct !== null ? `, ${pct}%` : ""
      }`
    : "Scanning…";
  // A finished scan confirms itself in the same slot, then settles to the last-run line. A
  // scan that reported a problem gets no flash: the error notice below carries the detail,
  // and a green "done" chip beside it would contradict.
  const scanFlash = useJobFlash(
    scanning,
    status?.error ? null : { ok: true, text: "Queue refreshed" },
  );

  // A scheduled scan that crashed outright writes no snapshot, so it is recorded separately
  // (job id "scheduled_scan", see get_schedule) instead of being silently invisible here.
  // Prefer that failure only while it is actually newer than the snapshot on hand -- once a
  // later scan succeeds, its fresh snapshot wins again with no need to clear the record.
  const scanFailedAt =
    scanJob && scanJob.last_ok === false && scanJob.last_run_at ? scanJob.last_run_at : null;
  const failureIsCurrent =
    scanFailedAt !== null &&
    (!snapshot || new Date(scanFailedAt).getTime() > new Date(snapshot.created_at).getTime());
  // A completed-but-degraded scan produced a result, just an incomplete one -- the warning
  // notice below explains that. It is not a "failed" run, so it must not paint the dot red;
  // only a scan attempt that crashed outright (above) is a real failure here.
  const lastRunAt = failureIsCurrent ? scanFailedAt : (snapshot?.created_at ?? null);
  const lastOk = failureIsCurrent ? false : snapshot ? true : null;

  return (
    <div className="jobrow">
      <div className="jobrow-main">
        <div className="jobrow-title">{title}</div>
        <div className="jobrow-desc">{desc}</div>

        <JobStatus
          running={scanning}
          runningLabel={runLabel}
          lastRunAt={lastRunAt}
          lastOk={lastOk}
          lastResult={failureIsCurrent ? (scanJob?.last_result ?? null) : null}
          flash={scanFlash}
        />

        {snapshot && !scanning && (
          <div className="jobrow-meta">
            {count(snapshot.item_count)} items, <strong>{count(snapshot.condemned)}</strong> would
            be removed, freeing{" "}
            <strong>{totalBytes(snapshot.reclaimable_bytes, snapshot.unknown_size_items)}</strong>
          </div>
        )}
        {delta && !scanning && <div className="jobrow-meta">{delta}</div>}
        {!snapshot && !scanning && (
          <div className="jobrow-meta">A scan only reads. It cannot delete.</div>
        )}

        {scanning ? (
          <>
            {/* A bare div with an inline width is a picture of a number and nothing else.
                `ScanLine` was the correct twin in the tree and this was not swept
                with it (#177, rule 72); the deletion path's pair went the same way in #170. */}
            <div
              className="bar"
              role="progressbar"
              aria-label={SCANNING_LABEL}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={pct ?? 0}
            >
              <div className="bar-fill" style={{ width: `${pct ?? 0}%` }} />
            </div>
            <div className="jobrow-sched">{KEEPS_RUNNING}</div>
          </>
        ) : (
          <div className="jobrow-sched">{scheduleText}</div>
        )}

        {start.error && (
          <Notice tone="error" inline>
            The scan didn't start: {start.error.message}
          </Notice>
        )}
        {/* `standing`, unlike `start.error` directly above it, which answers this bar's own
            Start press and stays an alert. A scan runs from the scheduler and from other
            devices, and the shell holds a second observer on `["scanStatus"]` that polls every
            15s while this row is idle (`App.tsx`), so a scheduled scan crashing writes this into
            the shared cache and paints it under a page nobody touched. */}
        {status?.error && (
          <Notice tone="error" inline standing>
            The scan hit a problem: {status.error}
          </Notice>
        )}

        {/* `standing`, same route one step later: `useScanSettled` invalidates `["snapshot"]` on
            the running-to-idle edge it sees through that same 15s poll, so a scheduled scan
            finishing incomplete draws this with no press either. */}
        {snapshot?.degraded && (
          <Notice tone="warn" standing>
            <strong>This scan came back incomplete.</strong> {snapshot.degraded_reason}
          </Notice>
        )}
      </div>

      <div className="jobrow-actions">
        <span className="slot-edit">
          {/* Named for the row it belongs to: this Edit sits on the Jobs page beside one Edit per
              upkeep job, and by its own text they are indistinguishable (rule 72). */}
          <button
            className="ghost"
            aria-label="Edit the library scan schedule"
            onClick={onEdit}
            disabled={!canEdit}
          >
            Edit
          </button>
        </span>
        <span className="slot-act">
          <button
            className="primary"
            onClick={() => start.mutate()}
            disabled={scanning || start.isPending}
          >
            {scanning ? "Scanning…" : start.isPending ? "Starting…" : "Scan library"}
          </button>
        </span>
      </div>
    </div>
  );
}
