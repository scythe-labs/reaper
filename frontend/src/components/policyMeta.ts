// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Plain-English identities for every protection and signal, so the editor reads like a
// person wrote it instead of exposing the engine's field names. `unit` picks the control:
// a duration gets a value+unit picker, a rating a 0–10 box, a count a plain number.
//
// These live in their own module because both the policy editor and the simulator column
// beside it name protections, and neither should have to import the other to do it.
//
// The labels and help sentences below live in `locales/en/ui.json` under `policyMeta.*`.
// This is a data module, not a component, so it reads the catalog through the plain
// `i18next` import rather than the `useTranslation` hook. Each table is a FUNCTION, never a
// constant: a string resolved in a module body keeps whatever language was serving when the
// module first loaded (`i18n-module-scope.test.ts`).

import i18next from "../i18n";

// `window` marks a gate that counts activity inside a look-back window, so the editor
// renders the window as a control of its own. Without it, the server could warn about a
// `window_days` value the operator had no way to change.
export type GateMeta = {
  label: string;
  help: string;
  unit?: "days" | "people";
  /** What sits to the LEFT of the threshold box. "at least" is right for a floor and wrong
   *  for a fixed length: the came-back hold keeps a title for exactly this long, not for a
   *  minimum of it. Optional, so every existing row keeps the words it had. */
  lead?: string;
  /** The floor the threshold box clamps to, when it is not the dormancy gate's default of 5.
   *  Declared per gate, the same way `aria` below is: the server accepts a came-back hold of
   *  one day (`policy.GateSetting._protective_floors`), so a single shared floor would clamp
   *  that value up to five without telling the operator. */
  min?: number;
  /** `aria` is the window control's accessible name, and it is per-gate rather than fixed,
   *  because two gates use this slot for different questions: one asks how far back recent
   *  plays count, the other how long an absence has to be. A screen reader hearing the
   *  popularity gate's sentence on the came-back row would be told the wrong thing about the
   *  box it is standing on. */
  window?: { label: string; help: string; aria: string };
  /** No switch a policy can carry sits behind this id: a retired gate, a pseudo-id the API
   *  tallies under (`hand_spare`), or one the engine emits with no policy row behind it. The
   *  server still emits it, in stored explanations and in the simulator's spared-by counts, so
   *  its copy has to stay readable. The docs' protections-table guard excludes these, because
   *  that table lists the switches an operator can set. That does not mean the id is inert:
   *  `season_progression` fires on every TV scan, and `custom` fires on every keep rule the
   *  operator wrote, which is why they need copy at all.
   *
   *  It is also what `PolicyEditor` reads to decide that turning a row off removes it: a
   *  policy carrying one of these cannot be saved in either switch position. That makes this
   *  flag the browser's copy of `engine.gates.POLICY_AUTHORABLE_GATES`, and
   *  `tests/test_api_type_mirror.py` fails if either set drifts from the other, in either
   *  direction. */
  retired?: boolean;
};

/** Every gate id the engine can emit, mirroring `GateId` in `src/reaper/engine/gates.py`.
 *
 *  It exists to make `gateMeta` complete: the `satisfies` clause below turns a gate added to
 *  the engine with no operator copy into a compile error, instead of printing a raw id at
 *  whoever is deciding what to delete.
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
  | "rewatch_odds"
  | "returned"
  | "season_progression"
  | "custom";

/** What the spared-by list calls a protection this build has no copy for.
 *
 *  This fallback handles only an id the browser has never heard of: every id the engine emits
 *  today is named above. It is honest rather than blank, since those titles really were kept
 *  by something, and it never prints the raw id: a `titleCase` of the slug would put "Season
 *  Progression" or "Custom" in front of an operator choosing what to delete. */
export const unnamedGateLabel = () => i18next.t("policyMeta.unnamedGateLabel");

/** `mediaType` selects the rewatch-odds label's movie/TV wording, the same "tv"/"other"
 *  choice `policyEditor.rewatchCopy` already uses, since this is a whole-policy context, not
 *  a per-item one. Defaults to "movie" so a caller with no media context in scope keeps
 *  today's wording rather than needing a prop threaded for a label it never asks for.
 *  `PolicyEditor`'s generic `GateRow` is that caller: it never actually renders this id,
 *  since the rewatch card owns it through `rewatchCopy` below. */
