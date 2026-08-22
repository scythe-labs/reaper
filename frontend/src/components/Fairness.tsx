// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Scales: where the disk went, and to whom.
//
// The screen you reach for when the question is not "what should I delete?" but "who is my
// library actually for?" One card per requester -- the balance bar weighs how much of the
// disk they were granted the last scan keeps against how much it would reclaim, and the
// watched % on the side says how much of what they asked for they used themselves.
//
// Every card opens its person panel (the why-panel shell, in ScalesPanel), whether or not
// the scan condemns anything of theirs: the panel is their whole request story, not just a
// reap list. App owns which person is open and renders the panel beside this list, exactly
// as the review screen renders the why-panel beside the queue.
//
// It deletes nothing. It reads the last scan, so it can never disagree with Review.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Trans, useTranslation } from "react-i18next";
import { useSlowWait } from "../announce";
import { ApiError, api, type RequesterRow } from "../api";
import { describeError } from "../errors";
import { bytes, count } from "../format";
import { BalanceBar } from "./BalanceBar";
import { CardOpen } from "./CardOpen";
import { type WatchReach, mirrorNote, reachIsMeasured, watchReach } from "./watchReach";
import { Notice } from "./Notice";
import { StaleReadNotice } from "./StaleReadNotice";

/** The circular-arrow refresh glyph, in the app's 16-grid inline-SVG house style. */
function RefreshIcon() {
  return (
    <svg className="ico" viewBox="0 0 16 16" width="14" height="14" fill="none" aria-hidden="true">
      <path
        d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M13.5 2.5v3h-3"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Share of their OWN requests this person has watched at least once. A behavioral signal
 *  (did the asker use what they asked for), kept apart from the disk balance below.
 *
 *  `null` when there is no history to take a share OF, which `watchReach` answers for both
 *  surfaces. A Seerr account nobody linked to a Plex account has no watches Reaper can see at
 *  all: `fairness._roll_up` counts plays only inside `if pid is not None`, so `played_by_them`
 *  is structurally 0 rather than measured, and `plays_by`'s own docstring makes guarding the
 *  None the caller's job. An empty mirror is the same zero reached the other way, and used to
 *  slip through here because this only checked the account. Rendering either as a red 0% told
 *  the operator a confident zero about somebody Reaper never looked at, on the screen where
 *  they decide whose files to delete. `ScalesPanel` is the twin (rule 72). */
function watchedPct(row: RequesterRow, reach: WatchReach): number | null {
  if (!reachIsMeasured(reach)) return null;
  if (row.requests_made === 0) return 0;
  return Math.round((100 * row.played_by_them) / row.requests_made);
}

function initial(name: string): string {
  const c = name.trim()[0];
  return c ? c.toUpperCase() : "?";
}

/** One requester's card: a summary that opens their full breakdown. Exported so its states
 *  (reclaimable / clean / selected) can be tested against props without the query. */
export function PersonCard({
  row,
  selected,
  onSelect,
  horizonAt,
}: {
  row: RequesterRow;
  selected: boolean;
  onSelect: (identity: string) => void;
  /** How far back the watch mirror reaches, from the report the rows came in. Null is an
   *  empty mirror, where no play is visible for anyone and the card shows no percentage.
   *  Required rather than defaulted: a card that silently fell back to a span would be the
   *  confident zero this exists to stop. */
  horizonAt: string | null;
}) {
  const { t } = useTranslation();
  const reach = watchReach(row.plex_id, horizonAt);
  const watched = watchedPct(row, reach);
  const granted = row.gb_granted_bytes;
  const reclaim = row.reclaimable_bytes;
  const used = Math.max(0, granted - reclaim);
  const hasReclaim = row.reclaimable_items > 0;

  const open = () => onSelect(row.identity);

  return (
    // A plain container: `CardOpen` on the name is the control, and this click is the redundant
    // mouse affordance. It carried `role="button"`, whose Children Presentational pruned the
    // card's whole body out of the accessibility tree -- the request count, the granted figure,
    // the kept/reclaimable balance and the watched percentage -- leaving one name (#169).
    <div className={`fair-card clickable${selected ? " selected" : ""}`} onClick={open}>
      <span className="fair-avatar" aria-hidden="true">
        {initial(row.name)}
      </span>

      <div className="fair-body">
        <div className="fair-row1">
          <CardOpen
            name={t("scales.board.card.openBreakdown", { name: row.name })}
            onActivate={open}
          >
            <span className="fair-name">{row.name}</span>
          </CardOpen>
          <span className="fair-sub">
            <Trans
              i18nKey="scales.board.card.summary"
              values={{ requests: count(row.requests_made), granted: bytes(granted) }}
              components={{ reqCount: <strong />, grantedSize: <strong /> }}
            />
          </span>
        </div>

        <BalanceBar granted={granted} reclaim={reclaim} hasReclaim={hasReclaim} />
        <div className="fair-legend">
          <span>
            <Trans
              i18nKey="scales.board.card.earningKeep"
              values={{ used: bytes(used) }}
              components={{ usedSize: <strong /> }}
            />
          </span>
          {hasReclaim ? (
            <span className="bad">
              <Trans
                i18nKey="scales.board.card.toReclaim"
                values={{ reclaim: bytes(reclaim), n: row.reclaimable_items }}
                components={{ reclaimSize: <strong /> }}
              />
            </span>
          ) : (
            <span className="muted">{t("scales.board.card.nothingReclaimable")}</span>
          )}
        </div>
      </div>

      <div className="fair-side">
        {watched === null ? (
          <span
            className="fair-watched"
            title={
              reach.kind === "no_account"
                ? t("scales.board.card.noAccountTitle")
                : t("scales.board.card.noHistoryTitle")
            }
          >
            <span className="fair-pct muted">{t("scales.board.card.unknown")}</span>
            <span className="fair-pct-lbl">
              {reach.kind === "no_account"
                ? t("scales.board.card.noAccountShort")
                : t("scales.board.card.noHistoryShort")}
            </span>
          </span>
        ) : (
          <span className="fair-watched">
            <span className={`fair-pct ${watched >= 50 ? "good" : "bad"}`}>{watched}%</span>
            <span className="fair-pct-lbl">{t("scales.board.card.theyWatched")}</span>
          </span>
        )}
        {hasReclaim ? (
          <span className="status-chip status-pressure">
            {t("scales.board.card.reclaimChip", { amount: bytes(reclaim) })}
          </span>
        ) : (
          <span className="status-chip status-kept">{t("scales.board.card.nothingToReclaim")}</span>
        )}
      </div>
    </div>
  );
}

export function Fairness({
  selectedIdentity,
  onSelectPerson,
  onOpenUnmatched,
  unmatchedSelected,
}: {
  /** The person whose panel is open, so their card wears the selection bar. */
  selectedIdentity?: string | null;
  /** Open a person's panel. App owns the selection and renders the panel beside this list. */
  onSelectPerson?: (identity: string) => void;
  /** Open the "not in the last scan" panel. App owns which panel is open. */
  onOpenUnmatched?: () => void;
  /** Whether that panel is the one open, so the tile wears the selection bar. */
  unmatchedSelected?: boolean;
}) {
  const { t } = useTranslation();
  const { data, isPending, isFetching, error } = useQuery({
    queryKey: ["fairness"],
    queryFn: api.fairness,
  });
  const queryClient = useQueryClient();
  const select = onSelectPerson ?? (() => {});

  // The board's own copy already warns this can take a moment, so a wait that runs long is the
  // expected case here rather than a fault. It is still said, because the operator who cannot
  // see the spinner is the one that copy does not reach (#332).
  useSlowWait(isPending ? t("scales.board.slowWait") : null);

  // Scales reads live requests and watch history, so a refresh pulls the latest without a full
  // scan. Invalidating the "fairness" prefix refetches the board and any open person panel.
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["fairness"] });

  // Defined once and rendered in BOTH states, because the state that needs it most is the one
  // with no people on the board: a fresh portal, or ids the scan has not backfilled, leaves
  // every request unmatched, and this tile is the only thing explaining why the page is empty.
  // It used to live inside the has-people branch, so it was hidden exactly then (B-27).
  const notInScanTile = data && data.not_in_scan > 0 && (
    <button
      type="button"
      className={`fair-stat fair-stat-btn${unmatchedSelected ? " selected" : ""}`}
      onClick={() => onOpenUnmatched?.()}
      // No `aria-expanded`: this opens NotInScanPanel, which App renders in a different subtree
      // entirely, so the attribute promised a disclosure whose content sits nowhere near it and
      // pointed at nothing. It is the same gesture as a review card opening the why panel, and
      // those claim nothing either. What tells the operator it opened is the panel itself, which
      // names itself and, on a phone where it covers the screen, takes focus (WhyShell).
    >
      {/* The `{" "}` between each span is load-bearing, not formatting. A name is computed by
          concatenating the text of the children, and JSX drops the newline between two
          elements -- so with the spans merely stacked, this control announced as one run-on
          word ("40Not in the last scanrequested since...") and the count an operator is being
          asked to act on was not heard as a number at all (#284). A whitespace-only anonymous
          flex item is not rendered (CSS Flexbox 4.1), so the tile draws exactly as before. */}
      <span className="fair-stat-num amber">{count(data.not_in_scan)}</span>{" "}
      <span className="fair-stat-lbl">{t("scales.board.notInScanLabel")}</span>{" "}
      <span className="fair-stat-sub">{t("scales.board.notInScanSub")}</span>{" "}
      <span className="fair-stat-more">
        {t("scales.board.seeWhatTheseAre")} <span aria-hidden="true">›</span>
      </span>
    </button>
  );

  return (
    <section className="fair">
      <div className="fair-head">
        <div className="fair-head-top">
          <h2>{t("scales.board.heading")}</h2>
          <button
            type="button"
            className={`ghost sm fair-refresh${isFetching ? " busy" : ""}`}
            onClick={refresh}
            disabled={isFetching}
            title={t("scales.board.refreshTooltip")}
          >
            <RefreshIcon />
            {isFetching ? t("common.refreshing") : t("common.refresh")}
          </button>
        </div>
        <p className="blurb">{t("scales.board.blurb")}</p>
      </div>

      {/* Divided, and without the exception string rule 21 forbids. The board is refetched by
          the Refresh button above and by every override, so this sat over a fully drawn board
          saying the read had failed (#190).

          The server's own sentence is preferred to a fixed one. Scales refuses with a 400
          naming the services it needs, and an install with Tautulli plus an *arr and no Seerr
          is scan-ready by the wizard's own account, so that refusal is the DEFAULT reading of
          this tab: "Couldn't load Scales." left those operators a dead tab naming nothing they
          could act on (#412). `ApiError` is the gate because only that carries a reason Reaper
          wrote; a fetch failure's own message is not operator copy (rule 21). */}
      {error && !data && (
        <Notice tone="error">
          {error instanceof ApiError ? describeError(error) : t("scales.board.loadFailed")}
        </Notice>
      )}
      {error && data && <StaleReadNotice what={t("scales.board.staleWhat")} />}
      {isPending && (
        // Live region dropped: it was mounted in the same commit as its text, which several
        // readers never announce (#332). `useSlowWait` above speaks it instead.
        <div className="fair-loading">
          <span className="spinner spinner-xl" aria-hidden="true" />
          <p className="fair-loading-lead">{t("scales.board.loadingLead")}</p>
          <p className="fair-loading-sub muted">{t("scales.board.loadingSub")}</p>
        </div>
      )}

      {data?.no_snapshot && <p className="empty">{t("scales.board.noSnapshot")}</p>}

      {data && !data.no_snapshot && data.rows.length === 0 && (
        <>
          <p className="empty">{t("scales.board.noRequests")}</p>
          {notInScanTile && <div className="fair-stats fair-stats-lone">{notInScanTile}</div>}
        </>
      )}

      {data && !data.no_snapshot && data.rows.length > 0 && (
        <>
          <div className="fair-stats">
            <div className="fair-stat">
              <span className="fair-stat-num">{count(data.total_requests)}</span>
              <span className="fair-stat-lbl">{t("scales.board.requestsLabel")}</span>
              <span className="fair-stat-sub">
                {t("scales.board.acrossPeople", { n: data.rows.length })}
              </span>
            </div>
            <div className="fair-stat">
              <span className="fair-stat-num red">{bytes(data.total_reclaimable_bytes)}</span>
              <span className="fair-stat-lbl">{t("scales.board.reclaimableLabel")}</span>
              <span className="fair-stat-sub red">
                {t("scales.board.reclaimableSub", { n: data.total_reclaimable_items })}
              </span>
            </div>
            {notInScanTile}
          </div>

          {/* Every card's percentage is counted over this span, so it is named whatever the
              span is. It used to render only when there WAS one, which left the worst case --
              a mirror that has never synced, where each card said a red 0% -- as the one case
              with no caveat at all. The drawer prints the same line (rule 72). */}
          <p className="fair-horizon muted">{mirrorNote(data.horizon_at)}</p>

          <div className="fair-list">
            {data.rows.map((row) => (
              <PersonCard
                key={row.identity}
                row={row}
                selected={row.identity === selectedIdentity}
                onSelect={select}
                horizonAt={data.horizon_at}
              />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
