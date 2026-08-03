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
// The verdict is the server's (`lists.ListHealth`), not this component's. Two surfaces deciding
// for themselves what `last_error` plus `last_synced_at` means is how the screen and the scan
// end up telling the operator different stories about one failed check (rule 144). What is
// decided here is only the sentence, and the one branch it takes is on `item_count`, which is a
// number rather than a second copy of the judgment: a failing list with members still covers
// them, and one without is protecting nothing right now. Those ask for different urgency.

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

/** The chip, and the sentence under it. One function so a state cannot pick up a chip on one
 *  render and lose its explanation on another. */
function describe(list: ProtectionList): { label: string; tone: string; detail: string } {
  switch (list.state) {
    case "working":
      return {
        label: "Working",
        tone: "ok",
        detail: `Protecting ${titles(list.item_count)}.`,
      };
    case "stale":
      return {
        label: "Out of date",
        tone: "warn",
        detail:
          `Still protecting ${titles(list.item_count)}, but the last check was too long ago. ` +
          "Anything added since then is not covered yet.",
      };
    case "failing":
      return {
        label: "Not working",
        tone: "bad",
        detail:
          list.item_count > 0
            ? `The last check failed. The ${titles(list.item_count)} from the last good check ` +
              "are still protected, and anything added since then is not."
            : "The last check failed and nothing is stored, so this list is protecting nothing.",
      };
    case "never_checked":
      return {
        label: "Not checked yet",
        tone: "idle",
        detail: "Runs with your next scan. Nothing on it is protected until it does.",
      };
  }
}

export function ListsPanel() {
  const lists = useQuery({ queryKey: ["lists"], queryFn: api.lists });

  return (
    <div className="panel">
      <h2>Lists</h2>
      <p className="muted">
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
          {/* Keyed on the slug, never the name: two same-service instances produce two rows
              whose display names can be spelled identically (rule 19, rule 63). */}
          {lists.data.map((list) => {
            const state = describe(list);
            return (
              <div className="set-row set-row-plain" key={list.slug}>
                <span className="set-label list-name">{list.name}</span>
                {/* ONE help paragraph, because the shared row grid gives the label's column a
                    single help slot and a second <p> lands on top of it (rule 45 wants one
                    anyway). The error rides inside it as a block, not beside it. */}
                <p className="help">
                  {state.detail}
                  {list.last_checked_at !== null && ` Last checked ${ago(list.last_checked_at)}.`}
                  {/* The service's own words, which is what names the thing to go and fix: a
                      library not called what Reaper asked for, a tag that was renamed. It
                      arrives from outside the app, so it wraps rather than truncates
                      (rule 139) -- two errors differing only in their tail would otherwise
                      read as the same error. */}
                  {list.error !== null && <span className="list-error">{list.error}</span>}
                </p>
                <span className="set-control">
                  <span className={`list-state list-state-${state.tone}`}>{state.label}</span>
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
