// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Reap tab: plan on the left, audit on the right.
//
// Idle and blocked are one screen. Blocked is idle with the failing checks named in the
// summary card and the head Reap button disabled; nothing else changes. "Reap N titles…"
// builds the plan and opens the confirmation sheet (ReapConfirm.tsx) in one action, which is
// the one gauntlet that can start a deletion: a dry run, the armed check, and the typed
// content-bound phrase, all unchanged here. "Practice run" walks the same dry run standalone,
// with no sheet and no arming required, and reports pass or fail right here on the page.
//
// Every number in the idle summary (the four tiles, the reason bars, the ledger) comes from
// `useReapCounts` (ReapBreakdown.tsx), which reads GET /api/reap/breakdown once and adjusts
// it for the unknown-size allowance the same way everywhere it is shown, so the tiles and the
// ledger can never disagree about what a reap would actually remove.
//
// Reaping and done are the same page, fed by the shared `["reapStatus"]` poll every other
// reap surface reads (ReapBar.tsx, ReapConfirm.tsx): while it reports a run executing, the
// left column becomes a live dashboard; once it reports the run ended, the left column shows
// the result read back from what the run actually persisted (the history row's totals, the
// outcomes journal), never the in-memory report. ReapBar owns every post-run cache
// invalidation (rule 79); this page only reads.
//
// The history card is always visible: past reaps, a pinned non-clickable row for a run still
// executing, and paging through the whole executed history. Every other row opens a
// read-only detail sheet with the same persisted totals and the same outcomes journal.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLayoutEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import {
  api,
  type ReapStatus,
  type Run,
  type RunList,
  type RunOutcomeRead,
  type RunReport,
  type RunSummary,
} from "../api";
import { DegradedDocLink } from "../docs/DocLink";
import { describeError } from "../errors";
import { bytes, count, date, itemBytes } from "../format";
import { reapBlockers, type ReapBlocker } from "../reapReadiness";
import { useSafety } from "../useSafety";
import { composeError } from "../why";
import { ReapBreakdown, useReapCounts } from "./ReapBreakdown";
import { ReapConfirm } from "./ReapConfirm";
import { ModalShell } from "./ModalShell";
import { Notice } from "./Notice";
import { ackRun, useAckedRun } from "./runAck";
import { ScytheGlyph } from "./ScytheGlyph";

/** The link text beside one failing readiness check, naming where to go fix it. Written as a
 *  literal per key, like `reasonLabel` in ReapBreakdown.tsx, rather than composing a catalog
 *  key from `b.key` at runtime, which the i18n key gate cannot verify. */
function blockerFixLabel(key: ReapBlocker["key"], t: (k: string) => string): string {
  if (key === "password") return t("reapPlan.summary.blockers.fixPassword");
  if (key === "plex") return t("reapPlan.summary.blockers.fixPlex");
  if (key === "tautulli") return t("reapPlan.summary.blockers.fixTautulli");
  return t("reapPlan.summary.blockers.fixArr");
}

/** The history row's state dot. Only three colors are ever claimed as a real outcome
 *  (protect for a clean finish, amber for aborted, condemn for one still running); a plan
 *  never executed, or a state this build does not know, reads as neutral rather than being
 *  painted as any of the three, an older or newer build included. */
function runDotClass(state: string): string {
  if (state === "completed") return "reap-run-dot ok";
  if (state === "aborted") return "reap-run-dot bad";
  if (state === "executing") return "reap-run-dot live";
  return "reap-run-dot";
}

type TileKind = "titles" | "free" | "movies" | "seasons" | "kept";

/** A reap-page tile's icon, one per metric. The idle summary uses titles/free/movies/seasons;
 *  the done result reuses `free` and adds `kept`, a shield with a check, for the items a
 *  protection held. Decorative: the label beside it names the metric, so it carries
 *  `aria-hidden`. Color comes from the tile's rail (CSS), not from here. The reap glyph the
 *  done result's Removed tile wears is the shared `ScytheGlyph`, never redrawn here. */
