// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The two faces of the policy workspace's simulator column. `Outcome` is the answer: the
// counts, the bytes, and the histogram with the threshold drawn across it. `StaleNotice`
// is what shows instead when the last scan's stored scores can no longer answer honestly,
// and it carries the one button that fixes that.
//
// PolicyEditor.tsx decides which of the two renders, and its header says why that decision
// is the most important behavior on the page.
//
// The copy lives in `locales/en/ui.json` under `policySim.*`, one message per rendered
// sentence.

import type { RefObject } from "react";
import { Trans, useTranslation } from "react-i18next";
import type { ProfileSettings, ReasonKey, SimStale, Simulation } from "../api";
import { useSuccessorFocus } from "../focus";
import { bytes, count, totalBytes } from "../format";
import i18next from "../i18n";
import { composeIn } from "../why";
import { gateMeta, unnamedGateLabel } from "./policyMeta";
import { Notice } from "./Notice";
import { ProgressBar } from "./ProgressBar";

/** The histogram, with the threshold drawn across it.
 *
 *  This is what makes a threshold a decision rather than a guess: you place it against
 *  the actual shape of the library, and you can see how many items sit just the wrong
 *  side of it. */
function Histogram({ buckets, threshold }: { buckets: number[]; threshold: number }) {
  const { t } = useTranslation();
  const peak = Math.max(...buckets, 1);

  return (
    <div className="histogram" aria-hidden>
      {buckets.map((n, i) => {
        const low = i * 10;
        const condemned = low + 10 > threshold;
        return (
          <div
            className="hist-col"
            key={low}
            title={t("policySim.histogram.bucketTooltip", { low, high: low + 9, n: count(n) })}
          >
            <div
              className={condemned ? "hist-bar hist-condemn" : "hist-bar"}
              style={{ height: `${(n / peak) * 100}%` }}
            />
            <span className="hist-label">{low}</span>
          </div>
        );
      })}
      <div className="hist-thresh" style={{ left: `${threshold}%` }}>
        <b>{threshold}</b>
      </div>
    </div>
  );
}

/** The heading this panel shows while a rescan runs, and the sentence the app says when one
 *  starts. One string, because a reader lands on that heading in the next breath and two
 *  copies of one fact drift (rule 144); `PolicyEditor` announces it from here.
 *
 *  A function, not a constant: a string resolved in a module body stays in whatever language
 *  was serving when the module first loaded (`i18n-module-scope.test.ts`). */
export const rescanHeading = () => i18next.t("policySim.rescanHeading");

/** Said and shown instead when the rescan cannot carry the changes yet.
 *
 *  This is the one thing about the wait an operator cannot see: the bar in front of them
 *  belongs to a scan that started BEFORE they saved, so it is scoring the old policy. Saying
 *  `rescanHeading` here would be a sentence that is wrong about the very thing it describes,
 *  which is why the announcement branches rather than settling for one string. */
export const rescanQueuedLead = () => i18next.t("policySim.rescanQueuedLead");

/** What saving does, and the one thing the counts beside it cannot show: they describe a
 *  draft, and nothing on the server moves until a scan re-scores the library under it.
 *  `PolicyEditor`'s save mutation starts that scan itself.
 *
 *  One string, rendered here and in the savebar, because the two surfaces answer the same
 *  question and an operator reads them in either order (rule 144). It used to be the
 *  savebar's alone, which is the bar at the bottom of the LEFT column -- so an operator
 *  watching this panel's numbers move had nowhere on it to learn that the list they review
 *  had not moved with them. The refusal notice below carries the same news for the edits
 *  that cannot preview; this is the sentence for the ones that can. */
export const appliesOnNextScan = () => i18next.t("policySim.appliesOnNextScan");

/** The "needs a scan" state. Informational, not an error: you didn't do anything wrong,
 *  the numbers just can't be re-derived from the old scan. So it's neutral, short, and gives
 *  you the one button that fixes it. A start that fails says so right here, or the button
 *  would appear to do nothing. */