export function gateMeta(mediaType: "movie" | "tv" = "movie"): Record<string, GateMeta> {
  return {
    min_dormancy: {
      label: i18next.t("policyMeta.gates.minDormancy.label"),
      help: i18next.t("policyMeta.gates.minDormancy.help"),
      unit: "days",
    },
    returned: {
      label: i18next.t("policyMeta.gates.returned.label"),
      help: i18next.t("policyMeta.gates.returned.help"),
      unit: "days",
      lead: i18next.t("policyMeta.gates.returned.lead"),
      min: 1,
      window: {
        label: i18next.t("policyMeta.gates.returned.window.label"),
        aria: i18next.t("policyMeta.gates.returned.window.aria"),
        help: i18next.t("policyMeta.gates.returned.window.help"),
      },
    },
    server_popularity: {
      label: i18next.t("policyMeta.gates.serverPopularity.label"),
      help: i18next.t("policyMeta.gates.serverPopularity.help"),
      unit: "people",
      window: {
        label: i18next.t("policyMeta.gates.serverPopularity.window.label"),
        aria: i18next.t("policyMeta.gates.serverPopularity.window.aria"),
        help: i18next.t("policyMeta.gates.serverPopularity.window.help"),
      },
    },
    rating_floor: {
      label: i18next.t("policyMeta.gates.ratingFloor.label"),
      help: i18next.t("policyMeta.gates.ratingFloor.help"),
    },
    // Rendered inside the rewatch card, not in the protections list: `PolicyEditor`'s gate
    // loop skips this id the way it skips `rating_floor`, and the card wires the same
    // stored gate row. The entry stays complete so the simulator's spared-by list and any
    // stored explanation name it properly.
    rewatch_odds: {
      label: i18next.t("policyMeta.gates.rewatchOdds.label", { mediaType }),
      help: i18next.t("policyMeta.gates.rewatchOdds.help"),
    },
    streaming_now: {
      label: i18next.t("policyMeta.gates.streamingNow.label"),
      help: i18next.t("policyMeta.gates.streamingNow.help"),
    },
    // Both gates are retired as switches: every list now protects through an `on_list` keep
    // rule, and the loader converts a stored body still carrying either
    // (`engine/policy_migrations.py convert_list_protections`). Their copy stays, because the
    // ids did not stop being emitted: any upgraded install has stored explanations carrying
    // both. Removing these entries would print the raw ids "Whitelisted" and "Curated List" at
    // the operator instead. The backend's own `_kept_phrase` keeps its two for exactly this
    // reason; this is its twin. The fallback above handles an id the browser has never heard
    // of, which is different from one whose copy was removed while the server still sends it.
    //
    // Each label says what THAT gate meant, matched to the field it actually reads: a label
    // taken from the wrong field would report a title kept by the IMDb Top 250, a list the
    // operator did not curate, as "On a list you curate yourself".
    whitelisted: {
      label: i18next.t("policyMeta.gates.whitelisted.label"),
      help: i18next.t("policyMeta.gates.whitelisted.help"),
      retired: true,
    },
    curated_list: {
      label: i18next.t("policyMeta.gates.curatedList.label"),
      help: i18next.t("policyMeta.gates.curatedList.help"),
      retired: true,
    },
    // Not a gate: `api/simulate.py` gives a hand spare its own id when it tallies what spared a
    // title, so the simulator names it as the hand spare it is. Reporting it under
    // `whitelisted` instead would make "Why titles were spared" show every hand spare as list
    // membership on a fresh install where no gate emits that id at all, and an operator reading
    // that could soften a list's keep rule believing it already covers those titles.
    hand_spare: {
      label: i18next.t("policyMeta.gates.handSpare.label"),
      help: i18next.t("policyMeta.gates.handSpare.help"),
      retired: true,
    },
    data_horizon: {
      // Tautulli can't see plays from before it was installed. The defense against that is the
      // dormancy clamp in fact derivation (`services/snapshot.py` `build_facts`,
      // `max(added_at, horizon)`), which runs regardless of this switch. `DataHorizonGate` can
      // never keep a file on its own: its `evaluate` only has a blocked branch and an abstain,
      // never a protect. Its one independent job is failing closed when the unwatched time is
      // Unknown, so the label names that instead of promising an outcome this switch does not
      // control.
      label: i18next.t("policyMeta.gates.dataHorizon.label"),
      help: i18next.t("policyMeta.gates.dataHorizon.help"),
    },
    // The four below carry no switch either. Two of them fire on ordinary scans, a season guard
    // and every operator-authored protect rule, so the spared-by list would otherwise print
    // "Season Progression" and "Custom", the engine's own vocabulary, in the panel an operator
    // reads while deciding what to delete. The other two are retired and reach the tally only
    // through an explanation an older scan stored.
    //
    // `gateMeta` is complete over `GateId` by construction (the `satisfies` clause below), so
    // an id that can never appear still costs one label, and one that can appear is always
    // named, rather than deciding per id which readers need it.
    //
    // `PolicyEditor`'s leftover-row notice is about a gate whose LIST is gone. `whitelisted`
    // and `curated_list` reach it: `api/policy.py`'s `_policy_out` serves each loaded row
    // through `GateSettingOut`, the same model without the save boundary's refusal, so a stored
    // `whitelisted` row arrives there instead of being dropped with its route.
    //
    // Of the four below, `unmanaged` and `others_watching` cannot reach that notice at all:
    // `PolicyBody._drop_retired_gates` strips both from every stored body on load. The other
    // two can, only from a hand-edited row: `GateSettingIn._must_be_authorable` refuses
    // `season_progression` and `custom` on every save, and no shim writes them. There the
    // notice's "Add its list again" clause names nothing, since `scan_runner.build_gates`
    // cannot build either id, and turning the row off removes it, which is the exit that
    // actually works. Keying the notice on a second, browser-side list of which ids are
    // list-shaped would cost a set that can drift from the server's.
    //
    // The season_progression label avoids "Your season rules", which names a control that does
    // not always exist. Every season on disk is held under this id when the guard cannot be
    // answered at all: `progress_is_establishable` is False whenever the watch mirror reaches
    // back less far than `in_progress_hold_days` (180 days by default), so a new install is in
    // that state for its whole library. The hold is a blocked protect
    // (`season_evidence.guard_result`), and `Evaluation.protectors` selects on the outcome
    // alone, so it lands in `protections_fired` and tallies here like any keep. An operator
    // told "your season rules" could loosen keep-last, keep-first, and the partway-through
    // guard in turn and watch the number hold still, since none of those rules caused it.
    // `api/review.py`'s `_kept_phrase` already refuses to say it for the same rows ("your watch
    // history is too short to tell"), so this label uses the same wording so the two surfaces
    // agree.
    season_progression: {
      label: i18next.t("policyMeta.gates.seasonProgression.label"),
      help: i18next.t("policyMeta.gates.seasonProgression.help"),
      retired: true,
    },
    custom: {
      label: i18next.t("policyMeta.gates.custom.label"),
      help: i18next.t("policyMeta.gates.custom.help"),
      retired: true,
    },
    others_watching: {
      // The help says no title was ever kept this way, because none was: no fact builder ever
      // produced a Known count, so the gate's floor of at least 1 was never met and it could
      // never protect. The label is what a stored explanation would read as; the help must not
      // invent a keep behind it.
      label: i18next.t("policyMeta.gates.othersWatching.label"),
      help: i18next.t("policyMeta.gates.othersWatching.help"),
      retired: true,
    },
    unmanaged: {
      // The wording the review queue's chip uses for the same id (`api/review.py`'s
      // `_kept_phrase`), so the two surfaces say one thing. Reaper builds its candidate list by
      // asking Sonarr and Radarr what they hold, so nothing lands here now.
      label: i18next.t("policyMeta.gates.unmanaged.label"),
      help: i18next.t("policyMeta.gates.unmanaged.help"),
      retired: true,
    },
  } satisfies Record<GateId | "hand_spare", GateMeta>;
}