function TileIcon({ kind }: { kind: TileKind }) {
  if (kind === "titles")
    return (
      <svg className="rt-ic" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M12 3l9 5-9 5-9-5 9-5Z"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
        <path
          d="M3 12l9 5 9-5M3 16l9 5 9-5"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
      </svg>
    );
  if (kind === "free")
    return (
      <svg className="rt-ic" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <ellipse cx="12" cy="6" rx="8" ry="3" stroke="currentColor" strokeWidth="1.6" />
        <path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6" stroke="currentColor" strokeWidth="1.6" />
        <path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" stroke="currentColor" strokeWidth="1.6" />
      </svg>
    );
  if (kind === "movies")
    return (
      <svg className="rt-ic" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" strokeWidth="1.6" />
        <path
          d="M7 4v16M17 4v16M3 8h4M17 8h4M3 12h18M3 16h4M17 16h4"
          stroke="currentColor"
          strokeWidth="1.3"
        />
      </svg>
    );
  if (kind === "kept")
    return (
      <svg className="rt-ic" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path
          d="M12 3l7 3v5c0 4-3 6.6-7 8-4-1.4-7-4-7-8V6l7-3Z"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
        <path
          d="M9 12l2 2 4-4"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  return (
    <svg className="rt-ic" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3" y="7" width="18" height="12" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M8 3l4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  );
}

/** One idle-summary tile: a colored rail and icon keyed to the metric, its label, then the big
 *  number. The rails read from the palette already in use: accent for the total, protect green
 *  for space freed, and the Radarr and Sonarr brand colors for movies and shows, so a tile's
 *  color says what it counts. */
function SummaryTile({ kind, label, value }: { kind: TileKind; label: string; value: string }) {
  return (
    <div className={`fair-stat rt rt-${kind}`}>
      <span className="rt-cap">
        <TileIcon kind={kind} />
        <span className="fair-stat-lbl">{label}</span>
      </span>
      <span className="fair-stat-num">{value}</span>
    </div>
  );
}

/** One row of past-reaps history. The run the status poll reports executing right now reads
 *  with the condemn "live" dot and stays a plain element: the live view of it is already this
 *  page's own left column, so opening a second, read-only copy of the same run would only
 *  ever show a stale echo of what is on screen already. Every other row is a button that
 *  opens the read-only detail sheet, and that includes a row STORED as executing that the
 *  poll does not claim: a crash mid-run leaves the row in that state forever, and pinning it
 *  as "running now" would both lie and close the only door to the record of what that run
 *  removed. Its dot is neutral, since "still executing" is exactly what cannot be claimed. */
function HistoryRow({
  run,
  liveRunId,
  onOpen,
}: {
  run: RunSummary;
  /** The run the shared status poll reports executing right now, or null. */
  liveRunId: number | null;
  onOpen: (run: RunSummary) => void;
}) {
  const { t } = useTranslation();
  const live = run.state === "executing" && run.id === liveRunId;
  const parts: string[] = [];
  if (live) {
    parts.push(t("reapPlan.history.runningNow"));
  } else if (run.deleted_items != null && run.deleted_bytes != null) {
    parts.push(
      t("reapPlan.history.freedRemoved", {
        bytes: bytes(run.deleted_bytes),
        count: count(run.deleted_items),
      }),
    );
  }
  if (!live && run.aborted_reason) parts.push(composeError(run.aborted_reason));

  const content = (
    <>
      <span
        className={runDotClass(run.state === "executing" && !live ? "" : run.state)}
        aria-hidden="true"
      />
      <span className="reap-run-what">
        {t("reapPlan.history.row", { id: run.id })}
        {/* A period between the freed/removed fragment and the aborted note, which is a
            whole sentence: a bare space runs "3 removed You stopped this run" together. */}
        {parts.length > 0 && <span className="reap-run-sub">{parts.join(". ")}</span>}
      </span>
      {/* `finished_at` is null before a run reaches a terminal state; `approved_at` (when it
          was created) is always set, and a real date beats none on a row that still has to
          show something on its right edge. */}
      <span className="reap-run-when">{date(run.finished_at ?? run.approved_at)}</span>
    </>
  );

  if (live) return <div className="reap-run">{content}</div>;
  return (
    <button type="button" className="reap-run" onClick={() => onOpen(run)}>
      {content}
    </button>
  );
}

//: One page of the run history. Growing `pages` re-fetches every page from offset 0 through
//: the newest one wanted, in parallel: a handful of small requests, always fresh, and never at
//: risk of an incremental cache reading stale after ReapBar's own invalidation of `["runs"]`
//: (a ref-cached "only fetch what's new" design would have to be told to forget itself on
//: every invalidation it did not cause, which is the bug this avoids by not existing).
const HISTORY_PAGE = 50;

async function fetchExecutedHistory(pages: number): Promise<RunList> {
  const responses = await Promise.all(
    Array.from({ length: pages }, (_, i) => api.runs(i * HISTORY_PAGE, HISTORY_PAGE, true)),
  );
  return {
    runs: responses.flatMap((r) => r.runs),
    total: responses.at(-1)?.total ?? 0,
  };
}

function useExecutedHistory() {
  const [pages, setPages] = useState(1);
  const query = useQuery({
    queryKey: ["runs", "executed", pages],
    queryFn: () => fetchExecutedHistory(pages),
  });
  return { ...query, showMore: () => setPages((p) => p + 1) };
}

//: The reaping card's item-status log: every outcome decided so far, held here and grown a
//: page at a time as the poll below finds more. Safe to keep an incremental cache across
//: polls, unlike the history list above: an item's outcome is decided once and never revised
//: (rule 112), so this can only ever grow, and nothing outside this hook ever needs to tell
//: it to forget what it already has.
const LIVE_OUTCOMES_PAGE = 50;

function useLiveOutcomesFeed(runId: number | null): { items: RunOutcomeRead[]; total: number } {
  const cacheRef = useRef<{ runId: number | null; items: RunOutcomeRead[] }>({
    runId: null,
    items: [],
  });

  const query = useQuery({
    queryKey: ["runOutcomesLive", runId],
    queryFn: async () => {
      if (runId == null) return { items: [] as RunOutcomeRead[], total: 0 };
      if (cacheRef.current.runId !== runId) cacheRef.current = { runId, items: [] };
      let total = 0;
      // Bounded: each page is small, and a 1s poll rarely lands more than one page
      // behind, but this still refuses to loop forever against a corrupted response.
      for (let guard = 0; guard < 20; guard += 1) {
        const page = await api.runOutcomes(
          runId,
          cacheRef.current.items.length,
          LIVE_OUTCOMES_PAGE,
        );
        total = page.outcome_count;
        if (page.outcomes.length === 0) break;
        cacheRef.current.items = cacheRef.current.items.concat(page.outcomes);
        if (cacheRef.current.items.length >= total) break;
      }
      return { items: cacheRef.current.items, total };
    },
    enabled: runId != null,
    refetchInterval: 1000,
  });

  return { items: query.data?.items ?? [], total: query.data?.total ?? 0 };
}

/** Every outcome a finished run has, fetched once. The first page alone (the API's own upper
 *  bound, 500) already covers almost every real run; only a plan bigger than that pages
 *  further. Shared by the done card's "Kept by checks" list and the read-only run detail
 *  sheet: both want the whole journal, neither is live. */
function useAllOutcomes(runId: number | null): { items: RunOutcomeRead[]; isPending: boolean } {
  const query = useQuery({
    queryKey: ["runOutcomesAll", runId],
    queryFn: async () => {
      if (runId == null) return [] as RunOutcomeRead[];
      const first = await api.runOutcomes(runId, 0, 500);
      if (first.outcomes.length >= first.outcome_count) return first.outcomes;
      const morePages = Math.ceil((first.outcome_count - first.outcomes.length) / 500);
      const rest = await Promise.all(
        Array.from({ length: morePages }, (_, i) =>
          api.runOutcomes(runId, first.outcomes.length + i * 500, 500),
        ),
      );
      return first.outcomes.concat(...rest.map((r) => r.outcomes));
    },
    enabled: runId != null,
  });
  return { items: query.data ?? [], isPending: query.isPending };
}

/** One outcome, in the grammar shared by the reaping card's live log, the done card's lists,
 *  and the read-only run detail sheet: a protect check for a confirmed removal (with its
 *  size), a red cross for a step that failed, an amber dot for an item a check kept, with the
 *  reason composed from its typed key (never matched against English, rule 92's sibling
 *  obligation for the browser side: the sentence is translated, so a string match on it would
 *  silently stop working the moment a translator touches it). The journal's `file_removed`
 *  stamp, not the state alone, decides the removal grammar: a FAILED step whose delete landed
 *  is a removed file with a problem after it, and calling it anything but removed would tell
 *  the operator a file that is off disk still exists. */
function OutcomeFeedRow({ outcome }: { outcome: RunOutcomeRead }) {
  const { t } = useTranslation();
  const reason = outcome.error_reason ? composeError(outcome.error_reason) : "";
  if (outcome.state === "verified") {
    return (
      <div className="feed-row gone">
        <span className="feed-mark" aria-hidden="true">
          ✓
        </span>
        {/* The glyph alone is not read by every screen reader at its default verbosity
            (ReapConfirm's own checklist carries the same note beside its ✓/✗ marks). */}
        <span className="sr-only">{t("reapPlan.itemStatus.removedSr")}</span>
        <span className="feed-title">{outcome.title || outcome.media_key}</span>
        <span className="feed-size">{itemBytes(outcome.size_bytes)}</span>
      </div>
    );
  }
  if (outcome.state === "failed" && outcome.file_removed) {
    return (
      <div className="feed-row gone">
        <span className="feed-mark" aria-hidden="true">
          ✓
        </span>
        <span className="feed-title">
          {outcome.title || outcome.media_key}
          <span className="feed-kept-why">
            {t("reapPlan.itemStatus.removedProblem", { reason })}
          </span>
        </span>
        <span className="feed-size">{itemBytes(outcome.size_bytes)}</span>
      </div>
    );
  }
  if (outcome.state === "failed") {
    return (
      <div className="feed-row failed">
        <span className="feed-mark" aria-hidden="true">
          ✕
        </span>
        <span className="feed-title">
          {outcome.title || outcome.media_key}
          <span className="feed-kept-why">{t("reapPlan.itemStatus.failedReason", { reason })}</span>
        </span>
      </div>
    );
  }
  return (
    <div className="feed-row kept">
      <span className="feed-mark" aria-hidden="true">
        •
      </span>
      <span className="feed-title">
        {outcome.title || outcome.media_key}
        <span className="feed-kept-why">{t("reapPlan.itemStatus.keptReason", { reason })}</span>
      </span>
    </div>
  );
}

/** The reaping card: live progress and the item-status log, both fed by the shared
 *  `["reapStatus"]` poll and the outcomes journal. Every number here is the server's own set
 *  (rule 5/30): nothing is derived locally. */
function ReapingCard({ status }: { status: ReapStatus }) {
  const { t } = useTranslation();
  const feed = useLiveOutcomesFeed(status.run_id);
  // Newest first: an item just decided is what a reader watching this log wants at the top.
  const displayed = [...feed.items].reverse();

  // Scroll position holds while rows arrive at the top, so a reader scrolled into the log
  // stays on the rows they were reading rather than being carried back to the newest one on
  // every poll tick.
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevHeightRef = useRef(0);
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const prevTop = el.scrollTop;
    if (prevTop > 0) el.scrollTop = prevTop + (el.scrollHeight - prevHeightRef.current);
    prevHeightRef.current = el.scrollHeight;
  }, [feed.items.length]);

  // `done` alone is the walk's own count over the confirmed set: the executor increments it
  // for every walked item whatever its outcome, vetoed ones included, so adding `skipped` on
  // top counts a mid-run veto twice and reads finished while files are still being removed.
  const handled = status.done;
  const pct = status.total > 0 ? Math.min(100, Math.round((handled / status.total) * 100)) : 0;
  // Every item is handled, but the run is still going: it is tidying Plex now (refreshing the
  // library so the deletes show, then the trash purge), which can take several seconds. Say so,
  // or the last "Now removing" line sits there looking hung through that wait.
  const finalizing = status.total > 0 && handled >= status.total;

  return (
    <>
      <div className="reap-card">
        <div className="fair-stats reap-tiles">
          <div className="fair-stat rt-freed">
            <span className="rt-cap">
              <TileIcon kind="free" />
              <span className="fair-stat-lbl">{t("reapPlan.tiles.freedSoFar")}</span>
            </span>
            <span className="fair-stat-num">{bytes(status.deleted_bytes)}</span>
          </div>
          <div className="fair-stat rt-removed">
            <span className="rt-cap">
              <ScytheGlyph className="rt-ic" strokeWidth={4.5} />
              <span className="fair-stat-lbl">{t("reapPlan.tiles.removed")}</span>
            </span>
            {/* `deleted_items`, the true removal count, never `done`: the walk's count also
                covers vetoed and failed items, so under the label "removed" it would claim
                removals that never happened. */}
            <span className="fair-stat-num">{count(status.deleted_items)}</span>
          </div>
          <div className="fair-stat rt-kept">
            <span className="rt-cap">
              <TileIcon kind="kept" />
              <span className="fair-stat-lbl">{t("reapPlan.tiles.keptByChecks")}</span>
            </span>
            <span className="fair-stat-num">{count(status.skipped)}</span>
          </div>
        </div>
        <div
          className="prog-track reap-plan-prog"
          role="progressbar"
          aria-label={t("reapConfirm.progress.ariaLabel")}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
          aria-valuetext={t("reapPlan.reaping.progressValueText", {
            done: count(handled),
            total: count(status.total),
          })}
        >
          <div className="prog-fill" style={{ width: `${pct}%` }} />
        </div>
        {finalizing ? (
          <p className="now-line finishing">
            <span className="spinner" aria-hidden="true" />
            {t("reapPlan.reaping.finishingUp")}
          </p>
        ) : status.title ? (
          <p className="now-line">{t("reapPlan.reaping.nowRemoving", { title: status.title })}</p>
        ) : null}
      </div>
      <div className="reap-card">
        {/* The decided count alone, with no "of {total}": an item spared before the run was
            claimed still gets a decided outcome, outside the confirmed set the total counts,
            so pairing the two can read "11 of 10". */}
        <h3 className="reap-feed-heading">
          {t("reapPlan.itemStatus.heading", { count: count(feed.total) })}
        </h3>
        {/* `tabIndex={0}`: every row is a `<span>`, nothing inside can take focus, so the box
            itself has to be a Tab stop or its scroll is unreachable by keyboard
            (a11y-scroll-reachable.test.ts). */}
        <div className="feed-scroll" ref={scrollRef} tabIndex={0}>
          <div className="feed">
            {displayed.map((o) => (
              <OutcomeFeedRow key={o.media_key} outcome={o} />
            ))}
          </div>
        </div>
        <p className="help reap-feed-help">{t("reapPlan.itemStatus.help")}</p>
      </div>
    </>
  );
}

