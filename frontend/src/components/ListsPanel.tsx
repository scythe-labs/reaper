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
import { Fragment, useState, type ReactNode } from "react";

import { api, type ListConfig, type ProtectionList } from "../api";
import { ListModal } from "./ListModal";
import { Notice } from "./Notice";

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
      return {
        label: "Out of date",
        tone: "warn",
        detail:
          `Still protecting ${titles(items)}, but the last check was too long ago. ` +
          "Anything added since then is not covered yet.",
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
      return {
        label: "Not checked yet",
        tone: "idle",
        detail: `Runs with your next scan. Nothing on it is protected until it does.${named}`,
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
    return <span className="kind-badge kind-arr">Sonarr Radarr</span>;
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
          <span className={`list-state list-state-${tone}`}>{label}</span>
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
            <span key={tag} className="tag-pill">
              {tag}
              {n !== null && <b>{n.toLocaleString()}</b>}
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
  const sentences = [
    ...new Set(
      definition.policy_use.map((use) =>
        use.strength === "hard"
          ? "Keeps every title on it"
          : `Leans toward keeping, up to ${use.points ?? 0} points off`,
      ),
    ),
  ];
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

/** Which rows a check is running for. `"all"` is the footer's button; a number is one
 *  definition. */
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
   *  that would race the first (rule 85's shape -- the button reports the state it is in). */
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
          <button
            className="ghost sm"
            onClick={() => {
              void health.refetch();
              void definitions.refetch();
            }}
          >
            Try again
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
              detail={shown.detail}
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
              checkError={failedTarget === "all" ? (check.error?.message ?? null) : null}
            />
          );
        })}
      </div>

      {check.data?.plex_error && <Notice tone="warn">{check.data.plex_error}</Notice>}

      <div className="list-foot">
        <span className="flex-spacer" />
        <button className="ghost" onClick={() => check.mutate("all")} disabled={check.isPending}>
          {running === "all" ? "Checking…" : "Check all now"}
        </button>
        <button className="primary" onClick={() => setModal({ list: null })}>
          Add a list
        </button>
      </div>

      {modal && <ListModal editing={modal.list} onClose={() => setModal(null)} />}
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
