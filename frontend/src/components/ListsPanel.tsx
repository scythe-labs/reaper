// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Lists. Every protection list, whether each is still protecting anything, and
// the controls to add, edit and check them (#475).
//
// **Settings owns what a list IS and where it comes from; Policy owns what it does.** Nothing
// is configured in two places: what a list does is a keep rule on Policy naming it, and each
// row here says how the policies are using it right now (`policy_use`), because a defined
// list no rule names is a list that protects nothing.
//
// **One row per definition, never one per stored list.** Reaper stores a tag list per *arr
// instance, so two Radarrs and two Sonarrs are four stored rows for the single thing the
// operator did. They collapse into one row carrying the family's worst state, which names only
// the servers that need attention. The join is `ProtectionList.list_id`, derived on the server
// beside the slug spellings; this component never parses a slug (rule 63).
//
// The verdict is the server's (`lists.ListHealth`), not this component's. Two surfaces deciding
// for themselves what `last_error` plus `last_synced_at` means is how the screen and the scan
// end up telling the operator different stories about one failed check (rule 144). What is
// decided here is only the sentence.
//
// The row grammar is the Jobs tab's (`.jobrow`), not a second settings-row shape. Both pages
// answer the same question -- is this thing working, and when did it last run -- so a reader
// moving between them should not have to learn two layouts (rule 18).

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useRef, useState, type ReactNode } from "react";

import { announce } from "../announce";
import { api, type ListConfig, type ListPolicyUse, type ProtectionList } from "../api";
import { useBackGuard } from "../backnav";
import { ListModal } from "./ListModal";
import { Notice } from "./Notice";
import { RESCAN_HEADING, RESCAN_QUEUED_LEAD } from "./PolicySimulator";

/** How long ago, in the app's usual plain phrasing. Null stamps are handled by the caller,
 *  which has a whole sentence to say about a list that has never checked in. */
function ago(iso: string): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function titles(n: number): string {
  return `${n.toLocaleString()} ${n === 1 ? "title" : "titles"}`;
}

type Tone = "ok" | "warn" | "bad" | "idle";
type State = ProtectionList["state"];

/** This screen's four states in the app's ONE chip family (`.status-chip`,
 *  23-queue-chips.css). They were a `.list-state` family of their own re-declaring the same
 *  token pairs, which rules 18 and 72 refuse: a color meaning "kept" has to move everywhere at
 *  once. Worn with `.status-chip-wrap`, the family's non-truncating variant, because these
 *  labels are not in a fixed column here. */
const STATE_CHIP: Record<Tone, string> = {
  ok: "status-kept",
  warn: "status-warn",
  bad: "status-pressure",
  idle: "status-quiet",
};

/** Worst first. A group wears its worst member's state, because a family reported by its best
 *  member is a family that says "Working" while one server's tag list protects nothing. */
const SEVERITY: State[] = ["failing", "never_checked", "stale", "working"];

function worst(rows: ProtectionList[]): State {
  return SEVERITY.find((s) => rows.some((r) => r.state === s)) ?? "working";
}

/** The chip and the sentence under it, for one row or one collapsed family. */
function describe(
  state: State,
  items: number,
  servers?: string[],
): { label: string; tone: Tone; detail: string } {
  const named = servers?.length ? ` ${servers.join(", ")}.` : "";

  // Emptiness is checked BEFORE the state, because a successful check over a list holding
  // nothing is the one combination where the server's verdict and the operator's question come
  // apart. The check worked, so `working` is true about the sync -- and rendering that green
  // tells someone whose keep list covers nothing that they are covered. Found by driving a real
  // install: a "Never Reap" collection sat green at 0 titles, which is indistinguishable from
  // the collection being read out of the wrong library (#483) or holding only entries Reaper
  // cannot identify (#474). Green there is the reassuring direction, which is the direction
  // this codebase must not fail in.
  if (items === 0 && (state === "working" || state === "stale")) {
    return {
      label: "Nothing on it",
      tone: "idle",
      detail:
        "The last check worked, but nothing is on this list, so it is protecting nothing. " +
        "Check the list itself, and that Reaper is reading the one you meant.",
    };
  }
  switch (state) {
    case "working":
      return { label: "Working", tone: "ok", detail: `Protecting ${titles(items)}.` };
    case "stale":
      // Names its members like `failing` and `never_checked` do. It was the one state that
      // took `servers` and ignored it, so a family out of date on one server of four said
      // something was wrong and not which one -- and "which one" is the whole reason a
      // rolled-up row names anybody.
      return {
        label: "Out of date",
        tone: "warn",
        detail:
          `Still protecting ${titles(items)}, but the last check was too long ago. ` +
          "Anything added since then is not covered yet." +
          named,
      };
    case "failing":
      return {
        label: "Not working",
        tone: "bad",
        detail:
          (items > 0
            ? `The last check failed. The ${titles(items)} from the last good check are still ` +
              "protected, and anything added since then is not."
            : "The last check failed and nothing is stored, so this is protecting nothing.") +
          named,
      };
    case "never_checked":
      // Branched on the stored count, the way `failing` above is. A rolled-up family takes
      // its state from its WORST member, and `never_checked` outranks `working`, so adding a
      // second *arr to a tag list that already holds titles lands here with `items` above
      // zero -- and the flat sentence then told the operator a live protection was not
      // protecting, on the screen built to answer exactly that.
      return {
        label: "Not checked yet",
        tone: "idle",
        detail:
          (items > 0
            ? `Still protecting ${titles(items)} from an earlier check. Anything added since ` +
              "then is not covered until the next one runs."
            : "Runs with your next scan. Nothing on it is protected until it does.") + named,
      };
  }
}

