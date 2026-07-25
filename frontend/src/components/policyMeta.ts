// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Plain-English identities for every protection and signal, so the editor reads like a
// person wrote it instead of exposing the engine's field names. `unit` picks the control:
// a duration gets a value+unit picker, a rating a 0–10 box, a count a plain number.
//
// These live in their own module because both the policy editor and the simulator column
// beside it name protections, and neither should have to import the other to do it.

// `window` marks a gate that counts activity inside a look-back window, so the editor
// renders the window as a control of its own. Without it the server could warn about a
// `window_days` the operator had no way to change (U-9, rules 42/25).
export type GateMeta = {
  label: string;
  help: string;
  unit?: "days" | "people";
  window?: { label: string; help: string };
};

export const GATE_META: Record<string, GateMeta> = {
  min_dormancy: {
    label: "Give every title time to be rewatched",
    help: "Nothing is removed until it has gone at least this long without a single play. Under about three years, people still circle back to a title surprisingly often.",
    unit: "days",
  },
  server_popularity: {
    label: "Keep what your users actually watch",
    help: "If at least this many different people have played it recently, it stays, whatever it scored.",
    unit: "people",
    window: {
      label: "counting plays from the last",
      help: "How far back “recently” reaches. A year is the usual setting. Make it much shorter and almost nothing counts as watched, so this protection stops catching anything.",
    },
  },
  rating_floor: {
    label: "Keep well-rated titles",
    help: "A title well rated on any source you trust is kept.",
  },
  streaming_now: {
    label: "Never touch something playing right now",
    help: "Re-checked in the seconds before any removal, not just at scan time.",
  },
  whitelisted: {
    label: "Spare titles you've tagged",
    help: "A title carrying one of these tags in Sonarr/Radarr is kept, whatever it scores. (A ‘Never Reap’ Plex collection is honored too.)",
  },
  curated_list: {
    label: "Honor protected lists",
    help: "Right now this is the IMDb Top 250. Anything on it is kept.",
  },
  data_horizon: {
    label: "Don't judge what predates your history",
    help: "Tautulli can't see plays from before it was installed, so anything older than your history is left alone rather than assumed unwatched.",
  },
  unmanaged: {
    label: "Only touch what Sonarr or Radarr manages",
    help: "If Sonarr or Radarr doesn't own the file, Reaper has no safe way to remove it.",
  },
};

export const SIGNAL_META: Record<string, { label: string; help: string }> = {
  unwatched: {
    label: "How long it's gone unwatched",
    // Was "The biggest single signal", which describes the shipped mix rather than the
    // control, and goes stale the first time the operator moves a slider.
    help: "The longer since anyone played it, the stronger the reason to remove it. It earns its full points only at the far end.",
  },
  few_watchers: {
    label: "How few people watch it",
    help: "Fewer recent watchers means more pressure to remove it.",
  },
  season_rank: {
    label: "How old a season is",
    help: "Older seasons of a show carry more pressure than the newest one. The season floor below still wins.",
  },
  low_rating: {
    label: "How low it's rated",
    help: "A poorly-rated title carries a little more pressure.",
  },
  size: {
    label: "How big it is on disk",
    // Was "It only ranks titles the score has already chosen", which stops being true the
    // moment it carries points: at any non-zero weight it decides, not just ranks.
    help: "Off by default. Big files are usually big because they're popular, so size makes a poor reason to delete. Give it points and it becomes one.",
  },
};

export function titleCase(id: string): string {
  return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