/** A tile value that may not exist yet: a run's totals stay `null` until it reaches a
 *  terminal state (rule 17/36). The dash is `aria-hidden`, matching the same "no value yet"
 *  idiom the Lists panel's per-server tag counts use, so a screen reader hears an empty cell
 *  rather than a dash read out as a word. */
function TileValue({ value, format }: { value: number | null; format: (n: number) => string }) {
  if (value == null) return <span aria-hidden="true">—</span>;
  return <>{format(value)}</>;
}

/** The three persisted-totals tiles, shared by the done card and the run detail sheet. Reads
 *  the run row's own stored columns, never the in-memory report (rule 5/30, and what makes a
 *  reload, API restart included, still show the true result). */
function RunTotalsTiles({ run }: { run: RunSummary }) {
  const { t } = useTranslation();
  return (
    <div className="fair-stats reap-tiles">
      <div className="fair-stat rt-freed">
        <span className="rt-cap">
          <TileIcon kind="free" />
          <span className="fair-stat-lbl">{t("reapPlan.tiles.freed")}</span>
        </span>
        <span className="fair-stat-num">
          <TileValue value={run.deleted_bytes} format={bytes} />
        </span>
      </div>
      <div className="fair-stat rt-removed">
        <span className="rt-cap">
          <ScytheGlyph className="rt-ic" strokeWidth={4.5} />
          <span className="fair-stat-lbl">{t("reapPlan.tiles.removed")}</span>
        </span>
        <span className="fair-stat-num">
          <TileValue value={run.deleted_items} format={count} />
        </span>
      </div>
      <div className="fair-stat rt-kept">
        <span className="rt-cap">
          <TileIcon kind="kept" />
          <span className="fair-stat-lbl">{t("reapPlan.tiles.keptByChecks")}</span>
        </span>
        <span className="fair-stat-num">
          <TileValue value={run.skipped} format={count} />
        </span>
      </div>
    </div>
  );
}

