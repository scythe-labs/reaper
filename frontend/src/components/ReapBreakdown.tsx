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
import { api, type SignalCount } from "../api";
import { bytes, count } from "../format";
import { useHoldsBackUnmeasured } from "./queueSettings";

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

  if (isPending) return <p className="muted">Loading…</p>;
  // An unreadable breakdown must never look like "nothing to reap": say we couldn't look,
  // in the amber tone, rather than rendering an empty ledger.
  if (isError || !data) {
    return (
      <p className="notice notice-warn">
        Couldn't load what a reap would remove. Reaper just can't show it right now. Reload to try
        again.
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
        <span className="rb-head">What this reap removes</span>
        <span className="rb-meta">
          {!allowanceUnknown && (
            <>
              <strong>{count(reapCount)}</strong> {reapCount === 1 ? "title" : "titles"} ·{" "}
            </>
          )}
          <strong>{bytes(data.will_reap_bytes)}</strong>
        </span>
      </div>
      <p className="rb-sub">
        Your policy's verdict from the last scan, with your own changes on top.
      </p>

      {allowanceUnknown ? (
        <>
          <p className="notice notice-warn">
            Reaper couldn't check your unknown-size allowance, so it can't say how many titles this
            reap removes. {plural(data.will_reap_unknown, "title", "titles")} on the list can't be
            measured. Reload to try again.
          </p>
          <Reasons rows={data.condemned_by} anchor={data.policy_condemned} />
        </>
      ) : reapCount === 0 ? (
        <p className="rb-empty">
          A reap would remove nothing right now.
          {data.will_reap > 0
            ? " Reaper couldn't measure any of the titles on the list, so it won't remove them."
            : data.policy_condemned > 0 && data.hand_spared > 0
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
              <span className="rb-n">{count(reapCount)}</span>
              <span className="rb-sz">{bytes(data.will_reap_bytes)}</span>
            </div>
          </div>
          <div className="rb-split">
            {plural(movies, "movie", "movies")} · {plural(seasons, "TV season", "TV seasons")} ·
            smallest first, test item first.
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
        <p className="notice notice-warn">
          {/* Titles, not spares: the server counts the rows a scan would hand back, and one
              whole-show spare can be holding five condemned seasons. Calling those "5 spares"
              named a thing the operator has one of (rule 21). */}
          <strong>
            {plural(data.spares_expired, "title is", "titles are")} kept by{" "}
            {data.spares_expired === 1 ? "a spare that expired" : "spares that expired"}.
          </strong>{" "}
          This reap won't remove {data.spares_expired === 1 ? "it" : "them"}. A new scan judges{" "}
          {data.spares_expired === 1 ? "it" : "them"} again.{" "}
          <button
            className="link"
            onClick={() => startScan.mutate()}
            disabled={startScan.isPending || scanning}
          >
            {scanning ? "Scanning…" : startScan.isPending ? "Starting…" : "Scan now"}
          </button>
        </p>
      )}
      {/* Its own notice in the shared error tone, not red text inside the warning above:
          every action failure in the app reads the same way (rule 42). */}
      {data.spares_expired > 0 && startScan.isError && (
        <p className="notice notice-error">The scan didn't start. Try again.</p>
      )}
      {data.hand_reaped_held > 0 && (
        <div className="rb-line">
          {/* "this reap", not "a scan": a scan never removes anything, and the Jobs page
              says exactly that in the same product ("A scan only reads. It cannot
              delete."). What holds these back is the reap this page is about (U-10). */}
          {plural(data.hand_reaped_held, "reap you marked is", "reaps you marked are")} on hold, so
          this reap won't remove {data.hand_reaped_held === 1 ? "it" : "them"} yet.{" "}
          <button className="link" onClick={onGoToReview}>
            See Review →
          </button>
        </div>
      )}
      {showUnmeasuredLine &&
        (holdsBackUnmeasured ? (
          <div className="rb-line">
            {plural(data.will_reap_unknown, "title", "titles")} can't be measured, so Reaper won't
            remove {data.will_reap_unknown === 1 ? "it" : "them"}.
          </div>
        ) : (
          <div className="rb-line">
            {plural(data.will_reap_unknown, "title", "titles")} can't be measured. These are only
            removed within your unknown-size allowance.
          </div>
        ))}
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
