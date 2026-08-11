// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The two stored settings every row of the review queue reads, and the one subscription they
// share.
//
// Both used to be read by a hook that opened its own query per caller -- one per Spare/Reap
// control (each card, and each season row of every expanded show) and one per card status line.
// Four hundred drawn cards with their seasons open came to roughly a thousand query observers on
// two keys: the request is deduped, but every write to either cache then re-rendered all thousand
// (P-7). The queue subscribes once, provides the values here, and a write now re-renders a row
// only when the value that row reads has actually changed.
//
// Outside the provider the hooks fall back to their own query, which is what the why panel, the
// show panel and the Reap page do: each renders a handful of controls, not hundreds.

import { useQuery } from "@tanstack/react-query";
import { createContext, useContext } from "react";
import { api, type ExpandSeasonsMode } from "../api";
import { useGeneralSettings } from "../useGeneralSettings";

/** Whether a show card starts with its season list open, given the operator's preference and
 *  which screen they are on.
 *
 *  A phone's season list is long enough to bury the next card, so the two screen sizes are
 *  separately choosable and "both" is the only value that opens on either. Pure and exported
 *  so all eight combinations are pinned by a table, rather than by eight async renders whose
 *  "stays collapsed" half would pass just as well against a preference that never loaded. */
export function shouldExpandSeasons(mode: ExpandSeasonsMode, narrowScreen: boolean): boolean {
  return mode === "both" || mode === (narrowScreen ? "mobile" : "desktop");
}

/** The two stored settings every row in the queue reads, subscribed to ONCE.
 *
 *  Both were read through a hook that opened its own `useQuery` per caller -- one per
 *  `OverrideControls` (each card, and each season row of every expanded show) and one per
 *  `CardStatusLine`. Four hundred drawn cards with their seasons open came to roughly a
 *  thousand query observers on two keys: the request is deduped, but every write to either
 *  cache then re-rendered all thousand (P-7). The queue subscribes once and hands the values
 *  down here instead, so a write re-renders a row only when the value it reads has changed.
 *
 *  Null outside the provider, where the hooks below fall back to their own query: the why
 *  panel, the show panel and the Reap page each render a handful of controls, not hundreds. */
export type QueueSettings = {
  defaultSpareDays: number;
  unmeasured: { holdsBack: boolean; isPending: boolean; isError: boolean };
};

export const QueueSettingsContext = createContext<QueueSettings | null>(null);

/** Options that make a fallback query inert while the provider is supplying the value.
 *
 *  A hook cannot be called conditionally, so both hooks below open their query either way and
 *  make it inert inside the provider. `enabled` stops the FETCH and nothing else: the observer is
 *  still subscribed, so every write to the key re-renders every component holding one -- the
 *  exact thousand-observer fan-out this file's provider exists to remove, left in place. Tracking
 *  no props at all is what actually stops the notification. Outside the provider the query is the
 *  real source, so it keeps the default tracking.
 *
 *  `useDefaultSpareDays` says the same thing as `useGeneralSettings(false)`, which takes both off
 *  one flag; this pair stays here because `["profile"]` has no shared hook. */
function silentInsideProvider(shared: QueueSettings | null): { notifyOnChangeProps?: [] } {
  return shared === null ? {} : { notifyOnChangeProps: [] };
}

/** How long a plain Spare press keeps an item: the operator's General preference (0 = forever,
 *  N = N days). Read from the shared general-settings cache, so flipping it in Settings takes
 *  effect here without a reload. Unknown/error reads as 0 -- forever, the safe, unchanged
 *  default. */
export function useDefaultSpareDays(): number {
  const shared = useContext(QueueSettingsContext);
  const { data } = useGeneralSettings(shared === null);
  return shared ? shared.defaultSpareDays : (data?.default_spare_days ?? 0);
}

/** One status line per card. Condemned leads with the amber dormancy pill, and the reason
 *  paragraph stands down WHENEVER the pill is present -- two status lines is noise whatever
 *  the reason says; the full sentences live in the panel. Sanctuary and Limbo wear their
 *  single short chip. */
/** Whether an item with no size is actually being kept out of plans right now.
 *
 *  `holdsBack` is only true while the allowance is off, which is its default. Above zero
 *  these items are reapable, and a card still promising "held back" would be telling the
 *  owner the opposite of what the plan will do. One shared query key, so this costs one
 *  request no matter how many cards ask.
 *
 *  An unknown or failed read answers TRUE, which is the safe default on a card (the size
 *  cell already says the size is unknown either way) and the WRONG one on a page that
 *  subtracts a count from a delete total -- so the read's state comes back with it, and a
 *  surface that states a number must render `isError` rather than a quietly adjusted one. */
export function useHoldsBackUnmeasured(): {
  holdsBack: boolean;
  isPending: boolean;
  isError: boolean;
} {
  const shared = useContext(QueueSettingsContext);
  const { data, isPending, isError } = useQuery({
    queryKey: ["profile"],
    queryFn: api.profile,
    enabled: shared === null,
    ...silentInsideProvider(shared),
  });
  // Inside the queue this comes from the one subscription above, read state and all -- so a
  // surface that states a number still gets the honest pending/error it must render.
  if (shared) return shared.unmeasured;
  return { holdsBack: (data?.max_unmeasured_per_run ?? 0) === 0, isPending, isError };
}
