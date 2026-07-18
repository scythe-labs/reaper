// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The fairness view: where the disk went, and to whom.
//
// This is the screen you reach for when the question is not "what should I delete?" but
// "who is my library actually for?" Per person: how many titles they asked for, how much
// disk that granted, how much of it they ever played, and how much nobody watched.
//
// It deletes nothing. It is a report -- a conversation to have, not a run to fire. The
// reclaimable column is what the requester rule would flag, shown here as information so
// the actual removal still goes through the reviewed scan and plan.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type RequesterRow } from "../api";
import { bytes, count, date } from "../format";

function pct(row: RequesterRow): number {
  if (row.requests_made === 0) return 0;
  return Math.round((100 * row.played_by_them) / row.requests_made);
}

function Row({
  row,
  onOpenInQueue,
}: {
  row: RequesterRow;
  onOpenInQueue?: ((title: string) => void) | undefined;
}) {
  const [open, setOpen] = useState(false);
  const played = pct(row);
  const hasUnwatched = row.unwatched_titles.length > 0;

  return (
    <>
      <tr className={open ? "selected" : ""}>
        <td className="title-cell">{row.name}</td>
        <td className="num">{count(row.requests_made)}</td>
        <td className="num">{bytes(row.gb_granted_bytes)}</td>
        <td className="num">
          <span className={played >= 50 ? "played-good" : "played-low"}>{played}%</span>
        </td>
        <td className="num">
          {row.reclaimable_items > 0 ? (
            <span className="reclaimable-cell">
              {count(row.reclaimable_items)} · {bytes(row.reclaimable_bytes)}
            </span>
          ) : (
            <span className="muted">·</span>
          )}
        </td>
        <td className="why-cell">
          {hasUnwatched && (
            // "show"/"hide" reads fine beside its own row, but every row's button says the
            // same thing, so the label spells out whose titles it opens.
            <button
              className="link"
              aria-expanded={open}
              aria-label={`${open ? "Hide" : "Show"} unwatched titles requested by ${row.name}`}
              onClick={() => setOpen((o) => !o)}
            >
              {open ? "hide" : "show"}
            </button>
          )}
        </td>
      </tr>
      {open && hasUnwatched && (
        <tr className="fairness-detail">
          <td colSpan={6}>
            <p className="muted">
              Requested by {row.name}, available long enough to judge, and never played by
              anyone:
            </p>
            <ul className="unwatched-titles">
              {row.unwatched_titles.map((t) => (
                // The next question is always "show me that one", so the title is the way
                // there. Without a jump handler it stays plain text rather than a dead link.
                <li key={t}>
                  {onOpenInQueue ? (
                    <button
                      className="link"
                      title="Find this in the review queue"
                      onClick={() => onOpenInQueue(t)}
                    >
                      {t}
                    </button>
                  ) : (
                    t
                  )}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

export function Fairness({
  onOpenInQueue,
}: {
  /** Jump to the review queue with this title already searched for. */
  onOpenInQueue?: (title: string) => void;
}) {
  const { data, isPending, error } = useQuery({ queryKey: ["fairness"], queryFn: api.fairness });

  return (
    <section className="fairness">
      <div className="fairness-head">
        <h2>Fairness</h2>
        <p className="blurb">
          Who asked for what, and who actually watched it. Read-only. Nothing here deletes
          anything; it is the picture behind the requests.
        </p>
      </div>

      {error && (
        <p className="notice notice-error">
          Couldn't load the fairness report: {error.message}
        </p>
      )}
      {isPending && <p className="muted">Loading…</p>}

      {data && (
        <>
          <p className="queue-total">
            <strong>{count(data.total_requests)}</strong> available requests across{" "}
            <strong>{count(data.rows.length)}</strong> people ·{" "}
            <strong>{count(data.total_reclaimable_items)}</strong> reclaimable (
            {bytes(data.total_reclaimable_bytes)})
            {data.unmatched_requests > 0 && (
              <span className="muted">
                {" "}
                · {count(data.unmatched_requests)} could not be judged (they couldn't be
                found in Plex)
              </span>
            )}
            {data.horizon_at && (
              <span className="muted">
                {" "}
                · watch history reaches back to {date(data.horizon_at)}, so older plays are
                invisible here
              </span>
            )}
          </p>
          {/* Six columns of names and numbers do not fit a phone. The wrapper keeps that
              scroll sideways inside the table instead of pushing the whole page sideways. */}
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Requester</th>
                  <th className="num">Requests</th>
                  <th className="num">Granted</th>
                  <th className="num">Played</th>
                  <th className="num">Reclaimable</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <Row key={row.name} row={row} onOpenInQueue={onOpenInQueue} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
