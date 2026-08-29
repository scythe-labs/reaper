import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { announce } from "../announce";
import { api } from "../api";
import { describeError } from "../errors";
import { bytes, count, souls } from "../format";
import { composeError } from "../why";
import { Notice } from "./Notice";
import { ackRun, useAckedRun } from "./runAck";

/** The app-wide reap bar: shown on every screen of the app while a reap runs, so its count and
 *  its Stop are reachable from wherever the operator is. The reap sheet closes the moment a run
 *  starts, so this bar is the run's presence off the Reap tab. A reap runs detached from the
 *  request that started it, so this bar (and Stop) survive navigating away and a tab reload: it
 *  re-attaches by polling the shared status. Its View button opens the Reap tab, the live
 *  dashboard and, once the run ends, the result. Not a safety surface (the always-on one is
 *  SafetyBanner), so it shows nothing when idle. Stop is graceful: the run halts after the item
 *  in flight and still tidies Plex, and deletion stays armed.
 *
 *  "Every screen" means every screen of the app, and the setup wizard is not one:
 *  `App.tsx`'s `Authed` returns it *instead of* `Dashboard`, so nothing under `Dashboard`
 *  mounts while it is up, this bar included. This bar's Stop is still reachable during a run
 *  that triggers the wizard: removing the Tautulli or the last *arr invalidates `["setup"]`,
 *  `scan_ready` goes false, and the wizard takes the page with no reload, landing on the
 *  Connect step, which has Back and Skip, so Stop is two presses away rather than lost.
 *
 *  It mounts, runs, and reaches its end, "Reap failed." included, and announces mount and end
 *  out loud. Its fill also carries `role="progressbar"`, the way `ScanLine`
 *  (components/ScanLine.tsx) does. The running ticks are deliberately not announced, by this bar
 *  or the Reap tab: the visual progressbar carries them, and a run of hundreds polling every
 *  second would otherwise hold the app's one polite region for the whole run. The end is the
 *  moment worth speaking, and it is spoken once, here.
 *
 *  Tested directly rather than through `App`, as `WhyPanelFallback` is: what it owes an
 *  operator is a property of this component, and reaching it through the whole authed `App`
 *  tree would test the login gate instead. This component is exported for its tests; the
 *  export carries no other meaning here, since every file in this codebase exports its
 *  component. */
