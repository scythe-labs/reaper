// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The scan, its progress, and the state of the snapshot it produced.
//
// The scan runs as a background job on the server (see api/scan.py), detached from the
// request that starts it, so closing the tab or switching screens does not stop it. This
// component starts a scan and then polls its progress, which means it also picks up a scan
// that is already running when the page loads.
//
// A scan only reads: it reads from the *arr apps and Tautulli, scores, and writes rows to
// Reaper's own database. GuardedTransport would refuse a mutating call even if one were tried.

import { useIsFetching, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { announce } from "../announce";
import { api, type ScheduledJob, type Snapshot } from "../api";
import { DegradedDocLink } from "../docs/DocLink";
import { describeError } from "../errors";
import { bytes, count, totalBytes } from "../format";
import i18next from "../i18n";
import { useScanStatus } from "../useScanStatus";
import { composeError, composeIn } from "../why";
import { JobStatus, jobResultText, useJobFlash } from "./JobStatus";
import { Notice } from "./Notice";
import { ProgressBar } from "./ProgressBar";
import { scanningLabel } from "./ScanLine";

/** Friendly names for the scan's internal phases, so the status line reads in plain words. A
 *  plain function rather than a frozen table: it reads the catalog on every call, and each id
 *  gets its own literal `t()` call because a computed key would be unreadable to the
 *  missing-key gate. `JobsPanel`'s own `jobMeta` takes the same shape. Exported because the
 *  first-run wizard shows the same progress line, so one function keeps the two surfaces
 *  from drifting into raw phase ids. */
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
 *  change with nothing to compare them against. Null when nothing moved is deliberate: a
 *  line saying "no change" every time would be noise.
 *
 *  Both byte figures cover only the items that had a measured size, and the two scans can
 *  have different unmeasured populations. An item measured last time but not this time
 *  drops out of the total on its own, which reads as progress that didn't happen. So when
 *  either scan carries unknown-size items, the difference is qualified rather than dropped.
 *  A line the operator already trusts must never quietly change what it means. */
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
      unknowns > 0
        ? i18next.t("shell.scanBar.deltaUnknownQualifier", { n: unknowns, count: unknowns })
        : "";
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

/** The library-scan row on the Jobs page: the primary run action, its last-scan stats and
 *  live progress, and an Edit control that opens the scan's schedule. It owns the whole scan
 *  lifecycle (start, poll, delta, degraded state). The title, description and schedule line
 *  are handed in as props, so the copy for every job lives in one place. */
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
  /** The scan's own schedule entry. Carries `last_run_at`, `last_ok`, and
   *  `last_result_reason` only for a scheduled scan that crashed outright and wrote no
   *  snapshot (see `get_schedule`). A successful run is read from `snapshot` below instead. */
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
   *  window. Read where `supersededSnapshot` is computed. */
  const [snapshotReadSettled, setSnapshotReadSettled] = useState(false);

  // The totals from the scan that just ended, captured on the running-to-not-running edge so
  // the new ones have something to be read against. The refresh that edge also triggers
  // belongs on the shell instead, because a scan cannot unmount the shell
  // (`useScanSettled`). A scan started from any other screen still needs the page in front
  // of the operator to refresh. This reads the old snapshot straight out of the cache: an invalidation
  // elsewhere in the same tick leaves that cached value in place while its refetch is still
  // in flight.
  useEffect(() => {
    if (wasScanning.current && !scanning) {
      // A fresh window opens here, so the "read settled" latch that bounds it below starts
      // closed. The refetch this edge triggers has not been seen to finish yet.
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

  // A mutation, not a fire-and-forget async onClick. A start that fails must say so, or the
  // button appears to do nothing at all.
  const start = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => {
      // Seed the cache with the returned status so polling begins immediately.
      queryClient.setQueryData(["scanStatus"], started);
      // The press disables its own button and swaps the schedule line for a progress bar,
      // both purely visual changes. Focus lands on `<body>` from the disable, the progress
      // bar announces nothing on its own, and without this the next thing a screen reader
      // says would be the finish, minutes away for a library scan. This announces at
      // `onSuccess`, so it reports a scan that actually started rather than one still being
      // asked for. A start that fails speaks through the `Notice` below instead.
      announce(`${scanningLabel()}. ${t("shell.scanBar.keepsRunning")}`);
    },
  });

  // The server sends a monotonic 0-100 value that rises smoothly across the scan's phases.
  // A done/total ratio does not work here: the early phases report total=0, so that ratio
  // reads 100% before any work begins, and it jumps whenever the denominator's meaning
  // changes between phases.
  const pct = status ? status.percent : null;

  // Only compares against a different snapshot. The first scan ever has nothing to compare
  // to, and a scan that wrote no new snapshot must not report a change of zero.
  const delta =
    before && snapshot && snapshot.id !== before.id ? scanDelta(before, snapshot) : null;

  // The snapshot in hand is not necessarily the last scan's, so nothing on this row may
  // speak for it during two windows: while a scan is running, and in the moment right after
  // one ends, while `useScanSettled`'s refetch of `["snapshot"]` is still out and `before`
  // still holds the id on screen (the same fact `delta` waits on above). A run that ended in
  // an error wrote no snapshot, so the one in hand is still the newest and still speaks for
  // itself.
  //
  // `snapshotReadSettled` bounds the second window. `before` only clears when the next scan
  // starts, so id-equality alone is not enough: if the refetch never lands with a new id, the
  // window would never close on its own. `JobsPanel`'s query is `retry: false`, so one
  // dropped request does exactly that, and without `snapshotReadSettled` the row would keep
  // rendering that snapshot's item and condemned counts while withholding its "incomplete"
  // verdict for the rest of the mount. That is the safe direction to fail in, but
  // `snapshotReadSettled` closes the window properly instead of relying on it.
  //
  // This is keyed on the refetch finishing, not on one being in flight. Between the status
  // going idle and `useScanSettled`'s invalidation actually starting a fetch, nothing is in
  // flight, and treating that gap as settled would paint the previous scan's verdict onto
  // this one for a tick. So the window holds until a fetch has been seen to finish, whether
  // it finished with a new snapshot or with an error.
  const refetchingSnapshot = useIsFetching({ queryKey: ["snapshot"] }) > 0;
  const wasRefetchingSnapshot = useRef(false);
  useEffect(() => {
    if (wasRefetchingSnapshot.current && !refetchingSnapshot) setSnapshotReadSettled(true);
    wasRefetchingSnapshot.current = refetchingSnapshot;
  }, [refetchingSnapshot]);

  const supersededSnapshot =
    scanning ||
    (before !== null &&
      !status?.error_reason &&
      snapshot?.id === before.id &&
      !snapshotReadSettled);

  // The scan's live phase, shown in the shared status slot while it runs.
  const stepText = status?.detail_reason
    ? composeIn("shell.scanBar.step", status.detail_reason)
    : "";
  const runLabel = status
    ? `${phaseLabel(status.phase)}${stepText ? `, ${stepText}` : ""}${
        pct !== null ? `, ${pct}%` : ""
      }`
    : t("common.scanning");
  // A finished scan confirms itself in the same slot, then settles to the last-run line. A
  // scan that reported a problem gets no flash. The error notice below carries the detail,
  // and a green "done" chip beside it would contradict it.
  const scanFlash = useJobFlash(
    scanning,
    status?.error_reason ? null : { ok: true, text: t("shell.scanBar.queueRefreshed") },
  );

  // A degraded scan is not an error, so it takes the success flash above. That flash fires
  // on the finish edge, while the snapshot in hand is still the previous scan's, so it
  // cannot speak to how this one came back. The notice that can is `standing`, which
  // announces nothing on its own: without this effect, "Queue refreshed" would be the only
  // thing a screen reader user hears about a scan Reaper will not act on. This announces
  // instead, once the fresh snapshot has landed and the row is speaking for it again, so the
  // verdict is finally known.
  //
  // This fires once per snapshot, and only after a finish this mount actually watched
  // (`before`). Navigating onto an already-degraded snapshot announces nothing, the same
  // restraint `useJobFlash` uses for a job that finished before the page loaded.
  const announcedIncompleteFor = useRef<number | null>(null);
  useEffect(() => {
    if (supersededSnapshot || before === null || !snapshot?.degraded) return;
    if (announcedIncompleteFor.current === snapshot.id) return;
    announcedIncompleteFor.current = snapshot.id;
    announce(t("shell.scanBar.incompleteAnnounce"));
  }, [supersededSnapshot, before, snapshot?.degraded, snapshot?.id, t]);

  // A scheduled scan that crashed outright writes no snapshot, so it is recorded separately
  // instead (job id "scheduled_scan", see get_schedule). This failure is only shown while it
  // is actually newer than the snapshot on hand. Once a later scan succeeds, its fresh
  // snapshot wins again with no need to clear the record.
  const scanFailedAt =
    scanJob && scanJob.last_ok === false && scanJob.last_run_at ? scanJob.last_run_at : null;
  const failureIsCurrent =
    scanFailedAt !== null &&
    (!snapshot || new Date(scanFailedAt).getTime() > new Date(snapshot.created_at).getTime());
  // A completed-but-degraded scan produced a result, just an incomplete one. The warning
  // notice below explains that. It is not a "failed" run, so it must not paint the dot red.
  // Only a scan attempt that crashed outright (above) counts as a real failure here.
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
          lastResult={jobResultText(
            failureIsCurrent ? (scanJob?.last_result_reason ?? null) : null,
          )}
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
            <ProgressBar label={scanningLabel()} percent={pct ?? 0} />
            <div className="jobrow-sched">{t("shell.scanBar.keepsRunning")}</div>
          </>
        ) : (
          <div className="jobrow-sched">{scheduleText}</div>
        )}

        {start.error && (
          <Notice tone="error" inline>
            {t("common.scanStartFailed", { message: describeError(start.error) })}
          </Notice>
        )}
        {/* `standing`, unlike `start.error` directly above it, which answers this bar's own
            Start press and stays an alert. A scan can also run from the scheduler or from
            another device: the shell polls `["scanStatus"]` every 15s while this row is idle
            (`App.tsx`), so a scheduled scan crashing writes this notice into the shared cache
            and paints it under a page nobody touched. */}
        {status?.error_reason && (
          <Notice tone="error" inline standing>
            {t("shell.scanBar.scanProblem", { error: composeError(status.error_reason) })}
          </Notice>
        )}

        {/* `standing`, same route one step later: `useScanSettled` invalidates `["snapshot"]`
            on the running-to-idle edge, seen through that same 15s poll, so a scheduled scan
            finishing incomplete draws this with no press either.

            Held while this snapshot is superseded, specifically. Two other notices make the
            same claim elsewhere and stay up longer: `ReapPlan`'s is why Build is disabled over
            that snapshot, and `ScanFreshness.tsx`'s freshness line is the age of the queue
            rendered below it, both true for as long as that snapshot is the one on hand. This
            one renders inside the running scan's own row, under its progress bar, where "This
            scan" reads as the one in flight, and the operator starting a rescan has already
            answered it. */}
        {snapshot?.degraded && !supersededSnapshot && (
          <Notice tone="warn" standing as="div" className="notice-doc">
            {/* Order matters: what the degradation means comes before why it happened. This
                notice states no separate consequence clause. Only `library_index`'s reasons
                carry "Nothing may be deleted from this scan" in their own text, so that is
                the only case where the operator sees it stated here at all. */}
            <span>
              <Trans
                i18nKey="shell.scanBar.degradedNotice"
                values={{ reason: snapshot.degraded_reason }}
                components={{ strong: <strong /> }}
              />
            </span>
            {/* This renders nothing when the degradation names no doc page, which is true for
                most of them, so the notice looks the same either way. `ReapPlan` carries the
                same pair. */}
            <DegradedDocLink doc={snapshot.degraded_doc} />
          </Notice>
        )}
      </div>

      <div className="jobrow-actions">
        <span className="slot-edit">
          {/* Named for the row it belongs to: this Edit sits on the Jobs page beside one Edit
              per upkeep job, and by its own text alone they are indistinguishable. */}
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
                : t("common.scanNow")}
          </button>
        </span>
      </div>
    </div>
  );
}