/** The done card: the result of the run this page just watched end, read back from what it
 *  persisted rather than the in-memory report (rule 5/30). "Kept by checks" lists exactly the
 *  items a check kept (the same set the tile above counts), and a failed item gets its own
 *  list instead: filing it under "kept" would claim a protection fired when nothing was
 *  checked. Either list only renders while there is something in it, the same restraint
 *  `ReapBreakdown` uses for its own pointer cards. */
function DoneCard({ run, onDismiss }: { run: RunSummary; onDismiss: () => void }) {
  const { t } = useTranslation();
  const outcomes = useAllOutcomes(run.id);
  const decided = [...outcomes.items].reverse();
  const problems = decided.filter((o) => o.state === "failed");
  const kept = decided.filter((o) => o.state === "skipped");

  return (
    <>
      <div className="reap-card">
        <h3 className="reap-finished-head">{t("reapPlan.done.heading")}</h3>
        <RunTotalsTiles run={run} />
        {run.aborted_reason && (
          <p className="help reap-done-note">{composeError(run.aborted_reason)}</p>
        )}
        <div className="reap-done-actions">
          <button type="button" className="ghost" onClick={onDismiss}>
            {t("common.done")}
          </button>
        </div>
      </div>
      {problems.length > 0 && (
        <div className="reap-card">
          <h3 className="reap-feed-heading">{t("reapPlan.done.problemsHeading")}</h3>
          <div className="feed">
            {problems.map((o) => (
              <OutcomeFeedRow key={o.media_key} outcome={o} />
            ))}
          </div>
        </div>
      )}
      {kept.length > 0 && (
        <div className="reap-card">
          <h3 className="reap-feed-heading">{t("reapPlan.done.keptByChecksHeading")}</h3>
          <div className="feed">
            {kept.map((o) => (
              <OutcomeFeedRow key={o.media_key} outcome={o} />
            ))}
          </div>
        </div>
      )}
    </>
  );
}

