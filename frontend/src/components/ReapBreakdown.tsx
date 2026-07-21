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

import { useQuery } from "@tanstack/react-query";
import { api, type SignalCount } from "../api";
import { bytes, count } from "../format";

// Built-in signals read as a policy question in the editor ("How long it's gone
// unwatched"); here they name the reason a title was condemned, so they get their own
// short phrasing. A custom rule has no entry and shows under its own name.
const REASON_LABEL: Record<string, string> = {
  unwatched: "Gone unwatched too long",
  few_watchers: "Few or no watchers",
  season_rank: "An older season",
  low_rating: "Low rating",
  size: "Large file on disk",
};

function reasonLabel(id: string): string {
  return REASON_LABEL[id] ?? id;
}

function plural(n: number, one: string, many: string): string {
  return `${count(n)} ${n === 1 ? one : many}`;
}

function Reasons({ rows, anchor }: { rows: SignalCount[]; anchor: number }) {
  if (rows.length === 0) return null;
  const max = Math.max(...rows.map((r) => r.count), 1);
  return (
    <div className="rb-reasons">
      <div className="rb-reasons-head">
        <h3>Why your policy condemned them</h3>
        <span className="rb-of">of {plural(anchor, "title", "titles")}</span>
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
      <p className="rb-note">
        Titles usually trip more than one, so these overlap and add up to more than the total.
      </p>
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
  const { data, isPending, isError } = useQuery({
    queryKey: ["reap-breakdown"],
    queryFn: api.reapBreakdown,
  });

  if (isPending) return <p className="muted">Loading…</p>;
  // An unreadable breakdown must never look like "nothing to reap": say we couldn't look,
  // in the amber tone, rather than rendering an empty ledger.
  if (isError || !data) {
    return (
      <p className="notice notice-warn">
        Couldn't load what a reap would remove. Reaper just can't show it right now. Reload to
        try again.
      </p>
    );
  }

  if (!data.has_snapshot) {
    return (
      <div className="reap-breakdown">
        <div className="rb-head">What this reap removes</div>
        <p className="rb-sub">No scan yet. Run one, and this shows what a reap would remove.</p>
      </div>
    );
  }

  const overrides = data.hand_spared > 0 || data.hand_reaped > 0;

  return (
    <div className="reap-breakdown">
      <div className="rb-headline">
        <span className="rb-head">What this reap removes</span>
        <span className="rb-meta">
          <strong>{count(data.will_reap)}</strong>{" "}
          {data.will_reap === 1 ? "title" : "titles"} · <strong>{bytes(data.will_reap_bytes)}</strong>
        </span>
      </div>
      <p className="rb-sub">Your policy's verdict from the last scan, with your own changes on top.</p>

      {data.will_reap === 0 ? (
        <p className="rb-empty">
          A reap would remove nothing right now.
          {data.policy_condemned > 0 && data.hand_spared > 0
            ? " You've spared everything the last scan condemned."
            : " The last scan condemned nothing."}
        </p>
      ) : (
        <>
          <div className="rb-ledger">
            {overrides && (
              <>
                <div className="rb-row">
                  <span className="rb-lab">Condemned by your policy</span>
                  <span className="rb-n">{count(data.policy_condemned)}</span>
                  <span className="rb-sz">{bytes(data.policy_condemned_bytes)}</span>
                </div>
                {data.hand_spared > 0 && (
                  <div className="rb-row rb-spare">
                    <span className="rb-lab">You spared by hand</span>
                    <span className="rb-n">− {count(data.hand_spared)}</span>
                    <span className="rb-sz">kept</span>
                  </div>
                )}
                {data.hand_reaped > 0 && (
                  <div className="rb-row rb-add">
                    <span className="rb-lab">You marked to reap by hand</span>
                    <span className="rb-n">+ {count(data.hand_reaped)}</span>
                    <span className="rb-sz">{bytes(data.hand_reaped_bytes)}</span>
                  </div>
                )}
                <div className="rb-rule" />
              </>
            )}
            <div className="rb-row rb-total">
              <span className="rb-lab">Will be reaped</span>
              <span className="rb-n">{count(data.will_reap)}</span>
              <span className="rb-sz">{bytes(data.will_reap_bytes)}</span>
            </div>
          </div>
          <div className="rb-split">
            {plural(data.movies, "movie", "movies")} · {plural(data.seasons, "TV season", "TV seasons")}{" "}
            · smallest first, test item first.
          </div>

          <Reasons rows={data.condemned_by} anchor={data.policy_condemned} />
        </>
      )}

      {data.will_reap_unknown > 0 && (
        <div className="rb-line">
          {plural(data.will_reap_unknown, "title", "titles")} can't be measured, so Reaper won't
          remove {data.will_reap_unknown === 1 ? "it" : "them"}.
        </div>
      )}
      <div className="rb-line">
        Warning your Plex users first? “Leaving Soon” is in{" "}
        <button className="link" onClick={onGoToPlexSettings}>
          Settings → Plex
        </button>
        .
      </div>
      <div className="rb-line">
        To spare a title or change a decision, open the{" "}
        <button className="link" onClick={onGoToReview}>
          Review queue →
        </button>
      </div>
    </div>
  );
}
