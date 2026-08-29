// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Why the policy condemned them, and the ledger behind the reap count.
//
// `useReapCounts` is the one place the Reap page turns the breakdown route into the numbers
// both halves of the page show: the summary card's tiles (ReapPlan.tsx) and this card's bars
// and ledger. Both read the same `["reap-breakdown"]` cache entry (React Query dedupes the
// request) and adjust it the same way, so the two can never disagree about what a reap
// actually removes. This component draws only the "why" side: the reason bars, and a
// closed-by-default fold holding the ledger that reached the total. The count itself, and
// what a reap removes it for, render in the summary card beside it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trans, useTranslation } from "react-i18next";
import { api, type ReapBreakdown as ReapBreakdownData, type SignalCount } from "../api";
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

/** Every number the Reap page's summary tiles and this card both need, read once. React
 *  Query dedupes the shared `["reap-breakdown"]` key, so calling this from two components
 *  costs one request, not two.
 *
 *  Adjusted for the unknown-size allowance the same way in both places (rule 62): the
 *  planner holds unmeasured items back only while `holdsBackUnmeasured` is true (the
 *  default), so a figure that did not subtract them would overstate what a reap removes.
 *  `allowanceUnknown` is the read-failed case: the safe default on a card is "assume held
 *  back", but silently assuming that here would understate a delete total, which is the
 *  unsafe direction on a page that states one. So a caller that shows this count checks
 *  `allowanceUnknown` and says it could not check, rather than printing an adjusted figure
 *  it cannot stand behind. */
export function useReapCounts(): {
  data: ReapBreakdownData | undefined;
  isPending: boolean;
  isError: boolean;
  holdsBackUnmeasured: boolean;
  allowanceUnknown: boolean;
  reapCount: number;
  reapBytes: number;
  movies: number;
  seasons: number;
} {
  const breakdown = useQuery({ queryKey: ["reap-breakdown"], queryFn: api.reapBreakdown });
  const allowance = useHoldsBackUnmeasured();
  const data = breakdown.data;
  const holdsBackUnmeasured = allowance.holdsBack;
  const allowanceUnknown = allowance.isError && (data?.will_reap_unknown ?? 0) > 0;
  const adjust = (total: number, unknown: number) =>
    holdsBackUnmeasured ? Math.max(0, total - unknown) : total;
  return {
    data,
    isPending: breakdown.isPending,
    isError: breakdown.isError,
    holdsBackUnmeasured,
    allowanceUnknown,
    reapCount: data ? adjust(data.will_reap, data.will_reap_unknown) : 0,
    reapBytes: data?.will_reap_bytes ?? 0,
    movies: data ? adjust(data.movies, data.movies_unknown) : 0,
    seasons: data ? adjust(data.seasons, data.seasons_unknown) : 0,
  };
}

