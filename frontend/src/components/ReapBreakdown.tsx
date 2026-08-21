// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What this reap removes: the ledger, and why.
//
// The Reap page's context above the plan. It reads the latest scan and the owner's live
// overrides and shows what a reap would remove -- what the policy condemned, what the owner
// changed by hand, the net -- and why the policy condemned them, tallied from each title's
// own stored signals. It has no per-item controls: sparing or reconsidering a single title
// is the Review queue's job, and this links there rather than growing a second, weaker copy
// of it. Deletes nothing; the plan is still built, dry-run, and executed below.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trans, useTranslation } from "react-i18next";
import { api, type SignalCount } from "../api";
import { bytes, count } from "../format";
import i18next from "../i18n";
import { useHoldsBackUnmeasured } from "./queueSettings";
import { Notice } from "./Notice";

// Built-in signals read as a policy question in the editor ("How long it's gone
// unwatched"); here they name the reason a title was condemned, so they get their own
// short phrasing. A custom rule has no entry and shows under its own name. A plain
// function, not a frozen table, so a language change is picked up the next time this
// renders (same shape as JobsPanel's `jobMeta`, docs/history/I18N_PLAN.md §3).
function reasonLabel(id: string): string {
  switch (id) {
    case "unwatched":
      return i18next.t("reapPlan.breakdown.reasons.unwatched");
    case "few_watchers":
      return i18next.t("reapPlan.breakdown.reasons.fewWatchers");
    case "season_rank":
      return i18next.t("reapPlan.breakdown.reasons.seasonRank");
    case "low_rating":
      return i18next.t("reapPlan.breakdown.reasons.lowRating");
    case "size":
      return i18next.t("reapPlan.breakdown.reasons.size");
    default:
      return id;
  }
}

function Reasons({ rows, anchor }: { rows: SignalCount[]; anchor: number }) {
  const { t } = useTranslation();
  if (rows.length === 0) return null;
  const max = Math.max(...rows.map((r) => r.count), 1);
  return (
    <div className="rb-reasons">
      <div className="rb-reasons-head">
        <h3>{t("reapPlan.breakdown.reasonsHeading")}</h3>
        <span className="rb-of">
          {t("reapPlan.breakdown.reasonsOf", { n: anchor, count: count(anchor) })}
        </span>
      </div>
      {rows.map((r) => (
        <div className="rb-bar" key={r.id}>
          <span className="rb-rlab">{reasonLabel(r.id)}</span>
          <span className="rb-track">
            <span className="rb-fill" style={{ width: `${Math.round((r.count / max) * 100)}%` }} />
          </span>
          <span className="rb-rn">{count(r.count)}</span>
        </div>
      ))}
      <p className="rb-note">{t("reapPlan.breakdown.reasonsOverlap")}</p>
    </div>
  );
}