/** "Radarr (4k) tag: reaper-keep" -> "Radarr (4k)". The fallback when a stored row predates
 *  the `server` field: the collapsed family still has to name which member needs attention.
 *  Falls back to the whole string, so a display name this does not recognize is still named
 *  rather than dropped. */
function serverOf(row: ProtectionList): string {
  if (row.server) return row.server;
  const cut = row.name.indexOf(" tag:");
  return cut === -1 ? row.name : row.name.slice(0, cut);
}

/** The kind badge beside the name: which family the list reads from, in each service's own
 *  colors. A tag list reads Sonarr and Radarr at once, so its pill is half of each. */
function kindBadge(source: ListConfig["source"]): ReactNode {
  if (source === "arr_tag") {
    // One span per half, so each name is centered in its own color instead of riding a
    // gradient stop that falls near, but not on, the space between them.
    //
    // The halves are hidden from a reader and the name is said once, in text: the two spans
    // are flex items, and whether the whitespace between two flex items reaches the
    // accessibility tree is not something to bet a name on -- a whitespace-only anonymous
    // item generates no box at all. Said outright, it cannot come out as one invented word,
    // and the claim does not rest on layout behavior no test here can observe.
    return (
      <span className="kind-badge kind-arr">
        <span aria-hidden="true">Sonarr</span>
        <span aria-hidden="true">Radarr</span>
        <span className="sr-only">Sonarr and Radarr</span>
      </span>
    );
  }
  if (source === "imdb") return <span className="kind-badge kind-imdb">IMDb</span>;
  return <span className="kind-badge kind-plex">Plex</span>;
}

/** One rendered row: the Jobs tab's shape, with the state chip on the title line and real
 *  actions where a job's buttons sit. Check now is the rightmost action on every row. */
function ListRow({
  title,
  badge,
  detail,
  label,
  tone,
  meta,
  error,
  checking,
  onCheck,
  onEdit,
  checkError,
  children,
}: {
  title: string;
  badge?: ReactNode;
  detail: string;
  label: string;
  tone: Tone;
  meta: string | null;
  error?: string | null;
  checking: boolean;
  onCheck: () => void;
  /** Absent for a row stored before its definition existed, which has nothing to edit. */
  onEdit?: (() => void) | undefined;
  /** Why the last check pressed on THIS row failed. Rendered here rather than in a page-level
   *  slot, because it is about this list and the button that retries it is on this row
   *  (rule 42). */
  checkError?: string | null | undefined;
  /** The row's own extras: tag pills, the per-server fold-out, the policy-use line. */
  children?: ReactNode;
}) {
  return (
    <div className="jobrow">
      <div className="jobrow-main">
        {/* The name arrives from Plex or an *arr, or from the operator's own keyboard, so it
            wraps rather than running through the box holding it (rule 139). */}
        <div className="list-head">
          <span className="jobrow-title list-name">{title}</span>
          {badge}
          <span className={`status-chip status-chip-wrap ${STATE_CHIP[tone]}`}>{label}</span>
        </div>
        <div className="jobrow-desc">{detail}</div>
        {children}
        {meta !== null && <div className="jobrow-sched">{meta}</div>}
        {/* The service's own words, which name the thing to go and fix: a library not called
            what Reaper asked for, a tag that was renamed. Also from outside the app. */}
        {error != null && <div className="jobrow-desc list-error">{error}</div>}
        {checkError != null && (
          <Notice tone="error" inline>
            The check didn't run: {checkError}
          </Notice>
        )}
      </div>
      <div className="jobrow-actions">
        <span className="slot-edit">
          {onEdit && (
            <button className="ghost" aria-label={`Edit ${title}`} onClick={onEdit}>
              Edit
            </button>
          )}
        </span>
        <span className="slot-act">
          <button
            className="primary"
            // The state is in the accessible name and the visible words lead, so voice
            // control still reaches the button while it is working (the shape JobRow uses).
            aria-label={checking ? `Checking…, ${title}` : `Check now, ${title}`}
            onClick={onCheck}
            disabled={checking}
          >
            {checking ? "Checking…" : "Check now"}
          </button>
        </span>
      </div>
    </div>
  );
}

