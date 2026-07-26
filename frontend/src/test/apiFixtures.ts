// SPDX-License-Identifier: AGPL-3.0-or-later
// Payloads for the settings reads a component tree performs on its own.
//
// A test that replaces `../api` wholesale hands every consumer `undefined` for whatever the
// mock omits -- including hooks the test never names. `useHoldsBackUnmeasured` reads
// ["profile"], the queue and the shell read ["general-settings"], the breakdown reads
// ["scanStatus"]. React Query answers a missing queryFn with an error state, so the tree
// quietly renders its "we could not read it" branch and the test passes for the wrong reason.
// Rule 135 fails the run on that; these are what the mocks return instead.
//
// The values are the shipped defaults, picked so a tree that was silently rendering the failed
// read keeps rendering the same thing: no unmeasured allowance (so unmeasured items are still
// held back), seasons collapsed, spares kept forever. A test that cares about any of these sets
// its own value -- these exist so a test that does NOT care never has to think about them.
import type { QueryClient } from "@tanstack/react-query";
import type { GeneralSettings, ProfileSettings, ScanStatus } from "../api";

export const DEFAULT_PROFILE: ProfileSettings = {
  max_items_per_run: 10,
  max_bytes_per_run: 1024 ** 4,
  max_items_per_30d: 100,
  max_bytes_per_30d: 10 * 1024 ** 4,
  caps_enabled: true,
  grace_days: 14,
  max_unmeasured_per_run: 0,
};

export const DEFAULT_GENERAL: GeneralSettings = {
  application_name: "Reaper",
  application_url: null,
  timezone: "UTC",
  accent_color: "#38bdf8",
  api_key_set: false,
  expand_seasons_default: false,
  default_spare_days: 0,
  proxy_trust_enabled: false,
  trusted_proxies: [],
};

/** Nothing running -- the shape `api.scanStatus` returns between scans. */
export const IDLE_SCAN: ScanStatus = {
  running: false,
  phase: "idle",
  done: 0,
  total: 0,
  percent: 0,
  detail: "",
  error: null,
  snapshot_id: 1,
  followup_queued: false,
};

/** Put those settings in the cache, fresh, for a tree that reads them on its own and a test that
 *  does not care what they say -- it renders with them applied from its first paint.
 *
 *  Mocking `api.profile` answers the read, but the answer arrives a microtask later, after a
 *  synchronous test body has finished asserting: such a test states its expectation about the
 *  panel while the settings are still pending, which is not the panel any operator sees, and the
 *  update lands after the test (rule 135). Seeding is the same payload, delivered at the only
 *  moment a sync test can read it. Keep the mock as well, for whatever refetches later.
 *
 *  Never seed a key the test varies. Fresh cached data means no fetch, so seeding a suite that
 *  rejects `api.profile` or holds it pending -- ReapBreakdown and PolicyEditor both do -- would
 *  quietly answer the read it is trying to fail, and the test would prove nothing. */
export function seedSettings(client: QueryClient): QueryClient {
  client.setQueryDefaults(["profile"], { staleTime: Infinity });
  client.setQueryDefaults(["general-settings"], { staleTime: Infinity });
  client.setQueryData(["profile"], DEFAULT_PROFILE);
  client.setQueryData(["general-settings"], DEFAULT_GENERAL);
  return client;
}