export function ReapBreakdown({ onGoToReview }: { onGoToReview: () => void }) {
  const { t } = useTranslation();
  const counts = useReapCounts();
  const { data } = counts;

  // The one action the expired-spares notice below offers: a scan is what realizes a spare's
  // clock, so it is the only thing that hands those titles back to policy. The shared
  // `["scanStatus"]` cache, so this costs no request of its own: the shell already polls it,
  // and a scan already running (the scheduler, another device) must show here rather than
  // offer a button that starts a second one.
  const queryClient = useQueryClient();
  const { data: scanStatus } = useQuery({ queryKey: ["scanStatus"], queryFn: api.scanStatus });
  const scanning = scanStatus?.running ?? false;
  const startScan = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => queryClient.setQueryData(["scanStatus"], started),
  });

  // The summary card beside this one already says when the read failed, there is no scan
  // yet, or nothing would be reaped, in every one of those words. Saying it twice on one
  // page is worse than saying it once, so this card renders nothing until there is a real
  // ledger, reasons, or a pointer below to explain.
  if (counts.isPending || counts.isError || !data || !data.has_snapshot) return null;

  // Reasons and the ledger both explain a total, so both need one worth explaining: a real
  // count, or the honest "we can't say" of allowanceUnknown. Neither means the pointers below
  // may show: an expired-spares or held-reaps count is real on its own even when the net is
  // zero, which is exactly the case ("everything condemned was spared, and those spares have
  // since expired") this card must not go silent over.
  const showReasons = counts.allowanceUnknown || counts.reapCount > 0;
  const showLedger = !counts.allowanceUnknown && counts.reapCount > 0;
  const showExpiredSpares = data.spares_expired > 0;
  const showHeldReaps = data.hand_reaped_held > 0;
  if (!showReasons && !showExpiredSpares && !showHeldReaps) return null;

  // Held back, size unknown: the same figure the summary card's help sentence names, restated
  // here as the ledger row it is. Only real while the allowance actually holds them back
  // (`showLedger` already requires !allowanceUnknown, so `holdsBackUnmeasured` here is the
  // planner's own live answer, not a guess).
  const showHeldBack = data.will_reap_unknown > 0 && counts.holdsBackUnmeasured;

  return (
    <div className="reap-card">
      {showReasons && <Reasons rows={data.condemned_by} anchor={data.policy_condemned} />}
      {/* With nothing policy-condemned there are no reason bars, so without this line the
          card is a bare fold and the count above it looks like the policy's doing. */}
      {showLedger && data.policy_condemned === 0 && (
        <p className="help rb-all-hand">{t("reapPlan.breakdown.allHandReaps")}</p>
      )}
      {showLedger && (
        <details className="gates-fold rb-ledger-fold">
          <summary>
            <span className="fold-caret" aria-hidden="true">
              ▸
            </span>
            <span className="fold-shut-label">{t("reapPlan.breakdown.ledgerFoldLabel")}</span>
            <span className="fold-open-label">{t("common.hide")}</span>
          </summary>
          <div className="rb-ledger">
            {/* Condemned always shows: it is where the total in front of the fold came from,
                spared/reaped/held-back or not. */}
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
            {showHeldBack && (
              <div className="rb-row rb-spare">
                <span className="rb-lab">{t("reapPlan.breakdown.heldBack")}</span>
                <span className="rb-n">− {count(data.will_reap_unknown)}</span>
                <span className="rb-sz" />
              </div>
            )}
            <div className="rb-rule" />
            <div className="rb-row rb-total">
              <span className="rb-lab">{t("reapPlan.breakdown.willBeReaped")}</span>
              <span className="rb-n">{count(counts.reapCount)}</span>
              <span className="rb-sz">{bytes(counts.reapBytes)}</span>
            </div>
          </div>
          <p className="help rb-footnote">
            <Trans
              i18nKey="reapPlan.breakdown.singleTitleFootnote"
              components={{ btn: <button className="link" onClick={onGoToReview} /> }}
            />
          </p>
        </details>
      )}

      {/* Spares whose clock has passed. They are counted in "You spared by hand" above and are
          absent from the total, with nothing else on the page to say why. A spare's expiry is
          realized only by a scan (`whitelist.purge_expired_spares`), so until one runs, the
          planner, this ledger and the executor all still read it and the file is genuinely
          kept. */}
      {showExpiredSpares && (
        // `standing`: a scan-derived count, true before this page loaded and until the next
        // scan moves it.
        <Notice tone="warn" standing>
          {/* Titles, not spares: the server counts the rows a scan would hand back, and one
              whole-show spare can be holding five condemned seasons. */}
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
              ? t("common.scanning")
              : startScan.isPending
                ? t("common.starting")
                : t("common.scanNow")}
          </button>
        </Notice>
      )}
      {showExpiredSpares && startScan.isError && (
        <Notice tone="error">{t("reapPlan.breakdown.scanFailed")}</Notice>
      )}
      {showHeldReaps && (
        <div className="rb-line">
          {/* "this reap", not "a scan": a scan never removes anything. What holds these back
              is the reap this page is about. */}
          {t("reapPlan.breakdown.handReapedHeld", {
            n: data.hand_reaped_held,
            count: count(data.hand_reaped_held),
          })}{" "}
          <button className="link" onClick={onGoToReview}>
            {t("reapPlan.breakdown.seeReview")}{" "}
            <span className="dir-glyph" aria-hidden="true">
              →
            </span>
          </button>
        </div>
      )}
    </div>
  );
}