/** The tag pills and the per-server fold-out, for a tag list's row. Counts are summed across
 *  the family's rows -- one per *arr instance -- and a tag no row has counted yet renders as
 *  the bare pill, never as zero (the counts are unknown, not empty). */
function TagCounts({ definition, mine }: { definition: ListConfig; mine: ProtectionList[] }) {
  const tags = definition.config.tags ?? [];
  if (tags.length === 0) return null;
  const counted = mine.filter(
    (r): r is ProtectionList & { tags: Record<string, number> } => r.tags !== null,
  );
  const total = (tag: string): number | null => {
    const holders = counted.filter((r) => r.tags[tag] !== undefined);
    if (holders.length === 0) return null;
    return holders.reduce((n, r) => n + (r.tags[tag] ?? 0), 0);
  };
  // Only servers with something to say about THESE tags: a stats body written before the
  // counts existed, or for tags since renamed, would otherwise render a server name beside
  // an empty count column.
  const perServer = counted.filter(
    (r) => r.server !== null && tags.some((tag) => r.tags[tag] !== undefined),
  );
  return (
    <>
      <div className="tag-pills">
        {tags.map((tag) => {
          const n = total(tag);
          return (
            // One text run, not two flex items. As flex children the tag and its count had
            // nothing between them at all, so the pill read and COPIED as "reaper-keep412".
            // A literal space rather than a gap, so the separation is in the text itself and
            // does not depend on how whitespace between flex items is treated.
            <span key={tag} className="tag-pill">
              {tag} {n !== null && <b>{n.toLocaleString()}</b>}
            </span>
          );
        })}
      </div>
      {perServer.length > 0 && (
        <details className="per-server">
          <summary>Counts by server</summary>
          <div className="server-grid">
            {perServer.map((r) => (
              <Fragment key={r.slug}>
                <span className="srv">{r.server}</span>
                <span className="cnt">
                  {tags
                    .filter((tag) => r.tags[tag] !== undefined)
                    .map((tag) => `${tag} ${r.tags[tag]!.toLocaleString()}`)
                    .join(", ")}
                </span>
              </Fragment>
            ))}
          </div>
        </details>
      )}
    </>
  );
}

/** How the policies use this list, under every row: the strength of each rule naming it, or
 *  the warning that none does. Sentences come from `policy_use` deduplicated, because the
 *  default rules name a list once per media type and one fact is said once. */
