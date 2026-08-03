// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Settings -> Lists. Every protection list, whether each is still protecting anything, and
// the controls to add, edit and remove them (#475).
//
// **Settings owns what a list IS and where it comes from; Policy owns what it does.** Nothing
// is configured in two places. The one exception is the keep tags, which stay on Policy with
// the same switch and boxes they always had -- they are a rule about tags rather than a list
// somebody named, and moving them would have been a second migration for no gain. They still
// appear here, as one row, because this screen's job is to show every protection at once.
//
// **One row per protection, never one per stored list.** Reaper stores a keep-tag list per
// *arr instance, so two Radarrs and two Sonarrs are four stored rows for the single thing the
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
import { useState } from "react";

import { api, type ListConfig, type ProtectionList } from "../api";
import { ListModal } from "./ListModal";
import { ModalShell } from "./ModalShell";
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

/** A list that is switched off. Its own state, because none of the four above fit: the stored
 *  membership may be perfectly healthy and it is still protecting nothing, and saying "Working"
 *  about a list the operator switched off is the reassuring direction this must not fail in. */
const OFF = {
  label: "Off",
  tone: "idle" as Tone,
  detail: "Switched off, so nothing on this list is protected. Edit it to switch it back on.",
};

/** "Radarr (4k) tag: reaper-keep" -> "Radarr (4k)". The server it belongs to is the useful
 *  half when a collapsed family has to name which member needs attention; the tag names are
 *  the same across them by construction, since one policy sets them. Falls back to the whole
 *  string, so a display name this does not recognize is still named rather than dropped. */
function serverOf(name: string): string {
  const cut = name.indexOf(" tag:");
  return cut === -1 ? name : name.slice(0, cut);
}

/** One rendered row: the Jobs tab's shape, with the state chip on the title line and real
 *  actions where a job's buttons sit. */
function ListRow({
  title,
  detail,
  label,
  tone,
  meta,
  error,
  checking,
  onCheck,
  onEdit,
  onRemove,
  checkError,
}: {
  title: string;
  detail: string;
  label: string;
  tone: Tone;
  meta: string | null;
  error?: string | null;
  checking: boolean;
  onCheck: () => void;
  /** Absent for the keep-tag row, which is configured on Policy and has no definition here. */
  onEdit?: (() => void) | undefined;
  /** Absent for the keep tags and for a list Reaper ships with. */
  onRemove?: (() => void) | undefined;
  /** Why the last check pressed on THIS row failed. Rendered here rather than in a page-level
   *  slot, because it is about this list and the button that retries it is on this row
   *  (rule 42). */
  checkError?: string | null | undefined;
}) {
  return (
    <div className="jobrow">
      <div className="jobrow-main">
        {/* The name arrives from Plex or an *arr, or from the operator's own keyboard, so it
            wraps rather than running through the box holding it (rule 139). */}
        <div className="list-head">
          <span className="jobrow-title list-name">{title}</span>
          <span className={`list-state list-state-${tone}`}>{label}</span>
        </div>
        <div className="jobrow-desc">{detail}</div>
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
        <span className="slot-del">
          {onRemove && (
            <button className="ghost danger" aria-label={`Remove ${title}`} onClick={onRemove}>
              Remove
            </button>
          )}
        </span>
      </div>
    </div>
  );
}

/** Confirming a removal. A list is a protection, so taking one away says what stops being
 *  protected, in the same words the modal that made it used. `ModalShell` rather than a
 *  native `confirm()`, which rule 18 bans and which no screen reader announces as a dialog. */
function RemoveConfirm({ list, onClose }: { list: ListConfig; onClose: () => void }) {
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: () => api.removeList(list.id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["lists-configured"] }),
        queryClient.invalidateQueries({ queryKey: ["lists"] }),
      ]);
      onClose();
    },
  });
  return (
    <ModalShell title={`Remove ${list.name}?`} onClose={onClose} canClose={!remove.isPending}>
      <div className="service-form">
        <p>
          Anything this list was keeping can be deleted by the next scan. Reaper does not delete the
          collection or the tags themselves, only its own record of them.
        </p>
        {remove.error && (
          <Notice tone="error">The list wasn't removed: {remove.error.message}</Notice>
        )}
        <div className="add-actions">
          <span className="flex-spacer" />
          <button type="button" className="ghost" onClick={onClose} disabled={remove.isPending}>
            Cancel
          </button>
          <button
            type="button"
            className="primary danger"
            onClick={() => remove.mutate()}
            disabled={remove.isPending}
          >
            {remove.isPending ? "Removing…" : "Remove list"}
          </button>
        </div>
      </div>
    </ModalShell>
  );
}

/** Which row a check is running for. `"all"` is the footer's button; a number is one
 *  definition; `"keep-tags"` is the policy's tags, which have no definition to be named by. */
type CheckTarget = number | "keep-tags" | "all";

