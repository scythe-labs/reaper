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
