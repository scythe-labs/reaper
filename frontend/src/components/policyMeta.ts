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
  /** No switch a policy can carry sits behind this id -- either a retired gate, or a
   *  pseudo-id the API tallies under (`hand_spare`) -- but the server still emits it, in
   *  stored explanations and in the simulator's spared-by counts, so its copy has to stay
   *  readable. The docs' protections-table guard excludes these: they do not ship today. */
  retired?: boolean;
};

/** Every gate id the engine can emit, mirroring `GateId` in `src/reaper/engine/gates.py`.
 *
 *  It exists to make `GATE_META` complete: the `satisfies` clause below turns a gate added
 *  to the engine without operator copy into a compile error, where before it was a raw id
 *  printed at whoever was deciding what to delete (#551, rules 21 and 103).
 *
 *  The union is the mirror, so it needs its own guard: `tests/test_api_type_mirror.py`'s
 *  `TestEveryGateIdHasOperatorCopy` pins it against the enum, both directions. */
export type GateId =
  | "whitelisted"
  | "streaming_now"
  | "rating_floor"
  | "server_popularity"
  | "others_watching"
  | "curated_list"
  | "data_horizon"
  | "unmanaged"
  | "min_dormancy"
  | "season_progression"
  | "custom";

/** What the spared-by list calls a protection this build has no copy for.
 *
 *  Rule 66's fallback, and it handles an id the browser has never heard of only: every id
 *  the engine emits today is named above. Honest rather than blank -- those titles really
 *  were kept by something -- and it never prints the id, which is the whole of #551: a
 *  `titleCase` of the slug put "Season Progression" and "Custom" in front of an operator
 *  choosing what to delete (rule 21). */
export const UNNAMED_GATE_LABEL = "Another protection";

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
  // Both gates are RETIRED as switches -- every list now protects through an `on_list` keep
  // rule, and the loader converts a stored body still carrying either
  // (`engine/policy_migrations.py convert_list_protections`). Their copy stays, because the ids did not
  // stop being emitted: any upgraded install has stored explanations carrying both. Deleting
  // the entries printed "Whitelisted" and "Curated List" at the operator (rule 21), which is
  // what the fallback did with any id before #551. The backend's own `_kept_phrase` kept its
  // two for exactly this reason; this is its twin (rules 64, 72). Rule 66's fallback is for an
  // id the browser has never heard of, not one whose copy was removed while the server still
  // sends it.
  //
  // Each label says what THAT gate meant, taken from the field it read. An earlier pass took
  // the two labels from `engine/fields.py` in source order and landed them on the wrong ids,
  // so a title kept by the IMDb Top 250 -- the one list the operator demonstrably did not
  // curate -- reported "On a list you curate yourself" (rule 21).
  whitelisted: {
    label: "On a list you curate yourself",
    help: "Kept because it is on a tag list, a Plex collection, or your watchlist.",
    retired: true,
  },
  curated_list: {
    label: "On a protected list",
    help: "Kept because it is on a ready-made list Reaper syncs, like the IMDb Top 250.",
    retired: true,
  },
  // Not a gate: `api/simulate.py` gives a hand spare its own id when it tallies what spared a
  // title, so the simulator names it as the hand spare it is. It rode in on `whitelisted`
  // once, which on a fresh install -- where no gate emits that id at all -- made "Why titles
  // were spared" report every hand spare as list membership, and an operator reading that
  // can soften a list's keep rule believing it covers them (rule 144).
  hand_spare: {
    label: "Spared by hand",
    help: "You spared these titles yourself, so the scan left them alone.",
    retired: true,
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
  // The four below carry no switch either, and each was missing until #551. Two of them fire
  // on ordinary scans -- a season guard, and every operator-authored protect rule -- so the
  // spared-by list printed "Season Progression" and "Custom" as the reasons titles were kept,
  // which is the engine's vocabulary in the panel read while deciding what to delete
  // (rule 21). The other two are retired and reach the tally only out of an explanation an
  // older scan stored.
  //
  // The entry is no longer an argument about which reader can ask for which id: `GATE_META`
  // is complete over `GateId` by construction (the `satisfies` below), so an id that cannot
  // appear costs one label, and one that can is named. That is the trade the earlier removal
  // of `unmanaged` got backwards -- it reasoned each reader out, correctly, and left the next
  // id added to the engine to be printed raw.
  //
  // None of the four can reach `PolicyEditor`'s leftover-row notice, whose copy is about a
  // gate whose LIST is gone: `PolicyBody._drop_retired_gates` strips `unmanaged` and
  // `others_watching` from every stored body on load, and `GateSettingIn._must_be_authorable`
  // refuses `season_progression` and `custom`, which no policy row builds.
  season_progression: {
    label: "Your season rules",
    help: "The newest seasons you keep, a show still running, or someone partway through.",
    retired: true,
  },
  custom: {
    label: "A rule you wrote",
    help: "One of your own keep rules matched these titles.",
    retired: true,
  },
  others_watching: {
    label: "Other people were watching it",
    help: "Kept by a scan that counted who else was watching. Reaper no longer checks this.",
    retired: true,
  },
  unmanaged: {
    // The wording the review queue's chip uses for the same id (`api/review.py`'s
    // `_kept_phrase`), so the two surfaces say one thing (rule 144). Reaper builds its
    // candidate list by asking Sonarr and Radarr what they hold, so nothing lands here now.
    label: "Not managed by Sonarr or Radarr",
    help: "Kept by a scan that looked outside what Sonarr and Radarr hold.",
    retired: true,
  },
} satisfies Record<GateId | "hand_spare", GateMeta>;

export const SIGNAL_META: Record<string, { label: string; help: string }> = {
  unwatched: {
    label: "How long it's gone unwatched",
    // Was "The biggest single signal", which describes the shipped mix rather than the
    // control, and goes stale the first time the operator moves a slider.
    //
    // "Untouched", never "since anyone played it": `engine/dormancy.py`'s
    // `reference_instant` measures from `last_played`, else from `max(added_at, horizon)`,
    // else from nothing at all -- so a play is one of two anchors, and an item with neither
    // is Unknown rather than scored. This is the copy that TEACHES the control, and the
    // recipe in
    // `docs/content/understandingPolicy.ts` points operators at this signal for
    // never-played backlog -- exactly the titles whose clock starts at arrival instead
    // (rule 72, with the review chip fixed for the same divergence).
    // The third sentence used to be "It earns its full points only at the far end." It is
    // gone because the card now says that with the operator's own numbers below it: the two
    // labeled bound boxes, then a worked example the engine answers against a real value. A
    // vaguer second copy of a fact computed live underneath it is what drifts (rule 144). A
    // static example written in here would be the same trap: it would go stale the moment
    // they moved either end of the ramp.
    help: "The longer it sits untouched, the stronger the reason to remove it. The clock starts at the last play, or at the day it arrived when there has never been one.",
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
