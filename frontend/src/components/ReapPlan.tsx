// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Reap tab: plan on the left, audit on the right.
//
// Idle and blocked are one screen. Blocked is idle with the failing checks named in the
// summary card and the head Reap button disabled; nothing else changes. "Reap N titles…"
// builds the plan and opens the confirmation sheet (ReapConfirm.tsx) in one action, which is
// the one gauntlet that can start a deletion: a dry run, the armed check, and the typed
// content-bound phrase, all unchanged here. "Practice run" walks the same dry run standalone,
// with no sheet and no arming required, and reports pass or fail right here on the page.
//
// Every number below (the four tiles, the reason bars, the ledger) comes from
// `useReapCounts` (ReapBreakdown.tsx), which reads GET /api/reap/breakdown once and adjusts
// it for the unknown-size allowance the same way everywhere it is shown, so the tiles and the
// ledger can never disagree about what a reap would actually remove.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { api, type Run, type RunSummary } from "../api";
import { DegradedDocLink } from "../docs/DocLink";
import { describeError } from "../errors";
import { bytes, count, date } from "../format";
import { reapBlockers, type ReapBlocker } from "../reapReadiness";
import { useSafety } from "../useSafety";
import { composeError } from "../why";
import { ReapBreakdown, useReapCounts } from "./ReapBreakdown";
import { ReapConfirm } from "./ReapConfirm";
import { Notice } from "./Notice";

/** The link text beside one failing readiness check, naming where to go fix it. Written as a
 *  literal per key, like `reasonLabel` in ReapBreakdown.tsx, rather than composing a catalog
 *  key from `b.key` at runtime, which the i18n key gate cannot verify. */
function blockerFixLabel(key: ReapBlocker["key"], t: (k: string) => string): string {
  if (key === "password") return t("reapPlan.summary.blockers.fixPassword");
  if (key === "plex") return t("reapPlan.summary.blockers.fixPlex");
  if (key === "tautulli") return t("reapPlan.summary.blockers.fixTautulli");
  return t("reapPlan.summary.blockers.fixArr");
}

/** The history row's state dot. Only three colors are ever claimed as a real outcome
 *  (protect for a clean finish, amber for aborted, condemn for one still running); a plan
 *  never executed, or a state this build does not know, reads as neutral rather than being
 *  painted as any of the three, an older or newer build included. */
function runDotClass(state: string): string {
  if (state === "completed") return "reap-run-dot ok";
  if (state === "aborted") return "reap-run-dot bad";
  if (state === "executing") return "reap-run-dot live";
  return "reap-run-dot";
}

function HistoryRow({ run }: { run: RunSummary }) {
  const { t } = useTranslation();
  const parts: string[] = [];
  if (run.deleted_items != null && run.deleted_bytes != null) {
    parts.push(
      t("reapPlan.history.freedRemoved", {
        bytes: bytes(run.deleted_bytes),
        count: count(run.deleted_items),
      }),
    );
  }
  if (run.aborted_reason) parts.push(composeError(run.aborted_reason));
  return (
    <div className="reap-run">
      <span className={runDotClass(run.state)} aria-hidden="true" />
      <span className="reap-run-what">
        {t("reapPlan.history.row", { id: run.id })}
        {parts.length > 0 && <span className="reap-run-sub">{parts.join(" ")}</span>}
      </span>
      {/* `finished_at` is null before a run reaches a terminal state; `approved_at` (when it
          was created) is always set, and a real date beats none on a row that still has to
          show something on its right edge. */}
      <span className="reap-run-when">{date(run.finished_at ?? run.approved_at)}</span>
    </div>
  );
}

