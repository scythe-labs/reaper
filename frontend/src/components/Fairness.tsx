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
import { type KeyboardEvent } from "react";
import { api, type RequesterRow } from "../api";
import { bytes, count, date } from "../format";

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
 *  (did the asker use what they asked for), kept apart from the disk balance below. */
function watchedPct(row: RequesterRow): number {
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
}: {
  row: RequesterRow;
  selected: boolean;
  onSelect: (identity: string) => void;
}) {
  const watched = watchedPct(row);
  const granted = row.gb_granted_bytes;
  const reclaim = row.reclaimable_bytes;
  const used = Math.max(0, granted - reclaim);
  // Granted can be zero (no size known for anything they asked for): show a full, neutral
  // bar rather than dividing by zero.
  const usedPct = granted > 0 ? (100 * used) / granted : 100;
  const reclaimPct = granted > 0 ? (100 * reclaim) / granted : 0;
  const hasReclaim = row.reclaimable_items > 0;

  const open = () => onSelect(row.identity);
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      open();
    }
  };

  return (
    <div
      className={`fair-card clickable${selected ? " selected" : ""}`}
      role="button"
      tabIndex={0}
      aria-label={`Open ${row.name}'s request breakdown`}
      onClick={open}
      onKeyDown={onKeyDown}
    >
      <span className="fair-avatar" aria-hidden="true">
        {initial(row.name)}
      </span>

      <div className="fair-body">
        <div className="fair-row1">
          <span className="fair-name">{row.name}</span>
          <span className="fair-sub">
            <strong>{count(row.requests_made)}</strong> requests ·{" "}
            <strong>{bytes(granted)}</strong> granted
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
        <span className="fair-watched">
          <span className={`fair-pct ${watched >= 50 ? "good" : "bad"}`}>{watched}%</span>
          <span className="fair-pct-lbl">they watched</span>
        </span>
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
      aria-expanded={unmatchedSelected ?? false}
    >
      <span className="fair-stat-num amber">{count(data.not_in_scan)}</span>
      <span className="fair-stat-lbl">Not in the last scan</span>
      <span className="fair-stat-sub">requested since, or filtered out</span>
      <span className="fair-stat-more">See what these are ›</span>
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
          Who asked for what, and who actually watched it. Read only: nothing here removes
          anything.
        </p>
      </div>

      {error && <p className="notice notice-error">Couldn't load Scales: {error.message}</p>}
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

          {data.horizon_at && (
            <p className="fair-horizon muted">
              Watch history reaches back to {date(data.horizon_at)}, so older plays are invisible
              here.
            </p>
          )}

          <div className="fair-list">
            {data.rows.map((row) => (
              <PersonCard
                key={row.identity}
                row={row}
                selected={row.identity === selectedIdentity}
                onSelect={select}
              />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
