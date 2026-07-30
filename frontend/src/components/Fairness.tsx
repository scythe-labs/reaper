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
import { api, type RequesterRow } from "../api";
import { bytes, count } from "../format";
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
  const reach = watchReach(row.plex_id, horizonAt);
  const watched = watchedPct(row, reach);
  const granted = row.gb_granted_bytes;
  const reclaim = row.reclaimable_bytes;
  const used = Math.max(0, granted - reclaim);
  // Granted can be zero (no size known for anything they asked for): show a full, neutral
  // bar rather than dividing by zero.
  const usedPct = granted > 0 ? (100 * used) / granted : 100;
  const reclaimPct = granted > 0 ? (100 * reclaim) / granted : 0;
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
          <CardOpen name={`Open ${row.name}'s request breakdown`} onActivate={open}>
            <span className="fair-name">{row.name}</span>
          </CardOpen>
          <span className="fair-sub">
            <strong>{count(row.requests_made)}</strong> requests · <strong>{bytes(granted)}</strong>{" "}
            granted
          </span>
        </div>

        <div
          className="fair-balance"
          role="img"
          aria-label={
            hasReclaim
              ? `${bytes(used)} kept, ${bytes(reclaim)} the scan would reclaim`
              : `${bytes(used)} kept, nothing reclaimable`
          }
        >
          <i className="used" style={{ width: `${usedPct}%` }} />
          {reclaimPct > 0 && <i className="reclaim" style={{ width: `${reclaimPct}%` }} />}
        </div>
        <div className="fair-legend">
          <span>
            <strong>{bytes(used)}</strong> earning its keep
          </span>
          {hasReclaim ? (
            <span className="bad">
              <strong>{bytes(reclaim)}</strong> to reclaim · {count(row.reclaimable_items)}{" "}
              {row.reclaimable_items === 1 ? "title" : "titles"}
            </span>
          ) : (
            <span className="muted">nothing reclaimable</span>
          )}
        </div>
      </div>

      <div className="fair-side">
        {watched === null ? (
          <span
            className="fair-watched"
            title={
              reach.kind === "no_account"
                ? "Their request account isn't linked to a Plex account, so Reaper can't see what they watched."
                : "Reaper hasn't read any watch history yet, so it can't see what anyone watched."
            }
          >
            <span className="fair-pct muted">Unknown</span>
            <span className="fair-pct-lbl">
              {reach.kind === "no_account" ? "no Plex account" : "no watch history"}
            </span>
          </span>
        ) : (
          <span className="fair-watched">
            <span className={`fair-pct ${watched >= 50 ? "good" : "bad"}`}>{watched}%</span>
            <span className="fair-pct-lbl">they watched</span>
          </span>
        )}
        {hasReclaim ? (
          <span className="status-chip status-pressure">{bytes(reclaim)} to reclaim</span>
        ) : (
          <span className="status-chip status-kept">Nothing to reclaim</span>
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
  const { data, isPending, isFetching, error } = useQuery({
    queryKey: ["fairness"],
    queryFn: api.fairness,
  });
  const queryClient = useQueryClient();
  const select = onSelectPerson ?? (() => {});

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
      <span className="fair-stat-lbl">Not in the last scan</span>{" "}
      <span className="fair-stat-sub">requested since, or filtered out</span>{" "}
      <span className="fair-stat-more">
        See what these are <span aria-hidden="true">›</span>
      </span>
    </button>
  );

  return (
    <section className="fair">
      <div className="fair-head">
        <div className="fair-head-top">
          <h2>Scales</h2>
          <button
            type="button"
            className={`ghost sm fair-refresh${isFetching ? " busy" : ""}`}
            onClick={refresh}
            disabled={isFetching}
            title="Reload requests and watch history"
          >
            <RefreshIcon />
            {isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
        <p className="blurb">
          Who asked for what, and who actually watched it. Read only: nothing here removes anything.
        </p>
      </div>

      {/* Divided, and without the exception string rule 21 forbids. The board is refetched by
          the Refresh button above and by every override, so this sat over a fully drawn board
          saying the read had failed (#190). */}
      {error && !data && <Notice tone="error">Couldn't load Scales.</Notice>}
      {error && data && <StaleReadNotice what="Scales" />}
      {isPending && (
        <div className="fair-loading" role="status" aria-live="polite">
          <span className="spinner spinner-xl" aria-hidden="true" />
          <p className="fair-loading-lead">Gathering requests…</p>
          <p className="fair-loading-sub muted">
            Reading every request and matching it to your last scan. This can take a moment.
          </p>
        </div>
      )}

      {data?.no_snapshot && (
        <p className="empty">Run a scan first. Scales reads your last library scan.</p>
      )}

      {data && !data.no_snapshot && data.rows.length === 0 && (
        <>
          <p className="empty">No available requests are in the last scan yet.</p>
          {notInScanTile && <div className="fair-stats fair-stats-lone">{notInScanTile}</div>}
        </>
      )}

      {data && !data.no_snapshot && data.rows.length > 0 && (
        <>
          <div className="fair-stats">
            <div className="fair-stat">
              <span className="fair-stat-num">{count(data.total_requests)}</span>
              <span className="fair-stat-lbl">Requests</span>
              <span className="fair-stat-sub">
                across {count(data.rows.length)} {data.rows.length === 1 ? "person" : "people"}
              </span>
            </div>
            <div className="fair-stat">
              <span className="fair-stat-num red">{bytes(data.total_reclaimable_bytes)}</span>
              <span className="fair-stat-lbl">Reclaimable</span>
              <span className="fair-stat-sub red">
                {count(data.total_reclaimable_items)}{" "}
                {data.total_reclaimable_items === 1 ? "title" : "titles"} the scan would remove
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