export function ReapPlan({
  onGoToSecurity,
  onGoToServices,
  onGoToPlexSettings,
  onGoToReview,
}: {
  /** Jump to Settings → Security, where the admin password is set. */
  onGoToSecurity: () => void;
  /** Jump to Settings → Services, where Radarr, Sonarr and Tautulli connect. */
  onGoToServices: () => void;
  /** Jump to Settings → Plex. */
  onGoToPlexSettings: () => void;
  /** Jump to the Review queue, where per-title decisions are made. */
  onGoToReview: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const safety = useSafety();
  const armed = safety.data?.destructive_enabled ?? false;

  const setup = useQuery({ queryKey: ["setup"], queryFn: api.setupStatus });
  const blockers: ReapBlocker[] = setup.data ? reapBlockers(setup.data) : [];
  // Empty while `setup` is still pending too, which is why `reapReady` below checks
  // `setup.isPending` on its own rather than trusting an empty `blockers` array: a read in
  // flight must never be read as every check having passed.
  const setupUnknown = setup.isError && !setup.data;

  // The planner refuses a degraded snapshot outright, so offering Reap over one only trades a
  // click for a 422. Said here, before the tiles below read as a list Reaper is ready to act
  // on. `retry: false`: a missing snapshot (no scan yet) is the normal empty state, not a
  // failure worth retrying.
  const snapshot = useQuery({ queryKey: ["snapshot"], queryFn: api.latestSnapshot, retry: false });
  const degraded = snapshot.data?.degraded === true;

  const counts = useReapCounts();
  const data = counts.data;

  const history = useQuery({ queryKey: ["runs"], queryFn: () => api.runs() });
  // A "planned" run is a plan that was built (the head Reap button, a standalone practice run)
  // and never executed: not a past reap, so it does not belong on a list of them. Filtered
  // client-side rather than at the route, since other surfaces (the app-wide bar's View) still
  // need to read a planned run by id.
  const pastRuns = history.data?.filter((run) => run.state !== "planned") ?? [];

  const [confirmRun, setConfirmRun] = useState<Run | null>(null);

  const reapReady =
    armed &&
    !setup.isPending &&
    !setup.isError &&
    !!setup.data &&
    blockers.length === 0 &&
    !degraded &&
    !counts.isPending &&
    !counts.isError &&
    !counts.allowanceUnknown &&
    counts.reapCount > 0;

  const createAndConfirm = useMutation({
    mutationFn: () => api.createRun("all"),
    onSuccess: (r) => {
      setConfirmRun(r);
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
  });

  const practice = useMutation({
    mutationFn: async () => {
      const run = await api.createRun("all");
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      return api.dryRun(run.id);
    },
  });
  const practiceReady =
    !degraded && !counts.isPending && !!data?.has_snapshot && counts.reapCount > 0;

  return (
    <section className="reap">
      <div className="reap-head">
        <h2>{t("reapPlan.page.title")}</h2>
        <div className="reap-head-actions">
          <button
            type="button"
            className="ghost"
            onClick={() => practice.mutate()}
            disabled={practice.isPending || !practiceReady}
          >
            {t("reapPlan.actions.practiceRun")}
          </button>
          <button
            type="button"
            className="danger fill"
            onClick={() => createAndConfirm.mutate()}
            disabled={createAndConfirm.isPending || !reapReady}
          >
            {createAndConfirm.isPending
              ? t("common.planning")
              : t("reapPlan.actions.reapButton", { count: counts.reapCount, n: counts.reapCount })}
          </button>
        </div>
      </div>

      <div className="reap-columns">
        <div className="reap-left">
          <div className="reap-card">
            {createAndConfirm.error && (
              <Notice tone="error">{describeError(createAndConfirm.error)}</Notice>
            )}

            {counts.isPending ? (
              <p className="muted">{t("common.loading")}</p>
            ) : counts.isError || !data ? (
              <Notice tone="warn">{t("reapPlan.summary.loadFailed")}</Notice>
            ) : !data.has_snapshot ? (
              <p className="muted">{t("reapPlan.summary.noScan")}</p>
            ) : (
              <>
                {degraded && (
                  // `standing`: `["snapshot"]` carries the last scan's own verdict on itself,
                  // so this is true before the page loads and stays true until a clean scan
                  // replaces it, never a reply to anything pressed here.
                  <Notice tone="warn" standing as="div" className="notice-doc">
                    <span>
                      <Trans
                        i18nKey="reapPlan.degraded.notice"
                        values={{ reason: snapshot.data?.degraded_reason ?? "" }}
                        components={{ strong: <strong /> }}
                      />
                    </span>
                    <DegradedDocLink doc={snapshot.data?.degraded_doc ?? null} />
                  </Notice>
                )}

                {counts.allowanceUnknown ? (
                  <Notice tone="warn">
                    {t("reapPlan.summary.allowanceUnknown", {
                      n: data.will_reap_unknown,
                      count: count(data.will_reap_unknown),
                    })}
                  </Notice>
                ) : counts.reapCount === 0 ? (
                  <p className="rb-empty">
                    {data.will_reap > 0
                      ? t("reapPlan.summary.emptyMeasured")
                      : data.policy_condemned > 0 && data.hand_spared > 0
                        ? t("reapPlan.summary.emptySpared")
                        : t("reapPlan.summary.emptyNone")}
                  </p>
                ) : (
                  <>
                    <div className="fair-stats">
                      <div className="fair-stat">
                        <span className="fair-stat-num">{count(counts.reapCount)}</span>
                        <span className="fair-stat-lbl">{t("reapPlan.summary.tiles.titles")}</span>
                      </div>
                      <div className="fair-stat">
                        <span className="fair-stat-num">{bytes(counts.reapBytes)}</span>
                        <span className="fair-stat-lbl">{t("reapPlan.summary.tiles.toFree")}</span>
                      </div>
                      <div className="fair-stat">
                        <span className="fair-stat-num">{count(counts.movies)}</span>
                        <span className="fair-stat-lbl">{t("reapPlan.summary.tiles.movies")}</span>
                      </div>
                      <div className="fair-stat">
                        <span className="fair-stat-num">{count(counts.seasons)}</span>
                        <span className="fair-stat-lbl">{t("reapPlan.summary.tiles.seasons")}</span>
                      </div>
                    </div>
                    <p className="help reap-summary-help">
                      {data.will_reap_unknown > 0 &&
                        (counts.holdsBackUnmeasured ? (
                          <>
                            {t("reapPlan.summary.help.heldBack", {
                              count: data.will_reap_unknown,
                              n: data.will_reap_unknown,
                            })}{" "}
                          </>
                        ) : (
                          <>
                            {t("reapPlan.summary.help.included", {
                              count: data.will_reap_unknown,
                              n: data.will_reap_unknown,
                            })}{" "}
                          </>
                        ))}
                      <Trans
                        i18nKey="reapPlan.summary.help.review"
                        components={{ btn: <button className="link" onClick={onGoToReview} /> }}
                      />
                    </p>
                  </>
                )}

                {/* The one blockers notice, from reapReadiness.ts: only the checks that fail,
                    each with a way to fix it. An unread setup state is its own line rather
                    than a claim any of the four passed. Nothing renders when all pass. */}
                {setupUnknown ? (
                  // `standing`: `["setup"]` is the state of the read itself, true from the
                  // moment it fails until the next one lands, not a reply to a press here.
                  <Notice tone="warn" standing>
                    {t("reapPlan.summary.setupUnknown")}
                  </Notice>
                ) : (
                  blockers.length > 0 && (
                    <Notice tone="warn" standing as="div">
                      <ul className="reap-blockers">
                        {blockers.map((b) => (
                          <li key={b.key}>
                            {b.sentence}{" "}
                            <button
                              className="link"
                              onClick={
                                b.key === "password"
                                  ? onGoToSecurity
                                  : b.key === "plex"
                                    ? onGoToPlexSettings
                                    : onGoToServices
                              }
                            >
                              {blockerFixLabel(b.key, t)}
                            </button>
                          </li>
                        ))}
                      </ul>
                    </Notice>
                  )
                )}
              </>
            )}

            {practice.isPending && (
              <div className="reap-practice-running">
                <span className="spinner" aria-hidden="true" />
                <span>{t("reapPlan.practiceRun.running")}</span>
              </div>
            )}
            {practice.isError && (
              <Notice tone="error">
                {t("reapConfirm.practiceRun.failed", { message: describeError(practice.error) })}
              </Notice>
            )}
            {practice.data && practice.data.state === "aborted" && (
              <div className="sim sim-info">
                <strong>{t("reapConfirm.practiceRun.stopped")}</strong>
                <p>{practice.data.aborted_reason && composeError(practice.data.aborted_reason)}</p>
              </div>
            )}
            {practice.data && practice.data.state !== "aborted" && (
              <div className="reap-practice-pass">
                {t("reapPlan.practiceRun.passed", {
                  count: practice.data.would_delete_items,
                  n: practice.data.would_delete_items,
                  bytes: bytes(practice.data.deleted_bytes),
                })}
                {practice.data.skipped > 0 &&
                  t("reapPlan.practiceRun.keptSuffix", { count: practice.data.skipped })}{" "}
                <button type="button" className="link" onClick={() => practice.reset()}>
                  {t("reapPlan.practiceRun.dismiss")}
                </button>
              </div>
            )}
          </div>

          <ReapBreakdown onGoToReview={onGoToReview} />
        </div>

        <div className="reap-card reap-history">
          <h3>{t("reapPlan.history.heading")}</h3>
          {history.isPending ? (
            <p className="muted">{t("common.loading")}</p>
          ) : history.isError || !history.data ? (
            <Notice tone="warn">{t("reapPlan.history.loadFailed")}</Notice>
          ) : pastRuns.length === 0 ? (
            <p className="muted">{t("reapPlan.history.empty")}</p>
          ) : (
            <div className="reap-runs">
              {pastRuns.map((run) => (
                <HistoryRow key={run.id} run={run} />
              ))}
            </div>
          )}
        </div>
      </div>

      {confirmRun && <ReapConfirm run={confirmRun} onClose={() => setConfirmRun(null)} />}
    </section>
  );
}