export function signalMeta(): Record<string, { label: string; help: string }> {
  return {
    unwatched: {
      label: i18next.t("policyMeta.signals.unwatched.label"),
      // "Untouched", never "since anyone played it": `engine/dormancy.py`'s
      // `reference_instant` measures from `last_played`, else from `max(added_at, horizon)`,
      // else from nothing at all. A play is one of two possible anchors, and an item with
      // neither is Unknown rather than scored. This copy teaches the control, and the recipe in
      // `docs/content/understandingPolicy.ts` points operators at this signal for never-played
      // backlog, exactly the titles whose clock starts at arrival instead.
      //
      // No static example of "full points at the far end" belongs here: the card already shows
      // that with the operator's own numbers, two labeled bound boxes plus a worked example the
      // engine answers against a real value. A written-in example would go stale the moment
      // they moved either end of the ramp.
      help: i18next.t("policyMeta.signals.unwatched.help"),
    },
    few_watchers: {
      label: i18next.t("policyMeta.signals.fewWatchers.label"),
      help: i18next.t("policyMeta.signals.fewWatchers.help"),
    },
    season_rank: {
      label: i18next.t("policyMeta.signals.seasonRank.label"),
      help: i18next.t("policyMeta.signals.seasonRank.help"),
    },
    low_rating: {
      label: i18next.t("policyMeta.signals.lowRating.label"),
      help: i18next.t("policyMeta.signals.lowRating.help"),
    },
    size: {
      label: i18next.t("policyMeta.signals.size.label"),
      // At any non-zero weight, this signal decides which titles are removed, not just ranks
      // among titles the score already chose.
      help: i18next.t("policyMeta.signals.size.help"),
    },
  };
}

export function titleCase(id: string): string {
  return id.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