export function ReapBar({
  onGoToReap,
  suppressed = false,
}: {
  onGoToReap: () => void;
  /** True while the operator is on the Reap tab, which is the run's own live dashboard: the
   *  bar's count, View, and Stop would only duplicate what is already on that page, so it
   *  renders nothing there. It stays MOUNTED, not unmounted: the post-run cache invalidation and
   *  the end announcement below both live here (rule 79) and must still fire when a run ends
   *  while the operator is watching it on the Reap tab. */
  suppressed?: boolean;
}) {
  const queryClient = useQueryClient();
  const { t } = useTranslation();
  // Shared with the Reap page's Done button and persisted, so dismissing a result on either
  // surface hides both, and a refresh does not bring it back.
  const dismissed = useAckedRun();
  // Idle still polls, slowly. A reap can be started from a phone or a second tab, and this
  // bar carries the only Stop on most screens: going silent when nothing is running here
  // would leave an open tab dark through someone else's deletion (the scan line idle-polls
  // at 15s for the same reason).
  const { data: status } = useQuery({
    queryKey: ["reapStatus"],
    queryFn: api.reapStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : 15000),
  });
  const stop = useMutation({
    mutationFn: (id: number) => api.stopRun(id),
    onSuccess: (s) => queryClient.setQueryData(["reapStatus"], s),
  });

  // A finished reap invalidates half the app: the queue lists titles that are gone, the
  // ledger promises to remove them, the snapshot's reclaimable figure counts them. That
  // refresh belongs HERE, on the component a reap does not unmount: the confirmation sheet
  // is explicitly designed to be closed mid-run, and everything it invalidated went with it.
  // Fired once, on the running-to-ended edge of a run this mount actually saw running, so a
  // page opened after the fact does not re-invalidate what it just fetched.
  const ranRef = useRef<number | null>(null);
  const settledRef = useRef<number | null>(null);
  useEffect(() => {
    if (!status || status.run_id == null) return;
    if (status.running) {
      ranRef.current = status.run_id;
      return;
    }
    if (ranRef.current !== status.run_id || settledRef.current === status.run_id) return;
    settledRef.current = status.run_id;
    // Say how it ended. The same edge, for the same reason: a run that finished before this tab
    // opened must not be announced as news. The sentence carries the outcome first and the
    // figures after, matching the bar's own text below, and it is said here rather than in the
    // reap sheet because closing the sheet does not take this bar with it. The sheet is meant
    // to be closed mid-run, and an operator who closed it would otherwise hear nothing at all
    // about a deletion finishing.
    announce(
      t("reapConfirm.bar.announceEnded", {
        phase: status.phase,
        souls: souls(status.deleted_items),
        bytes: bytes(status.deleted_bytes),
      }),
    );
    // ["run"] as well as ["runs"]: the plan surface reads one run by id, and that key does
    // not match the list's.
    for (const key of [
      ["runs"],
      ["run"],
      ["candidates"],
      ["reap-breakdown"],
      ["snapshot"],
      ["fairness"],
    ]) {
      void queryClient.invalidateQueries({ queryKey: key });
    }
  }, [status, queryClient, t]);

  // On the Reap tab the page itself is the live dashboard, so the bar draws nothing. This sits
  // AFTER the invalidation effect on purpose: the component stays mounted and that effect still
  // fires when the run ends here.
  if (suppressed) return null;

  if (!status || status.run_id == null) return null;
  const runId = status.run_id;
  const running = status.running;
  // Every terminal phase counts as ended, including "error", so a reap that crashed after
  // removing files still surfaces here (the one always-visible fallback) instead of vanishing.
  const ended =
    !running &&
    (status.phase === "complete" || status.phase === "aborted" || status.phase === "error");
  if (!running && !(ended && runId !== dismissed)) return null;

  if (ended) {
    const errored = status.phase === "error";
    return (
      <div className={errored ? "reap-bar errored" : "reap-bar done"}>
        <span className="reap-bar-text">
          <span className="reap-bar-lead">
            <span className="banner-dot" aria-hidden="true" />
            <b>{t("reapConfirm.bar.endedLabel", { phase: status.phase })}</b>
          </span>
          <span className="reap-bar-sub">
            {t("reapConfirm.bar.removedFreed", {
              souls: souls(status.deleted_items),
              bytes: bytes(status.deleted_bytes),
            })}
            {errored &&
              status.error_reason &&
              t("reapConfirm.bar.errorSuffix", { error: composeError(status.error_reason) })}
          </span>
        </span>
        <span className="reap-bar-actions">
          {/* No report to open for an errored run: the executor raised before the run's own row
              left PLANNED, so the Reap tab has no result card for it, and the failure is already
              on this bar. A complete or aborted run has a result there. */}
          {!errored && (
            <button className="link" onClick={onGoToReap}>
              {t("reapConfirm.bar.viewReport")}
            </button>
          )}
          <button className="sm" onClick={() => ackRun(runId)}>
            {t("reapConfirm.bar.dismiss")}
          </button>
        </span>
      </div>
    );
  }

  const pct = status.total > 0 ? Math.round((status.done / status.total) * 100) : 0;
  return (
    <div className="reap-bar">
      {/* The role goes on the TEXT, never on the bar. `progressbar` carries ARIA's Children
          Presentational: True, so a role on the container would prune everything inside it,
          including View, Stop, and the `role="alert"` that reports a failed Stop: a reader
          watching a live deletion would hear the percentage with no way to halt it. `CardOpen`
          undoes the same pruning on the queue's four cards. The three sibling bars and
          `ScanLine` all carry the role the same way, on an element holding only a fill.
          The text is the right anchor here: it is the visible readout, it is never empty, and
          it holds no control, where the fill is 0px wide at 0% and can drop out of the tree.
          `aria-valuetext` says what a person would say, so nobody is left reading out "62". */}
      <span
        className="reap-bar-text"
        role="progressbar"
        aria-label={t("reapConfirm.progress.ariaLabel")}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={t("reapConfirm.progress.valueText", {
          pct,
          done: count(status.done),
          total: count(status.total),
        })}
      >
        <span className="reap-bar-lead">
          <span className="banner-dot" aria-hidden="true" />
          {status.stopping ? (
            <b>{t("reapConfirm.stoppingLabel")}</b>
          ) : (
            <b>
              {t("reapConfirm.bar.reapingCount", {
                done: count(status.done),
                total: count(status.total),
              })}
            </b>
          )}
        </span>
        {/* A glance percent, shown on a phone only, where the freed size drops to its own line
            and would otherwise leave the top line half empty. Decorative: the progressbar aria
            above already voices the same figure, so this is hidden from a reader. */}
        {!status.stopping && <span className="reap-bar-pct" aria-hidden="true">{`${pct}%`}</span>}
      </span>
      {/* The space freed so far. Inline after the count on desktop, its own line on a phone. */}
      {!status.stopping && (
        <span className="reap-bar-sub">
          {t("reapConfirm.bar.freed", { bytes: bytes(status.deleted_bytes) })}
        </span>
      )}
      {/* The visible progress track. Decorative: the progressbar role and its aria live on the
          text above (which never prunes to nothing), so this stays aria-hidden. Inline on
          desktop between the count and the actions, full width on a phone. It replaces the old
          3px bottom sliver, which read as a hairline rather than progress. */}
      <span className="reap-bar-track" aria-hidden="true">
        <span className="reap-bar-track-fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="reap-bar-actions">
        <button className="link" onClick={onGoToReap}>
          {t("reapConfirm.bar.view")}
        </button>
        <button
          className="stop-btn"
          disabled={status.stopping || stop.isPending}
          onClick={() => stop.mutate(runId)}
        >
          {status.stopping ? t("reapConfirm.stopping") : t("reapConfirm.stop")}
        </button>
      </span>
      {/* A Stop that failed must say so. Swallowed, it reads as a run that is halting while it
          keeps deleting, and this is the only Stop on every screen but the sheet. */}
      {stop.error && (
        <Notice tone="error" className="reap-bar-error">
          {t("reapConfirm.bar.stopError", { error: describeError(stop.error) })}
        </Notice>
      )}
    </div>
  );
}