/** The read-only run detail sheet, opened from a history row. The same persisted totals and
 *  the same outcomes journal as the done card, for any past run rather than only the one just
 *  watched. */
function RunDetailSheet({ run, onClose }: { run: RunSummary; onClose: () => void }) {
  const { t } = useTranslation();
  const outcomes = useAllOutcomes(run.id);
  const items = [...outcomes.items].reverse();

  return (
    <ModalShell
      title={t("reapPlan.history.row", { id: run.id })}
      onClose={onClose}
      className="run-detail"
    >
      <p className="help">{date(run.finished_at ?? run.approved_at)}</p>
      {/* `standing`: a fact about a persisted row, true for as long as the sheet shows this
          run, not a reply to anything pressed here. */}
      {run.aborted_reason && (
        <Notice tone="warn" standing>
          {composeError(run.aborted_reason)}
        </Notice>
      )}
      <div className="run-detail-stats">
        <RunTotalsTiles run={run} />
      </div>
      <h3 className="reap-feed-heading">{t("reapPlan.detail.itemStatus")}</h3>
      {/* `tabIndex={0}`: same reasoning as the reaping card's `.feed-scroll` above, and the
          same rows. */}
      {outcomes.isPending ? (
        <p className="muted">{t("common.loading")}</p>
      ) : items.length === 0 ? (
        <p className="help">{t("reapPlan.detail.noOutcomes")}</p>
      ) : (
        <div className="run-detail-scroll" tabIndex={0}>
          <div className="feed">
            {items.map((o) => (
              <OutcomeFeedRow key={o.media_key} outcome={o} />
            ))}
          </div>
        </div>
      )}
    </ModalShell>
  );
}

