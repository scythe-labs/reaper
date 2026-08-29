// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Spare and Reap, wherever they are offered. The cards, an expanded show's season rows and
// the why-panel all set the same override, and all of them must refresh the same caches. So
// the list of caches an override touches is written once, here, and every surface that gains
// one only has to be added in a single place.

import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Candidate, type CandidatePage, type Override } from "./api";

/** The exact day in ms, for an optimistic timed-spare countdown until the server confirms it. */
const DAY_MS = 86_400_000;

/** Everything the server answers override-aware, so no surface is left quoting a number a
 *  hand decision has already changed. The queue and its panels are the obvious ones; the scan
 *  summary shifts lanes by the overrides and the Scales figures count only the effective reap
 *  set, so those two go wrong the moment a title is spared just as easily and just as
 *  invisibly. A surface that gains an override-aware number is added here, and only here. */
const OVERRIDE_AWARE = [["group"], ["candidate"], ["reap-breakdown"], ["snapshot"], ["fairness"]];

export function useOverrideMutations() {
  const queryClient = useQueryClient();

  // Patches one candidate's override fields wherever it sits in the cached queue pages,
  // without removing it from the lane it is on. This keeps a just-decided row in place,
  // dimmed and wearing its overlay, instead of vanishing under the operator's cursor when the
  // effective-lane filter would re-bucket it: the "don't live-disappear" behavior. The row
  // settles onto its effective lane only on the next fetch of that tab: a tab switch,
  // navigating away and back, or a rescan (see `settle`). Its stored verdict is pure policy
  // throughout; this only moves the overlay.
  //
  // Returns whether any cached row matched. A whole-show decision keys on the show/group key,
  // which no season row's media_key equals, so it patches nothing here. `patchShowOverride`
  // below is its counterpart, keyed on group_key. A row not currently loaded matches neither;
  // `settle` then falls back to a real refetch, so a decision is never silently invisible.
  const patchInPlace = (key: string, fields: Partial<Candidate>): boolean => {
    let matched = false;
    queryClient.setQueriesData<InfiniteData<CandidatePage>>({ queryKey: ["candidates"] }, (old) =>
      old
        ? {
            ...old,
            pages: old.pages.map((page) => ({
              ...page,
              items: page.items.map((c) => {
                if (c.media_key !== key) return c;
                matched = true;
                return { ...c, ...fields };
              }),
            })),
          }
        : old,
    );
    return matched;
  };

  // The whole-show counterpart of patchInPlace. A whole-show Spare/Reap keys on the show/group
  // key, which equals no season's media_key, so patchInPlace matches nothing. This patches the
  // show-level fields (`show_override` and its expiry) on every loaded season of the group
  // instead, so the card carrying the control lights up at once. Its tint, whole-show chip,
  // resting mark and lit state all read `show_override`, without needing to refetch the tab.
  // That keeps the show in the lane the operator is looking at (a whole-show reap of an item
  // sitting in Limbo would otherwise re-bucket to Condemned and vanish mid-review), the same
  // "don't live-disappear" behavior a per-item decision already gets. The per-season strip
  // marks (the page's `groups` rollup) and the honored-vs-held flag settle on the next fetch,
  // the same as a per-item season decision's do. Only the show-level fields are touched: a
  // whole-show decision sets no season's own override, and the effective (inherited)
  // resolution stays the server's, never recomputed on the client.
  //
  // Returns whether any row matched. A show with no loaded seasons (a panel left open after
  // the list moved on) matches nothing, and `settle` then falls back to a real refetch, as
  // before.
  const patchShowOverride = (
    groupKey: string,
    decision: Override | null,
    spareExpiresAt: string | null,
  ): boolean => {
    let matched = false;
    queryClient.setQueriesData<InfiniteData<CandidatePage>>({ queryKey: ["candidates"] }, (old) =>
      old
        ? {
            ...old,
            pages: old.pages.map((page) => ({
              ...page,
              items: page.items.map((c) => {
                if (c.group_key !== groupKey) return c;
                matched = true;
                return { ...c, show_override: decision, show_spare_expires_at: spareExpiresAt };
              }),
            })),
          }
        : old,
    );
    return matched;
  };

  // After a single hand decision, refreshes the show panels, the why-panel and the Reap
  // ledger now. When the decided row was patched in place (a movie or season by media_key, or
  // a whole show by its loaded seasons' group key), it marks the queue stale without
  // refetching the active tab (refetchType "none"), so it stays put and the lane re-buckets
  // only on the next fetch. When nothing was patched (a decision on a row or show not
  // currently loaded), it refetches the active tab instead, since there is no on-screen
  // overlay to preserve. A bulk action re-buckets at once on purpose and uses `refresh` below,
  // not this path.
  const settle = (patchedInPlace: boolean) => {
    void queryClient.invalidateQueries({
      queryKey: ["candidates"],
      refetchType: patchedInPlace ? "none" : "active",
    });
    for (const queryKey of OVERRIDE_AWARE) void queryClient.invalidateQueries({ queryKey });
  };

  // A full refetch, including the active queue tab. Bulk actions apply a decision to a whole
  // selection deliberately, so re-bucketing them at once is expected, unlike a single decision
  // made mid-review. Exported for those callers (the bulk bar) that want the queue reloaded
  // now.
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["candidates"] });
    for (const queryKey of OVERRIDE_AWARE) void queryClient.invalidateQueries({ queryKey });
  };

  const setOverride = useMutation({
    // `spareDays` is how long a spare keeps the item: 0 (or omitted) = forever, a positive
    // count that many days. Ignored by the server for a reap.
    mutationFn: ({
      key,
      decision,
      spareDays = 0,
    }: {
      key: string;
      decision: Override;
      spareDays?: number;
    }) => api.override(key, decision, undefined, spareDays),
    onSuccess: (_data, { key, decision, spareDays = 0 }) => {
      const spareExpiresAt =
        decision === "spare" && spareDays > 0
          ? new Date(Date.now() + spareDays * DAY_MS).toISOString()
          : null;
      // A movie or season already on screen: patch its own overlay fields. This is an
      // optimistic overlay, enough for handFate, the chips and the dimming to render in
      // place. A reap's honored-vs-held state (`override_effective`) is a server call
      // (reap_is_effective), so it is left null here. Until the next fetch, a reap then reads
      // as taking effect, the over-warning direction, since the row's own decision equals the
      // effective one for a per-item control. A whole-show decision matches no media_key, so
      // it falls through to patchShowOverride (its group key), keeping the show in its lane
      // too.
      const patched =
        patchInPlace(key, {
          override: decision,
          override_own: decision,
          override_effective: null,
          spare_expires_at: spareExpiresAt,
          // The fate field the colors read. Deliberately the spare just set, not the
          // server's own-or-show maximum, since reproducing that here would write the same
          // derivation twice. It cannot mislead about the color, because a spare set now is
          // always in the future or forever and so never reads expired; at worst a season
          // inside a longer show spare prints its own shorter countdown for one fetch, which
          // understates how long the file is kept, the cautious side of a keep claim.
          spare_covers_until: spareExpiresAt,
        }) || patchShowOverride(key, decision, spareExpiresAt);
      settle(patched);
    },
  });
  const clearOverride = useMutation({
    mutationFn: (key: string) => api.clearOverride(key),
    onSuccess: (_data, key) => {
      // Back to pure policy in place; the row settles onto its policy lane on the next fetch. A
      // whole-show clear matches no media_key, so fall through to patchShowOverride (its group
      // key) to drop the show-level overlay, keeping the show in its lane too.
      const patched =
        patchInPlace(key, {
          override: null,
          override_own: null,
          override_effective: null,
          spare_expires_at: null,
          // Read only alongside a "spare" decision, and this clears the decision, so nothing
          // reads it before the refetch resolves what (if anything) still covers the row.
          spare_covers_until: null,
        }) || patchShowOverride(key, null, null);
      settle(patched);
    },
  });

  return { setOverride, clearOverride, refresh };
}