function PolicyUse({
  definition,
  onGoToPolicy,
}: {
  definition: ListConfig;
  onGoToPolicy?: (() => void) | undefined;
}) {
  // An outright rule decides the item on its own, so a lean beside it never changes an
  // outcome and saying both described one list as doing two things (#510). Policy will not
  // compose the pair any more; a body stored before it could still carry one, and this is
  // what the operator is told about it: the strength that actually acts.
  //
  // WITHIN ONE MEDIA TYPE. Collapsed across both, a list keeping movies outright while only
  // leaning on TV read "Keeps every title on it" and dropped the lean entirely -- a movie
  // rule decides nothing about a season, so the operator lost the line saying their shows
  // on that list are still condemnable, on the screen built to answer that.
  const acting = (["movie", "tv"] as const).flatMap((mediaType) => {
    const mine = definition.policy_use.filter((use) => use.media_type === mediaType);
    const use = mine.find((u) => u.strength === "hard") ?? mine[0];
    return use ? [{ noun: mediaType === "movie" ? "movie" : "show", use } as const] : [];
  });
  const said = (use: ListPolicyUse, noun: "title" | "movie" | "show") => {
    const plural = noun === "title" ? "titles" : `${noun}s`;
    return use.strength === "hard"
      ? `Keeps every ${noun} on it`
      : `Leans toward keeping ${plural}, up to ${use.points ?? 0} points off`;
  };
  // Both types acting the same way is one fact, so it is said once, in the neutral noun.
  const [forMovies, forShows] = acting;
  const agree =
    forMovies !== undefined &&
    forShows !== undefined &&
    forMovies.use.strength === forShows.use.strength &&
    (forMovies.use.points ?? null) === (forShows.use.points ?? null);
  const sentences =
    agree && forMovies !== undefined
      ? [said(forMovies.use, "title")]
      : acting.map((a) => said(a.use, a.noun));
  if (sentences.length === 0) {
    return (
      <div className="policy-use unused">
        Not used by your policy yet, so it protects nothing.{" "}
        {onGoToPolicy && (
          <button className="link" onClick={onGoToPolicy}>
            Set it on Policy
          </button>
        )}
      </div>
    );
  }
  return (
    <div className="policy-use">
      {sentences.join(". ")}.{" "}
      {onGoToPolicy && (
        <button className="link" onClick={onGoToPolicy}>
          Change on Policy
        </button>
      )}
    </div>
  );
}

/** Which rows a check is running for. A number is one definition; `"all"` is the whole-pass
 *  target, which only an ORPHAN row uses now -- a stored row no definition owns has no id to
 *  check by, so checking everything is the only pass that can reach it. The screen's
 *  "Check all now" was removed with it: the nightly job checks every list and has its own
 *  Run now on Settings, Jobs. */
type CheckTarget = number | "all";

