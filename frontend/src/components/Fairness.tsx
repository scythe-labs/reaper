// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Scales: where the disk went, and to whom.
//
// The screen you reach for when the question is not "what should I delete?" but "who is my
// library actually for?" One card per requester -- the balance bar weighs how much of the
// disk they were granted the last scan keeps against how much it would reclaim, and the
// watched % on the side says how much of what they asked for they used themselves.
//
// It deletes nothing. It reads the last scan, so it can never disagree with Review: a title
// is reclaimable here only when the scan condemns it, and each one opens its real card.

import { useQuery } from "@tanstack/react-query";
import { useState, type KeyboardEvent } from "react";
import { api, type ReclaimableTitle, type RequesterRow } from "../api";
import { bytes, count, date } from "../format";

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

/** The film-strip placeholder a title wears in the chip. */
function PosterMark() {
  return (
    <span className="mc-poster" aria-hidden="true">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none">
        <path d="M4 5h16v14H4z" stroke="currentColor" strokeWidth="1.6" />
        <path
          d="M8 5v14M16 5v14M4 9h4M16 9h4M4 15h4M16 15h4"
          stroke="currentColor"
          strokeWidth="1.2"
        />
      </svg>
    </span>
  );
}

/** A reclaimable title, carrying its size and a jump to its real card. Every entry is
 *  condemned by the last scan, so clicking it lands on the item (or show) showing that
 *  verdict -- no tab hunting, and nothing to guess. */
function TitleChip({
  title,
  onOpen,
}: {
  title: ReclaimableTitle;
  onOpen: (() => void) | null;
}) {
  const body = (
    <>
      <PosterMark />
      <span className="mc-body">
        <span className="mc-title">{title.title}</span>
        <span className="mc-state">Reclaimable · {bytes(title.size_bytes)}</span>
      </span>
    </>
  );
  if (!onOpen) return <span className="media-chip static">{body}</span>;
  return (
    <button
      type="button"
      className="media-chip"
      title="Open this in the review queue"
      // Stop the click from also toggling the card shut.
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      {body}
      <span className="mc-go" aria-hidden="true">
        ›
      </span>
    </button>
  );
}

/** One requester's card. Exported so its states (reclaimable / clean / expanded) can be
 *  tested against props without standing up the query. */
export function PersonCard({
  row,
  onOpenItem,
  onOpenGroup,
}: {
  row: RequesterRow;
  onOpenItem?: ((candidateId: number) => void) | undefined;
  onOpenGroup?: ((key: string) => void) | undefined;
}) {
  const [open, setOpen] = useState(false);
  const watched = watchedPct(row);
  const granted = row.gb_granted_bytes;
  const reclaim = row.reclaimable_bytes;
  const used = Math.max(0, granted - reclaim);
  // Granted can be zero (no size known for anything they asked for): show a full, neutral
  // bar rather than dividing by zero.
  const usedPct = granted > 0 ? (100 * used) / granted : 100;
  const reclaimPct = granted > 0 ? (100 * reclaim) / granted : 0;
  const hasReclaim = row.reclaimable_items > 0;
  // The list is capped server-side; the count stays exact, so name the shortfall.
  const moreCount = row.reclaimable_items - row.reclaimable.length;

  const toggle = () => {
    if (hasReclaim) setOpen((o) => !o);
  };
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  };

  const opener = (t: ReclaimableTitle): (() => void) | null => {
    if (t.item_id != null && onOpenItem) return () => onOpenItem(t.item_id as number);
    if (t.group_key != null && onOpenGroup) return () => onOpenGroup(t.group_key as string);
    return null;
  };

  return (
    <div
      className={`fair-card${hasReclaim ? " clickable" : ""}${open ? " open" : ""}`}
      {...(hasReclaim
        ? { role: "button", tabIndex: 0, "aria-expanded": open, onClick: toggle, onKeyDown }
        : {})}
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

        {open && hasReclaim && (
          <div className="fair-detail">
            <p className="fair-detail-lead">Requested, and the last scan would remove these:</p>
            <div className="media-row">
              {row.reclaimable.map((t, i) => (
                <TitleChip key={`${t.item_id ?? t.group_key}-${i}`} title={t} onOpen={opener(t)} />
              ))}
              {moreCount > 0 && <span className="mc-more">+{count(moreCount)} more not shown</span>}
            </div>
          </div>
        )}
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
  onOpenItem,
  onOpenGroup,
}: {
  /** Open one item's card (a movie or season) on the review screen. */
  onOpenItem?: (candidateId: number) => void;
  /** Open a whole show's panel on the review screen. */
  onOpenGroup?: (key: string) => void;
}) {
  const { data, isPending, error } = useQuery({ queryKey: ["fairness"], queryFn: api.fairness });

  return (
    <section className="fair">
      <div className="fair-head">
        <h2>Scales</h2>
        <p className="blurb">
          Who asked for what, and who actually watched it. Read only: nothing here removes
          anything.
        </p>
      </div>

      {error && <p className="notice notice-error">Couldn't load Scales: {error.message}</p>}
      {isPending && <p className="muted">Loading…</p>}

      {data?.no_snapshot && (
        <p className="empty">Run a scan first. Scales reads your last library scan.</p>
      )}

      {data && !data.no_snapshot && data.rows.length === 0 && (
        <p className="empty">No available requests are in the last scan yet.</p>
      )}

      {data && !data.no_snapshot && data.rows.length > 0 && (
        <>
          <div className="fair-stats">
            <div className="fair-stat">
              <span className="fair-stat-num">{count(data.total_requests)}</span>
              <span className="fair-stat-lbl">Requests</span>
              <span className="fair-stat-sub">across {count(data.rows.length)} people</span>
            </div>
            <div className="fair-stat">
              <span className="fair-stat-num red">{bytes(data.total_reclaimable_bytes)}</span>
              <span className="fair-stat-lbl">Reclaimable</span>
              <span className="fair-stat-sub red">
                {count(data.total_reclaimable_items)}{" "}
                {data.total_reclaimable_items === 1 ? "title" : "titles"} the scan would remove
              </span>
            </div>
            {data.not_in_scan > 0 && (
              <div className="fair-stat">
                <span className="fair-stat-num amber">{count(data.not_in_scan)}</span>
                <span className="fair-stat-lbl">Not in the last scan</span>
                <span className="fair-stat-sub">requested since, or filtered out</span>
              </div>
            )}
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
                key={row.name}
                row={row}
                onOpenItem={onOpenItem}
                onOpenGroup={onOpenGroup}
              />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
