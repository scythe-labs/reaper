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

import { useIsFetching, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { announce } from "../announce";
import { api, type ScheduledJob, type Snapshot } from "../api";
import { bytes, count, totalBytes } from "../format";
import { useScanStatus } from "../useScanStatus";
import { JobStatus, useJobFlash } from "./JobStatus";
import { Notice } from "./Notice";
import { ProgressBar } from "./ProgressBar";
import { SCANNING_LABEL } from "./ScanLine";

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

  const status = useScanStatus();

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
  /** Whether the `["snapshot"]` refetch that the finish edge triggers has been seen to
   *  finish, however it finished. Declared beside `before` because it bounds the same
   *  window; read where `supersededSnapshot` is computed. */
  const [snapshotReadSettled, setSnapshotReadSettled] = useState(false);

  // The totals from the scan that just ended, captured on the running -> not-running edge so
  // the new ones have something to be read against. The refresh that edge also triggers is
  // NOT here: it belongs on the shell, which a scan cannot unmount (`useScanSettled`), or a
  // scan started from any other screen finished with nothing refreshing the page in front of
  // the operator. This reads the old snapshot straight out of the cache, which an
  // invalidation elsewhere in the same tick leaves in place while its refetch is in flight.
  useEffect(() => {
    if (wasScanning.current && !scanning) {
      // A fresh window opens here, so the "the read has settled" latch that bounds it below
      // starts closed: the refetch this edge triggers has not been seen to finish yet.
      setSnapshotReadSettled(false);
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

  // The snapshot in hand is not the last scan's, so nothing on this row may speak for it. Two
  // windows: a scan is running, and the moment after one ends, where `useScanSettled`'s refetch
  // of `["snapshot"]` is still out and `before` still holds the id that is on screen -- the same
  // fact `delta` waits on above. A run that ended in an error wrote no snapshot, so the one in
  // hand is still the newest and still speaks for itself.
  //
  // `snapshotReadSettled` is what BOUNDS the second window. `before` is cleared only when the
  // NEXT scan starts, so on id-equality alone the window never closed if the refetch never
  // landed with a new id -- and `JobsPanel`'s query is `retry: false`, so one dropped request
  // does exactly that. The row then went on rendering that snapshot's item and condemned
  // counts while withholding its "incomplete" verdict, for the life of the mount, which is
  // the reassuring direction to fail in.
  //
  // Keyed on the refetch COMPLETING rather than on one being in flight: between the status
  // going idle and `useScanSettled`'s invalidation actually starting a fetch, nothing is in
  // flight, and treating that as settled would paint the previous scan's verdict for a tick
  // as though it were this one's. So the window holds until a fetch has been seen to finish,
  // whether it finished with a new snapshot or with an error.
  const refetchingSnapshot = useIsFetching({ queryKey: ["snapshot"] }) > 0;
  const wasRefetchingSnapshot = useRef(false);
  useEffect(() => {
    if (wasRefetchingSnapshot.current && !refetchingSnapshot) setSnapshotReadSettled(true);
    wasRefetchingSnapshot.current = refetchingSnapshot;
  }, [refetchingSnapshot]);

  const supersededSnapshot =
    scanning ||
    (before !== null && !status?.error && snapshot?.id === before.id && !snapshotReadSettled);

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

  // A degraded scan is not an error, so it takes the success flash above -- and that flash
  // fires on the finish edge, where the snapshot in hand is still the PREVIOUS scan's, so it
  // cannot speak to how this one came back. The notice that can is `standing`, which announces
  // nothing by design, so "Queue refreshed" was the only thing a screen-reader user was told
  // about a scan Reaper will not act on (rules 85, 72). Said here instead: when the fresh
  // snapshot has landed, the row is speaking for it again, and the verdict is finally known.
  //
  // Once per snapshot, and only after a finish this mount watched (`before`), so navigating
  // onto an already-degraded snapshot announces nothing -- the same restraint `useJobFlash`
  // keeps about a job that finished before the page loaded.
  const announcedIncompleteFor = useRef<number | null>(null);
  useEffect(() => {
    if (supersededSnapshot || before === null || !snapshot?.degraded) return;
    if (announcedIncompleteFor.current === snapshot.id) return;
    announcedIncompleteFor.current = snapshot.id;
    announce("That scan came back incomplete. Reaper won't act on it.");
  }, [supersededSnapshot, before, snapshot?.degraded, snapshot?.id]);

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
            <ProgressBar label={SCANNING_LABEL} percent={pct ?? 0} />
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
            finishing incomplete draws this with no press either.

            Held while that snapshot is superseded, which is this notice specifically and not the
            two others carrying the same claim (rule 72). It renders inside the running scan's own
            row, under its progress bar, where "This scan" reads as the one in flight -- and the
            operator starting a rescan has already answered it. The other two sit on pages with no
            scan in view and stay: `ReapPlan`'s is why Build is disabled over that snapshot, and
            `ScanFreshness.tsx`'s freshness line is the age of the queue rendered below it, both still true
            for as long as that snapshot is the one on hand. */}
        {snapshot?.degraded && !supersededSnapshot && (
          <Notice tone="warn" standing>
            {/* What it means before why it happened. The consequence clause was dropped from
                here and from the Review page's line, leaving `ReapPlan` the only one of the
                three still saying it, and whether the operator saw it at all then depended on
                which source had failed: only `library_index`'s reasons carry "Nothing may be
                deleted from this scan" in their own text (rules 21, 72, 144). */}
            <strong>This scan came back incomplete.</strong> Reaper won&apos;t act on it.{" "}
            {snapshot.degraded_reason}
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