export function ReapPlan({
  onGoToSecurity,
  onGoToServices,
  onGoToPlexSettings,
  onGoToReview,
}: {
  /** Jump to Settings → Security, where the admin password is set. */
  onGoToSecurity: () => void;
  /** Jump to Settings → Services, where Radarr, Sonarr and Tautulli connect. */
  onGoToServices: () => void;
  /** Jump to Settings → Plex. */
  onGoToPlexSettings: () => void;
  /** Jump to the Review queue, where per-title decisions are made. */
  onGoToReview: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const safety = useSafety();
  const armed = safety.data?.destructive_enabled ?? false;

  const setup = useQuery({ queryKey: ["setup"], queryFn: api.setupStatus });
  const blockers: ReapBlocker[] = setup.data ? reapBlockers(setup.data) : [];
  // Empty while `setup` is still pending too, which is why `reapReady` below checks
  // `setup.isPending` on its own rather than trusting an empty `blockers` array: a read in
  // flight must never be read as every check having passed.
  const setupUnknown = setup.isError && !setup.data;

  // The planner refuses a degraded snapshot outright, so offering Reap over one only trades a
  // click for a 422. Said here, before the tiles below read as a list Reaper is ready to act
  // on. `retry: false`: a missing snapshot (no scan yet) is the normal empty state, not a
  // failure worth retrying.
  const snapshot = useQuery({ queryKey: ["snapshot"], queryFn: api.latestSnapshot, retry: false });
  const degraded = snapshot.data?.degraded === true;

  const counts = useReapCounts();
  const data = counts.data;

  // The shared reap-status poll: ReapBar and ReapConfirm read this exact key at this exact
  // cadence, so this page adds no second poll of its own to learn whether a run is executing.
  const reap = useQuery({
    queryKey: ["reapStatus"],
    queryFn: api.reapStatus,
    refetchInterval: (q) => (q.state.data?.running ? 1000 : 15000),
  });
  const status = reap.data;
  const reaping = status?.running === true;
  // Mirrors ReapBar's own "ended" derivation off the same poll (not off having watched the
  // run live from here): a reload, this page's first mount included, reads the same status
  // singleton ReapBar does and reaches the same answer. The dismissal is the shared,
  // persisted run ack (runAck.ts): pressing Done never touches the server, it hides this
  // run's result on both surfaces, across refreshes, until a different run ends.
  //
  // "error" is deliberately excluded, unlike ReapBar's own bar (which still has something to
  // show: a bare failure line). Usually that phase is a refusal raised before the run's own
  // row ever left PLANNED (a changed manifest, a changed policy), so it never gets the totals
  // this card reads, and `executed_only` leaves a still-PLANNED row out of the history list
  // entirely; showing this card for it would be a permanent "Loading…" over a run this page
  // can never find. A crash mid-run ends in the same phase with the row left EXECUTING and
  // no totals written, so the card has nothing to show for it either. That run's record stays
  // reachable through its history row, which opens like any other.
  const endedRunId =
    status &&
    !status.running &&
    status.run_id != null &&
    (status.phase === "complete" || status.phase === "aborted")
      ? status.run_id
      : null;
  const dismissedRunId = useAckedRun();
  const showDone = endedRunId != null && endedRunId !== dismissedRunId;

  const stop = useMutation({
    mutationFn: () => api.stopRun(status?.run_id ?? 0),
    onSuccess: (s) => queryClient.setQueryData(["reapStatus"], s),
  });

  const history = useExecutedHistory();
  const endedRun = history.data?.runs.find((r) => r.id === endedRunId) ?? null;

  const [detailRun, setDetailRun] = useState<RunSummary | null>(null);

  // The plan whose confirmation sheet is open, with the practice run it was proved by. Proving
  // before opening is what lets the sheet open at its settled content instead of into an empty
  // "checking" state (the wait lives on the head button's pending state, below).
  const [confirmRun, setConfirmRun] = useState<{ run: Run; report: RunReport } | null>(null);

  const reapReady =
    armed &&
    !setup.isPending &&
    !setup.isError &&
    !!setup.data &&
    blockers.length === 0 &&
    !degraded &&
    !counts.isPending &&
    !counts.isError &&
    !counts.allowanceUnknown &&
    counts.reapCount > 0;

  const createAndConfirm = useMutation({
    // Build the plan and prove it, both before the sheet opens, so it opens ready to confirm.
    // A practice run that fails to run at all surfaces here (createAndConfirm.error, below the
    // head), not inside an empty sheet; one that runs and stops opens the sheet at its stopped
    // message. Either way the sheet never opens mid-check.
    mutationFn: async () => {
      const run = await api.createRun("all");
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      const report = await api.dryRun(run.id);
      return { run, report };
    },
    onSuccess: (proved) => {
      // A standing practice result describes the plan as it stood before this reap, so
      // it must not survive into the run and greet the emptied plan afterward.
      practice.reset();
      setConfirmRun(proved);
    },
  });

  const practice = useMutation({
    mutationFn: async () => {
      const run = await api.createRun("all");
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      return api.dryRun(run.id);
    },
  });
  const practiceReady =
    !degraded && !counts.isPending && !!data?.has_snapshot && counts.reapCount > 0;

  const shown = history.data?.runs.length ?? 0;
  const historyTotal = history.data?.total ?? 0;

  return (
    <section className="reap">
      <div className="reap-head">
        <h2>{t("reapPlan.page.title")}</h2>
        {reaping ? (
          <div className="reap-head-actions">
            <button
              type="button"
              className="stop-btn"
              onClick={() => stop.mutate()}
              disabled={status?.stopping || stop.isPending}
            >
              {status?.stopping ? t("reapConfirm.stopping") : t("reapPlan.reaping.stop")}
            </button>
          </div>
        ) : (
          !showDone && (
            <div className="reap-head-actions">
              <button
                type="button"
                className="ghost"
                onClick={() => practice.mutate()}
                disabled={practice.isPending || !practiceReady}
              >
                {t("reapPlan.actions.practiceRun")}
              </button>
              <button
                type="button"
                className="danger fill"
                onClick={() => createAndConfirm.mutate()}
                disabled={createAndConfirm.isPending || !reapReady}
              >
                {createAndConfirm.isPending
                  ? t("common.planning")
                  : t("reapPlan.actions.reapButton", {
                      count: counts.reapCount,
                      n: counts.reapCount,
                    })}
              </button>
            </div>
          )
        )}
      </div>
      {reaping && stop.isError && (
        <Notice tone="error" className="reap-stop-error">
          {t("reapConfirm.bar.stopError", { error: describeError(stop.error) })}
        </Notice>
      )}

      <div className="reap-columns">
        <div className="reap-left">
          {reaping && status ? (
            <ReapingCard status={status} />
          ) : showDone ? (
            endedRun ? (
              <DoneCard run={endedRun} onDismiss={() => ackRun(endedRunId)} />
            ) : history.isError ? (
              // The result reads back from the history query, so a failed read is said
              // out loud rather than sitting on "Loading" until a refetch happens to
              // land, and Done stays reachable meanwhile.
              <div className="reap-card">
                <Notice tone="warn">{t("reapPlan.history.loadFailed")}</Notice>
                <div className="reap-done-actions">
                  <button type="button" className="ghost" onClick={() => ackRun(endedRunId)}>
                    {t("common.done")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="reap-card">
                <p className="muted">{t("common.loading")}</p>
              </div>
            )
          ) : (
            <>
              <div className="reap-card">
                {createAndConfirm.error && (
                  <Notice tone="error">{describeError(createAndConfirm.error)}</Notice>
                )}

                {counts.isPending ? (
                  <p className="muted">{t("common.loading")}</p>
                ) : counts.isError || !data ? (
                  <Notice tone="warn">{t("reapPlan.summary.loadFailed")}</Notice>
                ) : !data.has_snapshot ? (
                  <p className="muted">{t("reapPlan.summary.noScan")}</p>
                ) : (
                  <>
                    {degraded && (
                      // `standing`: `["snapshot"]` carries the last scan's own verdict on itself,
                      // so this is true before the page loads and stays true until a clean scan
                      // replaces it, never a reply to anything pressed here.
                      <Notice tone="warn" standing as="div" className="notice-doc">
                        <span>
                          <Trans
                            i18nKey="reapPlan.degraded.notice"
                            values={{ reason: snapshot.data?.degraded_reason ?? "" }}
                            components={{ strong: <strong /> }}
                          />
                        </span>
                        <DegradedDocLink doc={snapshot.data?.degraded_doc ?? null} />
                      </Notice>
                    )}

                    {counts.allowanceUnknown ? (
                      <Notice tone="warn">
                        {t("reapPlan.summary.allowanceUnknown", {
                          n: data.will_reap_unknown,
                          count: count(data.will_reap_unknown),
                        })}
                      </Notice>
                    ) : counts.reapCount === 0 ? (
                      <p className="rb-empty">
                        {data.will_reap > 0
                          ? t("reapPlan.summary.emptyMeasured")
                          : data.policy_condemned > 0 && data.hand_spared > 0
                            ? t("reapPlan.summary.emptySpared")
                            : t("reapPlan.summary.emptyNone")}
                      </p>
                    ) : (
                      <>
                        <div className="fair-stats reap-tiles">
                          <SummaryTile
                            kind="titles"
                            label={t("reapPlan.summary.tiles.titles")}
                            value={count(counts.reapCount)}
                          />
                          <SummaryTile
                            kind="free"
                            label={t("reapPlan.summary.tiles.toFree")}
                            value={bytes(counts.reapBytes)}
                          />
                          <SummaryTile
                            kind="movies"
                            label={t("reapPlan.summary.tiles.movies")}
                            value={count(counts.movies)}
                          />
                          <SummaryTile
                            kind="seasons"
                            label={t("reapPlan.summary.tiles.seasons")}
                            value={count(counts.seasons)}
                          />
                        </div>
                        <p className="help reap-summary-help">
                          {data.will_reap_unknown > 0 &&
                            (counts.holdsBackUnmeasured ? (
                              <>
                                {t("reapPlan.summary.help.heldBack", {
                                  count: data.will_reap_unknown,
                                  n: data.will_reap_unknown,
                                })}{" "}
                              </>
                            ) : (
                              <>
                                {t("reapPlan.summary.help.included", {
                                  count: data.will_reap_unknown,
                                  n: data.will_reap_unknown,
                                })}{" "}
                              </>
                            ))}
                          <Trans
                            i18nKey="reapPlan.summary.help.review"
                            components={{ btn: <button className="link" onClick={onGoToReview} /> }}
                          />
                        </p>
                      </>
                    )}

                    {/* The one blockers notice, from reapReadiness.ts: only the checks that fail,
                        each with a way to fix it. An unread setup state is its own line rather
                        than a claim any of the four passed. Nothing renders when all pass. */}
                    {setupUnknown ? (
                      // `standing`: `["setup"]` is the state of the read itself, true from the
                      // moment it fails until the next one lands, not a reply to a press here.
                      <Notice tone="warn" standing>
                        {t("reapPlan.summary.setupUnknown")}
                      </Notice>
                    ) : (
                      blockers.length > 0 && (
                        <Notice tone="warn" standing as="div">
                          <ul className="reap-blockers">
                            {blockers.map((b) => (
                              <li key={b.key}>
                                {b.sentence}{" "}
                                <button
                                  className="link"
                                  onClick={
                                    b.key === "password"
                                      ? onGoToSecurity
                                      : b.key === "plex"
                                        ? onGoToPlexSettings
                                        : onGoToServices
                                  }
                                >
                                  {blockerFixLabel(b.key, t)}
                                </button>
                              </li>
                            ))}
                          </ul>
                        </Notice>
                      )
                    )}
                  </>
                )}

                {practice.isPending && (
                  <div className="reap-practice-running">
                    <span className="spinner" aria-hidden="true" />
                    <span>{t("reapPlan.practiceRun.running")}</span>
                  </div>
                )}
                {practice.isError && (
                  <Notice tone="error">
                    {t("reapConfirm.practiceRun.failed", {
                      message: describeError(practice.error),
                    })}
                  </Notice>
                )}
                {practice.data && practice.data.state === "aborted" && (
                  <div className="sim sim-info">
                    <strong>{t("reapConfirm.practiceRun.stopped")}</strong>
                    <p>
                      {practice.data.aborted_reason && composeError(practice.data.aborted_reason)}
                    </p>
                  </div>
                )}
                {practice.data && practice.data.state !== "aborted" && (
                  <div className="reap-practice-pass">
                    {t("reapPlan.practiceRun.passed", {
                      count: practice.data.would_delete_items,
                      n: practice.data.would_delete_items,
                      bytes: bytes(practice.data.deleted_bytes),
                    })}
                    {practice.data.skipped > 0 &&
                      t("reapPlan.practiceRun.keptSuffix", { count: practice.data.skipped })}{" "}
                    <button type="button" className="link" onClick={() => practice.reset()}>
                      {t("reapPlan.practiceRun.dismiss")}
                    </button>
                  </div>
                )}
              </div>

              <ReapBreakdown onGoToReview={onGoToReview} />
            </>
          )}
        </div>

        <div className="reap-card reap-history">
          <h3>{t("reapPlan.history.heading")}</h3>
          {history.isPending ? (
            <p className="muted">{t("common.loading")}</p>
          ) : history.isError || !history.data ? (
            <Notice tone="warn">{t("reapPlan.history.loadFailed")}</Notice>
          ) : history.data.runs.length === 0 ? (
            <p className="muted">{t("reapPlan.history.empty")}</p>
          ) : (
            <>
              <div className="reap-runs">
                {history.data.runs.map((run) => (
                  <HistoryRow
                    key={run.id}
                    run={run}
                    liveRunId={reaping && status ? status.run_id : null}
                    onOpen={setDetailRun}
                  />
                ))}
              </div>
              <div className="reap-runs-foot">
                <span className="num">
                  {t("reapPlan.history.showingOf", {
                    shown: count(shown),
                    total: count(historyTotal),
                  })}
                </span>
                <button
                  type="button"
                  className="ghost"
                  onClick={history.showMore}
                  disabled={shown >= historyTotal}
                >
                  {t("reapPlan.history.showMore")}
                </button>
              </div>
            </>
          )}
        </div>
      </div>

      {confirmRun && (
        <ReapConfirm
          run={confirmRun.run}
          initialReport={confirmRun.report}
          onClose={() => setConfirmRun(null)}
        />
      )}
      {detailRun && <RunDetailSheet run={detailRun} onClose={() => setDetailRun(null)} />}
    </section>
  );
}