export function StaleNotice({
  scanning,
  followupQueued,
  starting,
  startError,
  onScan,
  percent,
  detail,
  staleKind,
  staleReason,
}: {
  scanning: boolean;
  /** A scan was already running when the rescan was requested, so a second one starts
   *  right after it. The copy must say so: the bar the owner is watching belongs to a
   *  scan that does NOT include their changes yet. */
  followupQueued: boolean;
  starting: boolean;
  startError: string | null;
  onScan: () => void;
  percent: number;
  detail: string;
  /** Which refusal this is, from the server. Null on a snapshot answered by an older build
   *  that did not send one, which lands on the general heading. */
  staleKind: SimStale | null;
  /** The server's typed reason for that refusal, composed here into the body paragraph. */
  staleReason: ReasonKey | null;
}) {
  const { t } = useTranslation();

  /** The heading for each refusal, keyed by what the server said the refusal was.
   *
   *  A heading only. The paragraph under it is the server's own `stale_reason`, so the
   *  sentence the operator reads and the sentence a reviewer reads are one string
   *  (`api/simulate.py`'s `_refused`, rule 144) -- they used to be two, and the frontend's copy
   *  was the only one anybody ever saw. An id this build does not know keeps the general
   *  heading and still renders that sentence, which is rule 66's "fallback handles unknown
   *  ids only": the server is always able to say what happened, even to an older browser. */
  const STALE_HEADINGS: Record<SimStale, string> = {
    gathers_differently: t("policySim.staleHeadings.gathersDifferently"),
    seasons_not_recorded: t("policySim.staleHeadings.seasonsNotRecorded"),
    // Names the control, never the cause: the episode map is also missing after a scan that
    // ran WITH the hold on and got no answer from Sonarr, and "turning that on" told that
    // operator they had done something they had not.
    in_progress_not_read: t("policySim.staleHeadings.inProgressNotRead"),
  };

  // "Scan now" replaces its own branch with the progress bar, and it is `disabled` from the press,
  // so focus is at `<body>` before the swap. Nothing focusable mounts in its place in ANY state of
  // this notice, so the target is the heading -- which is the same DOM node either side of the
  // swap (child 0 of both arms) and whose text becomes the sentence worth hearing (#173). The
  // pattern `useSavebarFocus` sets: after a completed action, the name of what you are looking at.
  const afterStart = useSuccessorFocus();
  return (
    <div className="sim sim-info">
      <h3 ref={afterStart.ref as RefObject<HTMLHeadingElement>} tabIndex={-1}>
        {scanning
          ? rescanHeading()
          : ((staleKind && STALE_HEADINGS[staleKind]) ?? STALE_HEADINGS.gathers_differently)}
      </h3>
      {scanning ? (
        <>
          <p>
            {followupQueued
              ? `${rescanQueuedLead()} ${t("policySim.queuedRescanTail")}`
              : t("policySim.scoringLibrary")}
          </p>
          <p className="muted">
            {t("policySim.progressStatus", {
              detail: detail || t("policySim.workingFallback"),
              percent,
            })}
          </p>
          <ProgressBar label={rescanHeading()} percent={percent} />
        </>
      ) : (
        <>
          {/* The server's reason, composed here rather than restated. It states the condition
              and never who caused it: this notice used to open "You changed what the scan
              reads" at operators who had changed nothing, because any upgrade adding a field
              to the hashed body leaves the recorded hash unmatchable until the next scan.

              It used to be a hardcoded paragraph here that named a keep tag, a season rule
              and the watch span all at once, beside a second copy in api/simulate.py that
              nothing ever rendered. Three refusals now, each with its own remedy, and a
              season rule previews rather than reaching any of them. `policySim.staleReason.<id>`
              (docs/history/I18N_PLAN.md §5) is composed from the one id `_refused` sends, so
              the reviewed sentence and the read sentence stay one catalog entry (rule 144).

              gathers_differently also fires with no policy edit at all, when the operator
              changes a protection list: the numbers then predate the lists, which is why the
              sentence states the mismatch and never names an edit (#512). */}
          <p>
            {staleReason
              ? composeIn("policySim.staleReason", staleReason)
              : t("policySim.staleReasonFallback")}
          </p>
          <button
            className="primary sm"
            onClick={() => {
              afterStart.arriving();
              onScan();
            }}
            disabled={starting}
          >
            {starting ? t("common.starting") : t("common.scanNow")}
          </button>
        </>
      )}
      {startError && (
        <Notice tone="error">{t("policySim.scanStartFailed", { error: startError })}</Notice>
      )}
    </div>
  );
}

