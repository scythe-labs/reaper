// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Spare and Reap, wherever they are offered. The cards, an expanded show's season rows and
// the why-panel all set the same override, and all of them must refresh the same caches --
// so the list of caches an override touches is written once, here, and every surface that
// gains one only has to be added in a single place.

import { type InfiniteData, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Candidate, type CandidatePage, type Override } from "./api";

/** The exact day in ms, for an optimistic timed-spare countdown until the server confirms it. */
const DAY_MS = 86_400_000;

export function useOverrideMutations() {
  const queryClient = useQueryClient();

  // Patch one candidate's override fields wherever it sits in the cached queue pages, WITHOUT
  // removing it from the lane it is on. This is what keeps a just-decided row in place -- dimmed
  // and wearing its overlay -- instead of vanishing under the operator's cursor when the
  // effective-lane filter would re-bucket it (the operator-approved "don't live-disappear"
  // behavior). The row settles onto its effective lane only on the NEXT fetch of that tab: a tab
  // switch, navigating away and back, or a rescan (see `settle`). Its stored verdict is pure
  // policy throughout; this only moves the overlay.
  //
  // Returns whether any cached row matched. A WHOLE-SHOW decision keys on the show/group key,
  // which no season row's media_key equals, so it patches nothing -- and a row not currently
  // loaded matches nothing either. `settle` reads this to fall back to a real refetch in those
  // cases, so a whole-show spare/reap is not silently invisible on the card that carries it.
  const patchInPlace = (key: string, fields: Partial<Candidate>): boolean => {
    let matched = false;
    queryClient.setQueriesData<InfiniteData<CandidatePage>>(
      { queryKey: ["candidates"] },
      (old) =>
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

  // After a single hand decision: refresh the show panels, the why-panel and the Reap ledger now.
  // When the decided row was patched in place (a movie or season already on screen), mark the
  // QUEUE stale WITHOUT refetching the active tab (refetchType "none"), so the row stays put and
  // the lane re-buckets only on the next fetch. When NOTHING was patched -- a whole-show decision
  // (keyed on the show/group key, which no row's media_key matches) or a row not currently loaded
  // -- refetch the active tab instead: there is no on-screen overlay to preserve, and a whole-show
  // decision covers a whole set at once, so re-bucketing it now is expected (as with a bulk action).
  const settle = (patchedInPlace: boolean) => {
    void queryClient.invalidateQueries({
      queryKey: ["candidates"],
      refetchType: patchedInPlace ? "none" : "active",
    });
    void queryClient.invalidateQueries({ queryKey: ["group"] });
    void queryClient.invalidateQueries({ queryKey: ["candidate"] });
    void queryClient.invalidateQueries({ queryKey: ["reap-breakdown"] });
  };

  // A full refetch, including the active queue tab. Bulk actions apply a decision to a whole
  // selection deliberately, so re-bucketing them at once is expected -- unlike a single decision
  // made mid-review. Exported for those callers (the bulk bar) that want the queue reloaded now.
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["candidates"] });
    void queryClient.invalidateQueries({ queryKey: ["group"] });
    void queryClient.invalidateQueries({ queryKey: ["candidate"] });
    void queryClient.invalidateQueries({ queryKey: ["reap-breakdown"] });
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
      // Optimistic overlay: enough for handFate, the chips and the dimming to render in place.
      // A reap's honored-vs-held (`override_effective`) is a server call (reap_is_effective), so
      // it is left null here -- until the next fetch a reap reads as taking, the over-warning
      // direction -- and the row's own decision equals the effective one for a per-item control.
      const patched = patchInPlace(key, {
        override: decision,
        override_own: decision,
        spared: decision === "spare",
        override_effective: null,
        spare_expires_at:
          decision === "spare" && spareDays > 0
            ? new Date(Date.now() + spareDays * DAY_MS).toISOString()
            : null,
      });
      settle(patched);
    },
  });
  const clearOverride = useMutation({
    mutationFn: (key: string) => api.clearOverride(key),
    onSuccess: (_data, key) => {
      // Back to pure policy in place; the row settles onto its policy lane on the next fetch.
      const patched = patchInPlace(key, {
        override: null,
        override_own: null,
        spared: false,
        override_effective: null,
        spare_expires_at: null,
      });
      settle(patched);
    },
  });

  return { setOverride, clearOverride, refresh };
}
