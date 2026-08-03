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
import type {
  GeneralSettings,
  ProfileSettings,
  ScanStatus,
  SetupStatus,
  Update,
  WatchEvidence,
} from "../api";

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
  expand_seasons_mode: "off",
  default_spare_days: 0,
  proxy_trust_enabled: false,
  trusted_proxies: [],
  // Not a desktop build: the shape every install other than the Mac and Windows
  // apps reports, and the one that renders no Desktop app group.
  desktop: null,
};

/** No watch record held, and no scan has counted: the state a fresh install is in, and the
 *  one that renders the Plex panel's watch-history group at rest.
 *
 *  `held_back` is `null`, not `0`, because that is what the route returns with no snapshot
 *  rows. A fixture stating `0` here would be the one shape a fresh database cannot produce,
 *  and every test that does not care about this read would render the "counted none" sentence
 *  instead of the "not recorded" one. */
export const DEFAULT_WATCH_EVIDENCE: WatchEvidence = { titles: 0, held_back: null };

/** The update check with nothing to say: enabled but unanswered, which renders no
 *  pill, no chip light, no banner -- the same nothing a tree rendered before the check
 *  existed. A test that cares about updates sets its own payload; this exists so every
 *  test that does not care never has to think about it. */
export const DEFAULT_UPDATE: Update = {
  channel: "release",
  enabled: true,
  current: "0.1.0",
  latest: null,
  update_available: null,
  url: null,
  checked_at: null,
  changes: [],
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

/** A fully configured install: everything connected, a scan behind it, and a real run allowed.
 *
 *  The Reap page reads this on its own to say, before the button, what would turn a run away
 *  (`reapReadiness.ts`). `reap_ready: true` is the value that renders NOTHING extra, which is
 *  the point: a test that does not care about setup keeps rendering the page it always did,
 *  and a test that does care sets its own. A failed read is not the same as this and does not
 *  substitute for it -- the page says so out loud, which is why the fixture cannot be omitted
 *  and left to the mock gap (rule 135). */
export const READY_SETUP: SetupStatus = {
  admin_exists: true,
  has_password: true,
  plex_linked: true,
  instances: { radarr: 1, sonarr: 1, tautulli: 1 },
  has_radarr: true,
  has_sonarr: true,
  has_tautulli: true,
  has_seerr: false,
  has_scanned: true,
  scan_ready: true,
  reap_ready: true,
  complete: true,
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
