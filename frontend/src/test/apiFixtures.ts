// SPDX-License-Identifier: AGPL-3.0-or-later
// Payloads for the settings reads a component tree performs on its own.
//
// A test that replaces `../api` wholesale hands every consumer `undefined` for whatever the
// mock omits, including hooks the test never names. `useHoldsBackUnmeasured` reads ["profile"],
// the queue and the shell read ["general-settings"], and the breakdown reads ["scanStatus"].
// React Query answers a missing queryFn with an error state, so the tree quietly renders its
// "could not read it" branch and the test passes for the wrong reason. A test suite check
// fails the run on that. These are what the mocks return instead.
//
// The values are the shipped defaults, picked so a tree that was silently rendering the failed
// read keeps rendering the same thing. There is no unmeasured allowance, so unmeasured items
// are still held back, seasons stay collapsed, and spares are kept forever. A test that cares
// about any of these sets its own value. These exist so a test that does not care never has to
// think about them.
import type { QueryClient } from "@tanstack/react-query";
import type {
  AuthUser,
  FieldValues,
  GeneralSettings,
  PlexStatus,
  ProfileSettings,
  ScanStatus,
  SetupStatus,
  Snapshot,
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
  // A tag already stored, so no fixture-backed render trips `useSeedLanguage` into a save.
  language: "en",
  api_key_set: false,
  expand_seasons_mode: "off",
  default_spare_days: 0,
  proxy_trust_enabled: false,
  trusted_proxies: [],
  // This is not a desktop build. It is the shape every install other than the Mac and Windows
  // apps reports, and it is the one that renders no Desktop app group.
  desktop: null,
};

/** No watch record held, and no scan has counted. This is the state a fresh install is in, and
 *  it renders the Plex panel's watch-history group at rest.
 *
 *  `held_back` is `null`, not `0`, because that is what the route returns with no snapshot
 *  rows. A fixture stating `0` here would be the one shape a fresh database cannot produce,
 *  and every test that does not care about this read would render the "counted none" sentence
 *  instead of the "not recorded" one. */
export const DEFAULT_WATCH_EVIDENCE: WatchEvidence = { titles: 0, held_back: null };

/** The update check with nothing to say. Enabled but unanswered, this renders no pill, no chip
 *  light, and no banner, the same nothing a tree rendered before the check existed. A test that
 *  cares about updates sets its own payload. This exists so every test that does not care never
 *  has to think about it. */
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

/** A linked server with nothing else said about it, and an empty library list.
 *
 *  Both fields exist to keep every read answered rather than for convenience. An unanswered
 *  `api.plexStatus` or `api.plexLibraries` renders the same could-not-read branch a real
 *  failure does, and a test can then assert against that branch believing it is the app.
 *  `linked: true` is the state the wizard's Plex step is about. A file whose subject is the
 *  unlinked step sets its own.
 *
 *  The library list is empty rather than populated because a library row is what several of
 *  these tests are about, and a fixture that painted rows would answer a read a test means to
 *  make itself. */
export const DEFAULT_PLEX_STATUS: PlexStatus = {
  linked: true,
  name: "Example Server",
  connection_uri: "http://plex.example:32400",
  last_ok_at: null,
  verify_tls: true,
  web_url: "https://app.plex.tv",
};

/** No suggestions, which is what a fresh scan with nothing distinct to offer really returns.
 *  The route's own docstring says an unknown field or a missing scan returns an empty list, and
 *  typing an unlisted value stays valid either way. A tree rendered against this is the tree an
 *  operator can genuinely see, and a filter suggester falls back to free text rather than to
 *  its failed-read branch.
 *
 *  This fixture exists because a mock that answers nothing renders the same failed-read branch
 *  with nothing to say so. `queryFn: () => api.vocabularyValues(f)` is an arrow function, so
 *  the queryFn is present and throws inside it, which React Query files as an ordinary
 *  rejection. */
export const DEFAULT_FIELD_VALUES: FieldValues = { field: "", values: [] };

/** An ordinary finished scan, nothing degraded. This is the shape `api.latestSnapshot` returns
 *  most of the time. `collection_sizes` is left absent, which is the honest default. Most tests
 *  never put a candidate in a collection, and the queue's card pickers read this
 *  unconditionally now, so a tree that does not care about it still needs an answer rather than
 *  the failed-read branch. A test about a collection's own size sets its own. */
export const DEFAULT_SNAPSHOT: Snapshot = {
  id: 1,
  created_at: "2026-01-01T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 0,
  degraded: false,
  degraded_reason: null,
  degraded_doc: null,
  condemned: 0,
  protected: 0,
  abstained: 0,
  reclaimable_bytes: 0,
  unknown_size_items: 0,
};

/** Nothing running. This is the shape `api.scanStatus` returns between scans. */
export const IDLE_SCAN: ScanStatus = {
  running: false,
  phase: "idle",
  done: 0,
  total: 0,
  percent: 0,
  detail_reason: null,
  error_reason: null,
  snapshot_id: 1,
  followup_queued: false,
};

/** A fully configured install. Everything is connected, a scan is behind it, and a real run is
 *  allowed.
 *
 *  The Reap page reads this on its own to say, before the button, what would turn a run away
 *  (`reapReadiness.ts`). `reap_ready: true` is the value that renders nothing extra, which is
 *  the point. A test that does not care about setup keeps rendering the page it always did, and
 *  a test that does care sets its own. A failed read is not the same as this and does not
 *  substitute for it, since the page says so out loud, which is why this fixture cannot be
 *  omitted and left to an unanswered mock. */
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

/** An ordinary signed-in admin, for a panel that reads ["me"] on its own.
 *
 *  `via_recovery: false` is both the shipped default and the strict setting. The Security
 *  panel's current-password box stays live and required, which is the form every test here was
 *  written against. A test about the recovery path sets its own user rather than editing this,
 *  so loosening the gate can never happen by accident in a suite that is about something else. */
export const SIGNED_IN_USER: AuthUser = {
  id: 1,
  username: "owner",
  provider: "local",
  thumb_url: null,
  via_recovery: false,
};

/** Puts those settings in the cache, fresh, for a tree that reads them on its own and a test
 *  that does not care what they say. The tree renders with them applied from its first paint.
 *
 *  Mocking `api.profile` answers the read, but the answer arrives a microtask later, after a
 *  synchronous test body has finished asserting. Such a test would state its expectation about
 *  the panel while the settings are still pending, which is not the panel any operator sees,
 *  since the update lands after the test. Seeding is the same payload, delivered at the only
 *  moment a synchronous test can read it. Keep the mock as well, for whatever refetches later.
 *
 *  Never seed a key the test varies. Fresh cached data means no fetch, so seeding a suite that
 *  rejects `api.profile` or holds it pending, which ReapBreakdown and PolicyEditor both do,
 *  would quietly answer the read it is trying to fail, and the test would prove nothing. */
export function seedSettings(client: QueryClient): QueryClient {
  client.setQueryDefaults(["profile"], { staleTime: Infinity });
  client.setQueryDefaults(["general-settings"], { staleTime: Infinity });
  client.setQueryData(["profile"], DEFAULT_PROFILE);
  client.setQueryData(["general-settings"], DEFAULT_GENERAL);
  return client;
}
