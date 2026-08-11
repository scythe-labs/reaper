// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one declaration of the general-settings read.
//
// Five components asked for `["general-settings"]` by hand under three different staleTimes: the
// app default of 30s, plus a 60s and a 5-minute override neither of which said why. Freshness is
// one decision for a value the settings panel writes and four other surfaces render, so it is
// made once here, and it is the app-wide default in `main.tsx` rather than a fourth number.

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { api, type GeneralSettings } from "./api";

/** The install's General settings.
 *
 *  `subscribed: false` for a caller that already has the values from somewhere else: the query
 *  then neither fetches nor re-renders its component when the cache is written. `enabled` alone
 *  stops only the fetch, leaving every holder subscribed to every write, which is the fan-out the
 *  queue's settings provider exists to remove. */
export function useGeneralSettings(subscribed = true): UseQueryResult<GeneralSettings> {
  return useQuery({
    queryKey: ["general-settings"],
    queryFn: api.general,
    enabled: subscribed,
    ...(subscribed ? {} : { notifyOnChangeProps: [] as [] }),
  });
}