export function Outcome({
  simulation,
  threshold,
  pace,
  edited,
}: {
  simulation: Simulation;
  threshold: number;
  pace: ProfileSettings | null;
  /** Whether the draft these numbers describe differs from the saved policy. The panel
   *  simulates on mount, before anything has been touched, so this is what separates "your
   *  edit changed no title" from "you changed nothing".
   *
   *  False while the answer on screen is a previous draft's. `PolicyEditor` keeps the last
   *  result rendered across a refetch, so "is this an edit" and "what did that edit do" can
   *  describe two different bodies for a round trip, and the sentence below is categorical
   *  enough that the mismatch reads as a finding rather than as a stale number (rule 85). */
  edited: boolean;
}) {
  const { t } = useTranslation();
  const moreExamples = simulation.newly_condemned - simulation.examples_newly_condemned.length;

  return (
    <div className="sim">
      <div className="sim-headline">
        <div>
          <span className="sim-number">{count(simulation.condemned)}</span>
          <span className="sim-unit">{t("policySim.itemsWouldBeRemoved")}</span>
        </div>
        <div>
          <span className="sim-number">
            {totalBytes(simulation.reclaimable_bytes, simulation.unknown_size_items)}
          </span>
          <span className="sim-unit">{t("policySim.reclaimed")}</span>
        </div>
      </div>

      {/* The comparison needs a draft to compare, so it renders only once there is one: with
          nothing edited it put the same number on both sides of a sentence built to contrast
          them, and the headline above already answers the untouched case on its own. On an
          inert edit the sentence IS the finding, which is why that branch stays. A `Notice`
          would be the wrong shape -- `NoticeTone` is error/warn and the component treats tone
          as a claim about severity, which this is not. */}
      {edited && (
        <>
          {simulation.changed_titles === 0 ? (
            <p className="sim-compare sim-inert">{t("policySim.noChangeNotice")}</p>
          ) : (
            <p className="sim-compare">
              <Trans
                i18nKey="policySim.comparisonLine"
                values={{
                  beforeCount: count(simulation.condemned_before),
                  afterCount: count(simulation.condemned),
                }}
                components={{ beforeNum: <strong />, afterNum: <strong /> }}
              />
            </p>
          )}
          {/* Under the same `edited` gate as the line above, and for the same reason: with
              nothing edited there is no save to describe, and an unprompted "a scan will
              start" beside numbers already drawn from the last one reads as a demand. It
              belongs on BOTH branches -- an inert edit saves and scans exactly like any
              other, and that operator has the most reason to wonder why a scan began. */}
          <p className="help">{appliesOnNextScan()}</p>
        </>
      )}

      <Histogram buckets={simulation.histogram} threshold={threshold} />
      <p className="help">{t("policySim.histogramHelp")}</p>

      {simulation.examples_newly_condemned.length > 0 && (
        <>
          <h3>{t("policySim.newOnTheList")}</h3>
          <ul className="sim-examples">
            {simulation.examples_newly_condemned.map((e) => (
              <li key={`${e.title}-${e.year ?? ""}`}>
                <span className="sim-example-title">
                  {e.title}
                  {e.year !== null && <span className="muted"> ({e.year})</span>}
                </span>
                <span className="sim-example-score">{e.score}</span>
              </li>
            ))}
            {moreExamples > 0 && (
              <li className="muted">{t("policySim.moreLeftAlone", { n: count(moreExamples) })}</li>
            )}
          </ul>
        </>
      )}

      {simulation.protected_by.length > 0 && (
        <>
          <h3>{t("policySim.whyTitlesWereSpared")}</h3>
          <dl className="sim-delta">
            {/* Every id the server can send is named in `gateMeta`, which `satisfies` keeps
                complete over `GateId`. The fallback is rule 66's, for an id from a server
                newer than this browser: it used to be `titleCase`, which printed the engine's
                own slug ("Season Progression", "Custom") as the reason a title was kept, in
                the panel read while deciding what to delete (#551, rule 21). */}
            {simulation.protected_by.map((g) => (
              <div key={g.gate}>
                <dt>{gateMeta()[g.gate]?.label ?? unnamedGateLabel()}</dt>
                <dd>{count(g.count)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}

      <dl className="sim-delta">
        {/* First, because it is the only row that summarizes the other four. The two deltas
            below count what enters and leaves the removal list, and the two after them are
            absolute counts with no before to read them against, so a protection edit that
            moved a sixth of the spared set into "not judged" left every number on this panel
            holding still (#488). */}
        <div className="sim-changed">
          <dt>{t("policySim.summary.titlesThatChange")}</dt>
          <dd>{count(simulation.changed_titles)}</dd>
        </div>
        <div>
          <dt>{t("policySim.summary.newlyCondemned")}</dt>
          <dd className={simulation.newly_condemned > 0 ? "danger" : ""}>
            +{count(simulation.newly_condemned)}
          </dd>
        </div>
        <div>
          <dt>{t("policySim.summary.noLongerCondemned")}</dt>
          <dd>−{count(simulation.no_longer_condemned)}</dd>
        </div>
        <div>
          <dt>{t("policySim.summary.sparedByAProtection")}</dt>
          <dd>{count(simulation.protected)}</dd>
        </div>
        <div>
          <dt>{t("policySim.summary.notJudged")}</dt>
          <dd>{count(simulation.abstained)}</dd>
        </div>
      </dl>

      {pace && (
        <p className="help">
          {/*
            Grace is a NOTICE, not a gate: nothing on the deletion path reads the window
            (services/grace.py), so "nothing is removed until it has waited out the
            N-day grace period" promised a hold that does not exist. What grace does is
            show a title as leaving for N days; what keeps it is a spare, a play, or the
            fact that a person starts every run.
          */}
          {pace.caps_enabled
            ? t("policySim.pace.withCaps", {
                items: count(pace.max_items_per_run),
                bytes: bytes(pace.max_bytes_per_run),
                days: pace.grace_days,
              })
            : // Caps off: the executor skips the per-run and rolling checks, so there is no
              // size limit to promise here (B-2). The countdown is unaffected by the switch.
              t("policySim.pace.noCaps", { days: pace.grace_days })}
        </p>
      )}

      <p className="blurb">{t("policySim.deltaBlurb")}</p>
    </div>
  );
}
