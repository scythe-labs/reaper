// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one declaration of the background scan's status read.
//
// The polling interval is declared once here, so a new caller reuses it rather than picking
// the same number again.

import { useQuery } from "@tanstack/react-query";

import { api, type ScanStatus } from "./api";

/** How often a running scan is re-read. Fast, because the bar it feeds is watched. */
const RUNNING_POLL_MS = 1000;

/** A surface that wants the scan's live progress.
 *
 *  This observer polls only while a scan is running, but the query itself does not go quiet
 *  the rest of the time: React Query runs one timer per observer, and `Dashboard` holds a
 *  second observer on this key with a 15s idle poll, so anywhere below the app shell the key
 *  keeps being read whatever this returns. The setup wizard is the exception, because
 *  `Authed` renders the wizard or the Dashboard, so there the polling really does stop.
 *
 *  Two readers deliberately do not call this. `Dashboard` declares the idle poll itself, which
 *  is a different job: it is what surfaces a scan the scheduler or another device started. And
 *  `ReapBreakdown` reads the cache with no interval at all, so the ledger costs no request of
 *  its own. */
export function useScanStatus(): ScanStatus | undefined {
  return useQuery({
    queryKey: ["scanStatus"],
    queryFn: api.scanStatus,
    refetchInterval: (query) => (query.state.data?.running ? RUNNING_POLL_MS : false),
  }).data;
}