export function ListsPanel({
  onGoToPolicy,
}: {
  /** Jump to the Policy screen's keep-rules section, where a list's rules live. Optional the
   *  way `SafetyBanner`'s jump is: without a navigator the sentences render and the link does
   *  not. */
  onGoToPolicy?: (() => void) | undefined;
}) {
  const queryClient = useQueryClient();
  const health = useQuery({ queryKey: ["lists"], queryFn: api.lists });
  const definitions = useQuery({ queryKey: ["lists-configured"], queryFn: api.listConfigs });

  // `null` is closed, `{ list: null }` is Add, `{ list }` is Edit. One piece of state rather
  // than an open flag beside a subject, so the two cannot disagree about what is on screen.
  const [modal, setModal] = useState<{ list: ListConfig | null } | null>(null);
  // The modal decides when it may be dismissed and mirrors that whole answer here, the
  // arrangement the service and schedule editors use. Nothing registered here at all once,
  // so Back fell through to the Settings section frame: the panel navigated, this component
  // unmounted, and an in-flight save's refusal went with it while the operator walked away
  // believing the list saved (rule 80).
  const blockCloseRef = useRef(false);
  useBackGuard(
    modal !== null,
    () => setModal(null),
    () => !blockCloseRef.current,
  );

  // Adding, editing or removing a list changes which titles are protected, and the queue is
  // showing fates scored under the lists as they were. Nothing here re-scores them: a check
  // refreshes MEMBERSHIP, which is a different thing, so the queue kept its stale fates with
  // no stale notice and an approved plan met the executor's list interlock at the far end.
  // `PolicyEditor` starts a scan on save for exactly this class of change, and a list is the
  // half of the policy that moved out of the policy body (rules 72, 144).
  //
  // It lives on the panel rather than in the modal because the modal is unmounting: a
  // mutation started on the way out loses the surface that would report it. Idempotent
  // server-side, so a scan already running is simply followed.
  const startScan = useMutation({
    mutationFn: () => api.startScan(),
    onSuccess: (started) => {
      queryClient.setQueryData(["scanStatus"], started);
      // Spoken, because starting a scan is otherwise invisible from this screen: the
      // progress it drives is on another tab. Same two sentences `PolicyEditor` says, from
      // the panel that owns them, and branching for the same reason -- a scan that was
      // already running is scoring the lists as they were before this edit.
      announce(started.followup_queued ? RESCAN_QUEUED_LEAD : `${RESCAN_HEADING}.`);
    },
  });

  const check = useMutation({
    mutationFn: (target: CheckTarget) => api.syncLists(target === "all" ? {} : { list_id: target }),
    // Rule 85: the row's chip is the result, and it is only true once the refetch has landed.
    // Awaiting the invalidation inside the mutation keeps `isPending` true until then, so the
    // button says "Checking…" for exactly as long as the answer on screen is the old one.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["lists"] }),
  });
  const running = check.isPending ? check.variables : null;
  const failedTarget = check.isError ? check.variables : null;
  /** Whether `target`'s button is the one to show as busy. "Check all now" really is
   *  checking every row, so every row says so and none of them can be pressed again while
   *  it runs. Found by driving it: the footer and the rows disagreed, and a row still
   *  offering "Check now" during the pass it was already part of invites a second check
   *  that would race the first (rule 85's shape -- the button reports the state it is in).
   *  Reachable from an orphan row's own button now that the footer's is gone. */
  const busy = (target: CheckTarget) => running === target || running === "all";

  // Both reads, explicitly (rules 17/36). This panel's contract is "always visible", so a
  // failure renders a fallback saying what is unknown rather than an empty screen implying
  // there is nothing to protect.
  if (health.isPending || definitions.isPending) {
    return (
      <div className="panel">
        <h2>Lists</h2>
        <p className="muted">Loading your lists…</p>
      </div>
    );
  }
  if (health.isError || definitions.isError) {
    return (
      <div className="panel">
        <h2>Lists</h2>
        {/* An alert, not `standing`: these are fetched when the tab is opened rather than
            polled, so it mounts once with something the operator has not been told yet. */}
        <Notice tone="error">
          Couldn't load your lists, so there is no way to tell here whether they are working.{" "}
          {/* Says it is retrying while it is. The query's status stays `error` across a
              refetch -- only `isFetching` moves -- so the press changed nothing on screen
              until it settled, which reads as a dead button (rule 85's shape: the control
              reports the state it is in, as the check buttons on the rows do). */}
          <button
            className="ghost sm"
            disabled={health.isFetching || definitions.isFetching}
            onClick={() => {
              void health.refetch();
              void definitions.refetch();
            }}
          >
            {health.isFetching || definitions.isFetching ? "Trying…" : "Try again"}
          </button>
        </Notice>
      </div>
    );
  }

  const rows = health.data;
  /** Every health row synced for one definition. Several for a tag list: one per *arr. */
  const forList = (id: number) => rows.filter((r) => r.list_id === id);

  // Anything stored before a definition existed to own it. The server re-homes such rows
  // onto their definitions before answering (`lists.adopt_legacy`), so what reaches here is
  // only what no definition could safely claim; the next successful check retires it. It is
  // still protecting until then, so it is rendered rather than hidden -- a row that holds
  // titles and is invisible here is the failure this screen exists to fix.
  const orphans = rows.filter((r) => r.list_id === null);

  return (
    <div className="panel">
      <h2>Lists</h2>
      <p className="blurb">
        The lists that keep titles safe, and whether each one is still working. What a list does is
        set with a keep rule on Policy.
      </p>

      <div className="set-rows">
        {definitions.data.map((definition) => {
          const mine = forList(definition.id);
          const state = worst(mine);
          const items = mine.reduce((n, r) => n + r.item_count, 0);
          // Only the members in the worst state are named, and only when something is wrong.
          // Listing all of them would put a tag list back at one line per server, which is
          // the thing being fixed.
          const wanted = mine.filter((r) => r.state === state).map(serverOf);
          const shown =
            mine.length === 0
              ? describe("never_checked", 0)
              : describe(state, items, state === "working" ? undefined : wanted);
          // A row with nothing stored is the one whose sentence a running check contradicts:
          // "Runs with your next scan" beside a button that says "Checking…". That pair is now
          // what every operator sees for the length of the check a save starts, so the row says
          // what is happening instead. Every other state describes the LAST check and stays
          // true while the next one runs.
          const detail =
            busy(definition.id) && mine.length === 0 ? "Checking it now." : shown.detail;
          // Sorted ascending and read at [0], so a family reports its OLDEST check: the row
          // is a claim about the whole family, and the newest member's stamp would say the
          // protection is fresher than its weakest part. Same direction as `worst` above,
          // and stated because reading the stalest of a set looks like an off-by-one.
          const checked = mine
            .map((r) => r.last_checked_at)
            .filter((v): v is string => v !== null)
            .sort();
          const errors = [...new Set(mine.map((r) => r.error).filter(Boolean))];
          const across =
            definition.source === "arr_tag" && mine.length > 1
              ? `Across ${mine.length} servers. `
              : "";
          return (
            <ListRow
              key={definition.id}
              title={definition.name}
              badge={kindBadge(definition.source)}
              detail={detail}
              label={shown.label}
              tone={shown.tone}
              meta={
                (checked.length ? `Last checked ${ago(checked[0]!)}. ` : "") +
                across +
                sourceHint(definition)
              }
              error={errors.length ? errors.join(" ") : null}
              checking={busy(definition.id)}
              onCheck={() => check.mutate(definition.id)}
              onEdit={() => setModal({ list: definition })}
              checkError={failedTarget === definition.id ? (check.error?.message ?? null) : null}
            >
              {definition.source === "arr_tag" && <TagCounts definition={definition} mine={mine} />}
              <PolicyUse definition={definition} onGoToPolicy={onGoToPolicy} />
            </ListRow>
          );
        })}

        {orphans.map((row) => {
          const shown = describe(row.state, row.item_count);
          return (
            <ListRow
              key={row.slug}
              title={row.name}
              detail={shown.detail}
              label={shown.label}
              tone={shown.tone}
              meta={
                (row.last_checked_at ? `Last checked ${ago(row.last_checked_at)}. ` : "") +
                "Set up before this screen existed. Your next check moves it onto a list you " +
                "can edit."
              }
              error={row.error}
              checking={busy("all")}
              onCheck={() => check.mutate("all")}
            />
          );
        })}
      </div>

      {/* The sink for a failed check of EVERYTHING, whether it was started here or from an
          orphan row, whose button drives the "all" target. Every row's own sink compares
          `failedTarget` against a definition id, which is a number and can never equal "all",
          so before this the whole-pass failure had nowhere to land at all: the button went
          back to rest, every row kept its stale chip, and nothing said the check had not run
          (rules 17/36, 42). */}
      {failedTarget === "all" && (
        <Notice tone="error">The check didn't run: {check.error?.message}</Notice>
      )}

      {/* Only while the check it came from is the last thing that happened. `check.data` holds
          the previous result until the next mutation replaces it, so a Plex warning from one
          check stayed on screen through an unrelated Edit or Add, reading as though it were
          about those (rule 85). A check in flight has not answered yet, so it says nothing. */}
      {!check.isPending && check.data?.plex_error && (
        <Notice tone="warn">{check.data.plex_error}</Notice>
      )}

      {/* No "Check all now" here. Checking everything is the upkeep job's whole purpose --
          it runs nightly and has its own Run now on Settings, Jobs -- and it used to be the
          IMDb lists only, which is why this screen grew a second way to do it. Now that the
          job covers every source, two buttons meaning "refresh all of them" in two places is
          the same job offered twice. Per-row Check now stays: it is the one thing the job
          cannot do, which is check the single list you just edited. */}
      <div className="list-foot">
        <span className="flex-spacer" />
        <button className="primary" onClick={() => setModal({ list: null })}>
          Add a list
        </button>
      </div>

      {modal && (
        <ListModal
          editing={modal.list}
          onClose={() => setModal(null)}
          blockCloseRef={blockCloseRef}
          // A saved list is checked on the spot, through the same mutation the row's own
          // button drives: a list is protecting nothing until something reads it, and an
          // operator who has just said what the list is should not have to say "now go and
          // read it" as a second step. The row shows it as busy and carries whatever comes
          // back, a count or the source's own refusal, which is the answer to the question
          // they were really asking when they saved.
          onSaved={(list) => check.mutate(list.id)}
          // Every path that changed the registry, removal included: what a list keeps is
          // scored into the queue's fates, so the library has to be re-judged whether a list
          // arrived, moved, or left.
          onChanged={() => startScan.mutate()}
        />
      )}
    </div>
  );
}

/** Where a row's list comes from, in one clause, so a reader can tell two lists apart when
 *  their names do not. The operator named the list; this says what they pointed it at. */
function sourceHint(definition: ListConfig): string {
  if (definition.source === "plex_collection") {
    const { library, collection } = definition.config;
    return library && collection
      ? `The "${collection}" collection in ${library}.`
      : "A Plex collection.";
  }
  if (definition.source === "plex_watchlist") {
    return "The watchlist of the Plex account Reaper is signed in with.";
  }
  if (definition.source === "arr_tag") {
    return definition.config.match === "all"
      ? "Read from every connected Sonarr and Radarr. Titles need every tag."
      : "Read from every connected Sonarr and Radarr.";
  }
  return definition.config.preset
    ? "Keeps itself up to date."
    : `IMDb list ${definition.config.list_id ?? ""}.`;
}
