// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Whether Reaper is allowed to delete right now, read the one way everywhere.

import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

/** The armed/read-only state, polled.
 *
 * Deletion is armed in the database and stays armed until someone turns it off, so this is
 * the one piece of state in the app that changes without this tab doing anything: arm it on
 * a phone and a desktop tab left on Review would go on saying "Reaper can look but can't
 * remove anything" until it was reloaded. That is the fail-open direction on the app's one
 * always-visible safety surface, so the answer is re-read on a clock and on returning to the
 * tab, overriding the app-wide `refetchOnWindowFocus: false` (main.tsx) that suits data which
 * only moves when a scan runs.
 *
 * The two settings cover each other, which is why both are here. React Query holds the interval
 * while the tab is hidden, so a background tab costs nothing and learns nothing; coming back to
 * it is itself a refetch, so the banner is right by the time anyone is reading it. A tab left
 * open and visible does not depend on that, and re-reads on the clock.
 *
 * 15s matches the idle scan-status poll: soon enough that the banner is not meaningfully
 * behind, cheap enough to run forever on every open tab.
 */
export function useSafety() {
  return useQuery({
    queryKey: ["safety"],
    queryFn: api.safety,
    refetchInterval: 15_000,
    refetchOnWindowFocus: true,
  });
}
