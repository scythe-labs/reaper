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
import { Trans, useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api, type ScheduledJob, type Snapshot } from "../api";
import { DegradedDocLink } from "../docs/DocLink";
import { bytes, count, totalBytes } from "../format";
import i18next from "../i18n";
import { useScanStatus } from "../useScanStatus";
import { JobStatus, useJobFlash } from "./JobStatus";
import { Notice } from "./Notice";
import { ProgressBar } from "./ProgressBar";
import { SCANNING_LABEL } from "./ScanLine";

/** Friendly names for the scan's internal phases, so the status line reads in English. A
 *  plain function rather than a frozen table, read from the catalog on every call (each id
 *  gets its own literal `t()` call, since a computed key is unreadable to the missing-key
 *  gate) -- the same shape `JobsPanel`'s own `jobMeta` takes. Exported because the first-run
 *  wizard shows the same progress line; one function keeps the two surfaces from drifting
 *  into raw phase ids. */
export function phaseLabel(phase: string): string {
  const labels: Record<string, string> = {
    starting: i18next.t("shell.scanBar.phase.starting"),
    history: i18next.t("shell.scanBar.phase.history"),
    lists: i18next.t("shell.scanBar.phase.lists"),
    gathering: i18next.t("shell.scanBar.phase.gathering"),
    scoring: i18next.t("shell.scanBar.phase.scoring"),
    done: i18next.t("shell.scanBar.phase.done"),
    shelves: i18next.t("shell.scanBar.phase.shelves"),
    complete: i18next.t("shell.scanBar.phase.complete"),
  };
  return labels[phase] ?? phase;
}

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
    parts.push(
      i18next.t("shell.scanBar.deltaItems", {
        n: count(Math.abs(items)),
        dir: items > 0 ? "up" : "down",
      }),
    );
  }
  if (size !== 0) {
    const qualifier =
      unknowns > 0 ? i18next.t("shell.scanBar.deltaUnknownQualifier", { n: unknowns }) : "";
    parts.push(
      i18next.t("shell.scanBar.deltaSize", {
        n: bytes(Math.abs(size)),
        dir: size > 0 ? "up" : "down",
        qualifier,
      }),
    );
  }
  return i18next.t("shell.scanBar.deltaSummary", { parts: parts.join(", ") });
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
  const { t } = useTranslation();
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
      announce(`${SCANNING_LABEL}. ${t("shell.scanBar.keepsRunning")}`);
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
    : t("common.scanning");
  // A finished scan confirms itself in the same slot, then settles to the last-run line. A
  // scan that reported a problem gets no flash: the error notice below carries the detail,
  // and a green "done" chip beside it would contradict.
  const scanFlash = useJobFlash(
    scanning,
    status?.error ? null : { ok: true, text: t("shell.scanBar.queueRefreshed") },
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
    announce(t("shell.scanBar.incompleteAnnounce"));
  }, [supersededSnapshot, before, snapshot?.degraded, snapshot?.id, t]);

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
            <Trans
              i18nKey="shell.scanBar.snapshotSummary"
              values={{
                itemCount: count(snapshot.item_count),
                condemnedCount: count(snapshot.condemned),
                freedAmount: totalBytes(snapshot.reclaimable_bytes, snapshot.unknown_size_items),
              }}
              components={{ condemned: <strong />, freed: <strong /> }}
            />
          </div>
        )}
        {delta && !scanning && <div className="jobrow-meta">{delta}</div>}
        {!snapshot && !scanning && (
          <div className="jobrow-meta">{t("shell.scanBar.scanOnlyReads")}</div>
        )}

        {scanning ? (
          <>
            <ProgressBar label={SCANNING_LABEL} percent={pct ?? 0} />
            <div className="jobrow-sched">{t("shell.scanBar.keepsRunning")}</div>
          </>
        ) : (
          <div className="jobrow-sched">{scheduleText}</div>
        )}

        {start.error && (
          <Notice tone="error" inline>
            {t("common.scanStartFailed", { message: start.error.message })}
          </Notice>
        )}
        {/* `standing`, unlike `start.error` directly above it, which answers this bar's own
            Start press and stays an alert. A scan runs from the scheduler and from other
            devices, and the shell holds a second observer on `["scanStatus"]` that polls every
            15s while this row is idle (`App.tsx`), so a scheduled scan crashing writes this into
            the shared cache and paints it under a page nobody touched. */}
        {status?.error && (
          <Notice tone="error" inline standing>
            {t("shell.scanBar.scanProblem", { error: status.error })}
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
          <Notice tone="warn" standing as="div" className="notice-doc">
            {/* What it means before why it happened. The consequence clause was dropped from
                here and from the Review page's line, leaving `ReapPlan` the only one of the
                three still saying it, and whether the operator saw it at all then depended on
                which source had failed: only `library_index`'s reasons carry "Nothing may be
                deleted from this scan" in their own text (rules 21, 72, 144). */}
            <span>
              <Trans
                i18nKey="shell.scanBar.degradedNotice"
                values={{ reason: snapshot.degraded_reason }}
                components={{ strong: <strong /> }}
              />
            </span>
            {/* Nothing renders for a degradation with no page, which is most of them, so this
                notice looks exactly as it did unless the scan named one. `ReapPlan` carries the
                same pair (rule 72). */}
            <DegradedDocLink doc={snapshot.degraded_doc} />
          </Notice>
        )}
      </div>

      <div className="jobrow-actions">
        <span className="slot-edit">
          {/* Named for the row it belongs to: this Edit sits on the Jobs page beside one Edit per
              upkeep job, and by its own text they are indistinguishable (rule 72). */}
          <button
            className="ghost"
            aria-label={t("shell.scanBar.editScheduleLabel")}
            onClick={onEdit}
            disabled={!canEdit}
          >
            {t("common.edit")}
          </button>
        </span>
        <span className="slot-act">
          <button
            className="primary"
            onClick={() => start.mutate()}
            disabled={scanning || start.isPending}
          >
            {scanning
              ? t("common.scanning")
              : start.isPending
                ? t("common.starting")
                : t("shell.scanBar.scanLibrary")}
          </button>
        </span>
      </div>
    </div>
  );
}
