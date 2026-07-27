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
    // Was "Don't judge what predates your history", promising an outcome this switch does
    // not control. Tautulli can't see plays from before it was installed, and the defense
    // against that is the dormancy CLAMP in fact derivation (`services/snapshot.py`
    // `build_facts`, `max(added_at, horizon)`), which runs in either switch position.
    // `DataHorizonGate` can never keep a file -- its `evaluate` has a blocked branch and an
    // abstain, no PROTECT -- and its one independent job is failing closed when the
    // unwatched time is Unknown. So the switch is named for that (rules 25, 21).
    label: "Stop if the unwatched time can't be read",
    help: "A title Reaper couldn't measure is left alone rather than judged. Either way, unwatched time is never counted further back than your history goes, so nothing older than it looks never-watched.",
  },
  // `unmanaged` ("Only touch what Sonarr or Radarr manages") was here. Its gate is retired
  // (see `engine/gates.py`): Reaper builds its candidate list by asking Sonarr and Radarr
  // what they hold, so a file neither owns can never reach the set that protection filtered,
  // and the switch did nothing in either position.
  //
  // Removing the entry is safe because neither reader can ask for it, NOT because of the
  // `titleCase` fallback both readers carry. `PolicyEditor` maps the served policy body,
  // which no longer holds the gate; `PolicySimulator` maps `protected_by`, which needs a
  // PROTECT result this gate could never return. A stored explanation's protection rows do
  // not come through here at all: `WhyPanel` renders the backend's own `detail` text. If the
  // fallback were ever reached it would print a bare "Unmanaged" into operator copy, which
  // rule 21 does not allow, so it is a backstop and not the reason this is safe.
};

export const SIGNAL_META: Record<string, { label: string; help: string }> = {
  unwatched: {
    label: "How long it's gone unwatched",
    // Was "The biggest single signal", which describes the shipped mix rather than the
    // control, and goes stale the first time the operator moves a slider.
    //
    // "Untouched", never "since anyone played it": `engine/dormancy.py`'s
    // `reference_instant` is `last_played or max(added_at, horizon)`, so a play is one of
    // three anchors. This is the copy that TEACHES the control, and the recipe in
    // `docs/content/understandingPolicy.ts` points operators at this signal for
    // never-played backlog -- exactly the titles whose clock starts at arrival instead
    // (rule 72, with the review chip fixed for the same divergence).
    help: "The longer it sits untouched, the stronger the reason to remove it. The clock starts at the last play, or at the day it arrived when there has never been one. It earns its full points only at the far end.",
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
