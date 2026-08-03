// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Lists. Whether each protection list is still protecting anything (#475).
//
// Read-only, and deliberately so for now: a list is still configured where it always was --
// keep tags on Policy, the IMDb Top 250 by shipping with Reaper -- and this screen is the one
// place they can all be seen at once. That is the half that was missing. Every column here was
// written on every sync since lists shipped and reached the operator through nothing but a
// degraded-scan notice, which fires late and names a slug rather than a cause.
//
// **One row per protection, never one per stored list.** Reaper stores a keep-tag list per *arr
// instance per match mode, so two Radarrs and two Sonarrs are four rows for the single thing the
// operator did: tag some titles `reaper-keep`. Every instance added multiplies that, and a page
// that grows with the server count stops being readable exactly on the installs with the most to
// lose. So the *arr lists collapse into one row that carries the whole family's worst state and
// names only the servers that need attention. `source` comes from the server for this; the slug
// spellings live beside the retire sweep and are not re-derived here.
//
// The verdict is the server's (`lists.ListHealth`), not this component's. Two surfaces deciding
// for themselves what `last_error` plus `last_synced_at` means is how the screen and the scan
// end up telling the operator different stories about one failed check (rule 144). What is
// decided here is only the sentence.
//
// The row grammar is the Jobs tab's (`.jobrow`), not a second settings-row shape. Both pages
// answer the same question -- is this thing working, and when did it last run -- so a reader
// moving between them should not have to learn two layouts (rule 18).

import { useQuery } from "@tanstack/react-query";

import { api, type ProtectionList } from "../api";
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
 *  member is a family that says "Working" while one server's keep tags protect nothing. */
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

/** One rendered row: the Jobs tab's shape, with the state chip where a job's buttons sit. */
function ListRow({
  title,
  detail,
  label,
  tone,
  meta,
  error,
}: {
  title: string;
  detail: string;
  label: string;
  tone: Tone;
  meta: string | null;
  error?: string | null;
}) {
  return (
    <div className="jobrow">
      <div className="jobrow-main">
        {/* The name arrives from Plex or an *arr, so it wraps rather than running through the
            box holding it (rule 139). */}
        <div className="jobrow-title list-name">{title}</div>
        <div className="jobrow-desc">{detail}</div>
        {meta !== null && <div className="jobrow-sched">{meta}</div>}
        {/* The service's own words, which name the thing to go and fix: a library not called
            what Reaper asked for, a tag that was renamed. Also from outside the app. */}
        {error != null && <div className="jobrow-desc list-error">{error}</div>}
      </div>
      <div className="jobrow-actions">
        <span className={`list-state list-state-${tone}`}>{label}</span>
      </div>
    </div>
  );
}

/** "Radarr (4k) tag: reaper-keep" -> "Radarr (4k)". The server it belongs to is the useful
 *  half when a collapsed family has to name which member needs attention; the tag names are
 *  the same across them by construction, since one policy sets them. Falls back to the whole
 *  string, so a display name this does not recognize is still named rather than dropped. */
function serverOf(name: string): string {
  const cut = name.indexOf(" tag:");
  return cut === -1 ? name : name.slice(0, cut);
}

export function ListsPanel() {
  const lists = useQuery({ queryKey: ["lists"], queryFn: api.lists });

  return (
    <div className="panel">
      <h2>Lists</h2>
      <p className="blurb">
        The lists that keep titles safe, and whether each one is still working. A list that stops
        working stops protecting, so this is the page to check when a scan says it cannot delete
        anything.
      </p>

      {lists.isPending ? (
        <p className="muted">Loading your lists…</p>
      ) : lists.isError ? (
        // An alert, not `standing`: this query is fetched when the tab is opened rather than
        // polled, so it mounts once with something the operator has not been told yet.
        <Notice tone="error">
          Couldn't load your lists, so there is no way to tell here whether they are working.{" "}
          <button className="ghost sm" onClick={() => void lists.refetch()}>
            Try again
          </button>
        </Notice>
      ) : lists.data.length === 0 ? (
        <p className="muted">
          No lists yet. Reaper builds them on the first scan, from the tags you set on Policy and
          the lists it ships with.
        </p>
      ) : (
        <div className="set-rows">
          {(() => {
            const tags = lists.data.filter((l) => l.source === "arr_tag");
            const rest = lists.data.filter((l) => l.source !== "arr_tag");
            const rows = rest.map((l) => {
              const d = describe(l.state, l.item_count);
              return (
                <ListRow
                  key={l.slug}
                  title={l.name}
                  detail={d.detail}
                  label={d.label}
                  tone={d.tone}
                  meta={l.last_checked_at && `Last checked ${ago(l.last_checked_at)}.`}
                  error={l.error}
                />
              );
            });

            if (tags.length > 0) {
              const state = worst(tags);
              // Only the members in the worst state are named. Listing all of them would put
              // the row back at one line per server, which is the thing being fixed.
              const wanted = tags.filter((t) => t.state === state).map((t) => serverOf(t.name));
              const items = tags.reduce((n, t) => n + t.item_count, 0);
              const d = describe(state, items, state === "working" ? undefined : wanted);
              const checked = tags
                .map((t) => t.last_checked_at)
                .filter((v): v is string => v !== null)
                .sort();
              const errors = [...new Set(tags.map((t) => t.error).filter(Boolean))];
              rows.push(
                <ListRow
                  key="arr-tags"
                  title="Titles you've tagged"
                  detail={d.detail}
                  label={d.label}
                  tone={d.tone}
                  meta={
                    (checked.length ? `Last checked ${ago(checked[0]!)}. ` : "") +
                    `Across ${tags.length} ${tags.length === 1 ? "server" : "servers"}. ` +
                    "Set the tags on Policy."
                  }
                  error={errors.length ? errors.join(" ") : null}
                />,
              );
            }
            return rows;
          })()}
        </div>
      )}
    </div>
  );
}