export function ReapBreakdown({
  onGoToPlexSettings,
  onGoToReview,
}: {
  /** Jump to Settings → Plex, where the Leaving Soon shelf lives. */
  onGoToPlexSettings: () => void;
  /** Jump to the Review queue, where per-title decisions are made. */
  onGoToReview: () => void;
}) {
  const { t } = useTranslation();
  const { data, isPending, isError } = useQuery({
    queryKey: ["reap-breakdown"],
    queryFn: api.reapBreakdown,
  });
  // The planner holds unmeasured items back only while the unknown-size allowance is 0 (its
  // default). Above zero it admits them, so the "won't remove them" line and the count that
  // subtracts them would both be lies (B-8). One shared query key, so this is free here.
  // Every number on this page turns on it, so a read that FAILED is said out loud rather
  // than defaulted: the card's safe default (assume held back) would silently shrink a
  // delete count here, which is the unsafe direction.
  const allowance = useHoldsBackUnmeasured();
  const holdsBackUnmeasured = allowance.holdsBack;
  // The one action the expired-spares notice below offers: a scan is what realizes a spare's
  // clock, so it is the only thing that hands those titles back to policy. Declared up here
  // with the other hooks -- the early returns below are what makes that necessary.
  //
  // The shared `["scanStatus"]` cache, so this costs no request of its own: the shell already
  // polls it, and a scan already running (the scheduler, another device) must show here rather
  // than offer a button that starts a second one. What the finished scan then refreshes --
  // this ledger included -- is the shell's `useScanSettled`, not this component, which the
  // operator can navigate away from mid-scan.
  const queryClient = useQueryClient();
  const { data: scanStatus } = useQuery({ queryKey: ["scanStatus"], queryFn: api.scanStatus });
  const scanning = scanStatus?.running ?? false;
  const startScan = useMutation({
    mutationFn: () => api.startScan(),
    // Seed the cache with the returned status so the polling starts at once, rather than the
    // shell's idle poll taking up to 15s to notice (the scan bar's own start does the same).
    onSuccess: (started) => queryClient.setQueryData(["scanStatus"], started),
  });

  if (isPending) return <p className="muted">{t("reapPlan.breakdown.loading")}</p>;
  // An unreadable breakdown must never look like "nothing to reap": say we couldn't look,
  // in the amber tone, rather than rendering an empty ledger.
  //
  // Undivided on purpose, and the one site in the #190 sweep that stays that way. Everywhere
  // else a failed refetch keeps the last good value and says it may be out of date, because what
  // is held beats nothing. What is held HERE is a set of delete counts, and ["reap-breakdown"]
  // is override-aware, so one Spare click refetches it: keeping the old ledger would state how
  // many titles this reap removes at the moment the operator changed that number. `PolicyEditor`
  // settles the same question the same way for the simulator's column ("a stale count shown as
  // current is exactly what this column must never do"), so refusing is the consistent answer
  // rather than an unswept branch. The arm covers both reads and the `!data` disjunct says so:
  // a first read that failed settles at `isError` with `data` still undefined, which `isPending`
  // above does not catch. Only the OTHER case, a failed refetch with a ledger still in hand, is
  // dropping anything, and it is the one this note is about.
  if (isError || !data) {
    return <Notice tone="warn">{t("reapPlan.breakdown.loadFailed")}</Notice>;
  }

  if (!data.has_snapshot) {
    return (
      <div className="reap-breakdown">
        <div className="rb-head">{t("reapPlan.breakdown.title")}</div>
        <p className="rb-sub">{t("reapPlan.breakdown.noScan")}</p>
      </div>
    );
  }

  const overrides = data.hand_spared > 0 || data.hand_reaped > 0;
  // The count the run will actually reap. With the allowance off (the default) the planner
  // drops the unmeasured tail, so the headline and total must match the confirmed count, not
  // will_reap, which still counts those held-back items (B-8, rule 30). Bytes already sum only
  // what is measured, so they need no adjustment.
  const reapCount = holdsBackUnmeasured
    ? Math.max(0, data.will_reap - data.will_reap_unknown)
    : data.will_reap;
  // The split runs over the same set as the total above it, subtracting the same held-back
  // rows. Printing the raw figures beside an adjusted total had one page stating two
  // different counts for one reap (rule 30).
  const movies = holdsBackUnmeasured ? Math.max(0, data.movies - data.movies_unknown) : data.movies;
  const seasons = holdsBackUnmeasured
    ? Math.max(0, data.seasons - data.seasons_unknown)
    : data.seasons;
  // Without the allowance there is no honest count to print -- but only when something on
  // the list is actually unmeasured; otherwise the two answers agree and the read doesn't
  // matter.
  const allowanceUnknown = allowance.isError && data.will_reap_unknown > 0;
  // The unmeasured note is its own line only while a ledger is showing; the empty state
  // says the same thing in one sentence rather than two.
  const showUnmeasuredLine = data.will_reap_unknown > 0 && reapCount > 0 && !allowanceUnknown;

  return (
    <div className="reap-breakdown">
      <div className="rb-headline">
        <span className="rb-head">{t("reapPlan.breakdown.title")}</span>
        <span className="rb-meta">
          {!allowanceUnknown && (
            <>
              <Trans
                i18nKey="reapPlan.breakdown.reapCountMeta"
                values={{ n: reapCount, count: count(reapCount) }}
                components={{ strong: <strong /> }}
              />{" "}
            </>
          )}
          <strong>{bytes(data.will_reap_bytes)}</strong>
        </span>
      </div>
      <p className="rb-sub">{t("reapPlan.breakdown.sub")}</p>

      {allowanceUnknown ? (
        <>
          {/* No reload advice (#195): this renders on the Reap page, above a plan the operator
              may have spent a queue's worth of Spare and Reap decisions arriving at. */}
          <Notice tone="warn">
            {t("reapPlan.breakdown.allowanceUnknown", {
              n: data.will_reap_unknown,
              count: count(data.will_reap_unknown),
            })}
          </Notice>
          <Reasons rows={data.condemned_by} anchor={data.policy_condemned} />
        </>
      ) : reapCount === 0 ? (
        <p className="rb-empty">
          {data.will_reap > 0
            ? t("reapPlan.breakdown.emptyMeasured")
            : data.policy_condemned > 0 && data.hand_spared > 0
              ? t("reapPlan.breakdown.emptySpared")
              : t("reapPlan.breakdown.emptyNone")}
        </p>
      ) : (
        <>
          <div className="rb-ledger">
            {overrides && (
              <>
                <div className="rb-row">
                  <span className="rb-lab">{t("reapPlan.breakdown.condemnedByPolicy")}</span>
                  <span className="rb-n">{count(data.policy_condemned)}</span>
                  <span className="rb-sz">{bytes(data.policy_condemned_bytes)}</span>
                </div>
                {data.hand_spared > 0 && (
                  <div className="rb-row rb-spare">
                    <span className="rb-lab">{t("reapPlan.breakdown.handSpared")}</span>
                    <span className="rb-n">− {count(data.hand_spared)}</span>
                    <span className="rb-sz">{t("reapPlan.breakdown.kept")}</span>
                  </div>
                )}
                {data.hand_reaped > 0 && (
                  <div className="rb-row rb-add">
                    <span className="rb-lab">{t("reapPlan.breakdown.handReaped")}</span>
                    <span className="rb-n">+ {count(data.hand_reaped)}</span>
                    <span className="rb-sz">{bytes(data.hand_reaped_bytes)}</span>
                  </div>
                )}
                <div className="rb-rule" />
              </>
            )}
            <div className="rb-row rb-total">
              <span className="rb-lab">{t("reapPlan.breakdown.willBeReaped")}</span>
              <span className="rb-n">{count(reapCount)}</span>
              <span className="rb-sz">{bytes(data.will_reap_bytes)}</span>
            </div>
          </div>
          <div className="rb-split">
            {t("reapPlan.breakdown.split", {
              movies,
              moviesCount: count(movies),
              seasons,
              seasonsCount: count(seasons),
            })}
          </div>

          <Reasons rows={data.condemned_by} anchor={data.policy_condemned} />
        </>
      )}

      {/* Spares whose clock has passed. They are counted in "You spared by hand" above and are
          absent from the total, with nothing else on the page to say why -- a spare's expiry is
          realized only by a SCAN (`whitelist.purge_expired_spares`), so until one runs the
          planner, this ledger and the executor all still read it and the file is genuinely kept.
          Outside the reapCount branch on purpose: when every condemned title was spared and
          those spares have since expired, the empty state is exactly when this matters most.
          Warn tone with its own action, like the page's stale-plan notice, because the operator
          has to do something (scan) for these to move -- not the informational `.rb-line` the
          held reaps use, which only points at Review. */}
      {data.spares_expired > 0 && (
        // `standing`: a scan-derived count, true before this page loaded and until the next scan
        // moves it. Its sibling below is the opposite -- `startScan.isError` answers the Scan now
        // press inside this notice -- and stays an alert.
        <Notice tone="warn" standing>
          {/* Titles, not spares: the server counts the rows a scan would hand back, and one
              whole-show spare can be holding five condemned seasons. Calling those "5 spares"
              named a thing the operator has one of (rule 21). */}
          <Trans
            i18nKey="reapPlan.breakdown.expiredSpares"
            values={{ n: data.spares_expired, count: count(data.spares_expired) }}
            components={{ strong: <strong /> }}
          />{" "}
          <button
            className="link"
            onClick={() => startScan.mutate()}
            disabled={startScan.isPending || scanning}
          >
            {scanning
              ? t("reapPlan.breakdown.scanning")
              : startScan.isPending
                ? t("reapPlan.breakdown.starting")
                : t("reapPlan.breakdown.scanNow")}
          </button>
        </Notice>
      )}
      {/* Its own notice in the shared error tone, not red text inside the warning above:
          every action failure in the app reads the same way (rule 42). */}
      {data.spares_expired > 0 && startScan.isError && (
        <Notice tone="error">{t("reapPlan.breakdown.scanFailed")}</Notice>
      )}
      {data.hand_reaped_held > 0 && (
        <div className="rb-line">
          {/* "this reap", not "a scan": a scan never removes anything, and the Jobs page
              says exactly that in the same product ("A scan only reads. It cannot
              delete."). What holds these back is the reap this page is about (U-10). */}
          {t("reapPlan.breakdown.handReapedHeld", {
            n: data.hand_reaped_held,
            count: count(data.hand_reaped_held),
          })}{" "}
          <button className="link" onClick={onGoToReview}>
            {t("reapPlan.breakdown.seeReview")} <span aria-hidden="true">→</span>
          </button>
        </div>
      )}
      {showUnmeasuredLine &&
        (holdsBackUnmeasured ? (
          <div className="rb-line">
            {t("reapPlan.breakdown.unmeasuredHeld", {
              n: data.will_reap_unknown,
              count: count(data.will_reap_unknown),
            })}
          </div>
        ) : (
          <div className="rb-line">
            {t("reapPlan.breakdown.unmeasuredAllowed", {
              n: data.will_reap_unknown,
              count: count(data.will_reap_unknown),
            })}
          </div>
        ))}
      <div className="rb-line">
        <Trans
          i18nKey="reapPlan.breakdown.plexNotice"
          components={{ btn: <button className="link" onClick={onGoToPlexSettings} /> }}
        />
      </div>
      <div className="rb-line">
        {t("reapPlan.breakdown.reviewQueueLead")}{" "}
        <button className="link" onClick={onGoToReview}>
          {t("reapPlan.breakdown.reviewQueue")} <span aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  );
}