export function ListsPanel() {
  const queryClient = useQueryClient();
  const health = useQuery({ queryKey: ["lists"], queryFn: api.lists });
  const definitions = useQuery({ queryKey: ["lists-configured"], queryFn: api.listConfigs });

  // `null` is closed, `{ list: null }` is Add, `{ list }` is Edit. One piece of state rather
  // than an open flag beside a subject, so the two cannot disagree about what is on screen.
  const [modal, setModal] = useState<{ list: ListConfig | null } | null>(null);
  const [removing, setRemoving] = useState<ListConfig | null>(null);

  const check = useMutation({
    mutationFn: (target: CheckTarget) =>
      api.syncLists(
        target === "all" ? {} : target === "keep-tags" ? { keep_tags: true } : { list_id: target },
      ),
    // Rule 85: the row's chip is the result, and it is only true once the refetch has landed.
    // Awaiting the invalidation inside the mutation keeps `isPending` true until then, so the
    // button says "Checking…" for exactly as long as the answer on screen is the old one.
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["lists"] }),
  });
  const running = check.isPending ? check.variables : null;
  const failedTarget = check.isError ? check.variables : null;

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

  // The keep tags, and anything stored before a definition existed to own it. Both are rows
  // with no `list_id`: the keep tags because they are configured on Policy, the second because
  // an upgrade re-homes a list under a new slug on its first successful check. The second kind
  // is still protecting until that happens, so it is rendered rather than hidden -- a row that
  // holds titles and is invisible here is the failure this screen exists to fix.
  const keepTagRows = rows.filter((r) => r.list_id === null && r.source === "arr_tag");
  const orphans = rows.filter((r) => r.list_id === null && r.source !== "arr_tag");

  return (
    <div className="panel">
      <h2>Lists</h2>
      <p className="blurb">
        The lists that keep titles safe, and whether each one is still working. A list that stops
        working stops protecting, so this is the page to check when a scan says it cannot delete
        anything.
      </p>

      <div className="set-rows">
        {definitions.data.map((definition) => {
          const mine = forList(definition.id);
          const state = worst(mine);
          const items = mine.reduce((n, r) => n + r.item_count, 0);
          // Only the members in the worst state are named, and only when something is wrong.
          // Listing all of them would put a tag list back at one line per server, which is
          // the thing being fixed.
          const wanted = mine.filter((r) => r.state === state).map((r) => serverOf(r.name));
          const shown = !definition.enabled
            ? OFF
            : mine.length === 0
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
              detail={shown.detail}
              label={shown.label}
              tone={shown.tone}
              meta={
                (checked.length ? `Last checked ${ago(checked[0]!)}. ` : "") +
                across +
                sourceHint(definition)
              }
              error={definition.enabled && errors.length ? errors.join(" ") : null}
              checking={running === definition.id}
              onCheck={() => check.mutate(definition.id)}
              onEdit={() => setModal({ list: definition })}
              // A list Reaper ships with is never removable: a keep rule in the default
              // policy names it, and a rule naming a list that is gone reads as a live
              // protection covering nothing (rule 25). Switching it off is always available.
              onRemove={definition.built_in ? undefined : () => setRemoving(definition)}
              checkError={failedTarget === definition.id ? (check.error?.message ?? null) : null}
            />
          );
        })}

        {keepTagRows.length > 0 &&
          (() => {
            const state = worst(keepTagRows);
            const items = keepTagRows.reduce((n, r) => n + r.item_count, 0);
            const wanted = keepTagRows
              .filter((r) => r.state === state)
              .map((r) => serverOf(r.name));
            const shown = describe(state, items, state === "working" ? undefined : wanted);
            const checked = keepTagRows
              .map((r) => r.last_checked_at)
              .filter((v): v is string => v !== null)
              .sort();
            const errors = [...new Set(keepTagRows.map((r) => r.error).filter(Boolean))];
            return (
              <ListRow
                key="arr-tags"
                title="Titles you've tagged"
                detail={shown.detail}
                label={shown.label}
                tone={shown.tone}
                meta={
                  (checked.length ? `Last checked ${ago(checked[0]!)}. ` : "") +
                  `Across ${keepTagRows.length} ` +
                  `${keepTagRows.length === 1 ? "server" : "servers"}. ` +
                  "Set the tags on Policy."
                }
                error={errors.length ? errors.join(" ") : null}
                checking={running === "keep-tags"}
                onCheck={() => check.mutate("keep-tags")}
                checkError={failedTarget === "keep-tags" ? (check.error?.message ?? null) : null}
              />
            );
          })()}

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
              checking={running === "all"}
              onCheck={() => check.mutate("all")}
              checkError={failedTarget === "all" ? (check.error?.message ?? null) : null}
            />
          );
        })}
      </div>

      {check.data?.plex_error && <Notice tone="warn">{check.data.plex_error}</Notice>}

      <div className="list-foot">
        <button className="ghost" onClick={() => check.mutate("all")} disabled={check.isPending}>
          {running === "all" ? "Checking…" : "Check all now"}
        </button>
        <span className="flex-spacer" />
        <button className="primary" onClick={() => setModal({ list: null })}>
          Add a list
        </button>
      </div>

      {modal && <ListModal editing={modal.list} onClose={() => setModal(null)} />}
      {removing && <RemoveConfirm list={removing} onClose={() => setRemoving(null)} />}
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
  if (definition.source === "arr_tag") {
    const tags = definition.config.tags ?? [];
    const joiner = definition.config.match === "all" ? " and " : " or ";
    return tags.length ? `Tagged ${tags.join(joiner)} in Sonarr or Radarr.` : "An *arr tag.";
  }
  return "Ships with Reaper, and keeps itself up to date.";
}
