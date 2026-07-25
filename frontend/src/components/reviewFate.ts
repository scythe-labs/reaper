// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What is actually going to happen to an item, and what a hand control may therefore offer.
//
// These are the primitives rules 48 and 49 are written in, and the whole app -- the queue, the
// why panel, the show panel -- has to agree on them, so they live here rather than inside the
// queue page that used to hold them (R-1). Nothing in this file renders or fetches: it reads
// the fields a candidate already carries and answers one question each.
//
// The shapes are structural on purpose. A season row, a movie card, a show panel's season and a
// group's mark all carry the same three fields, and none of them needs to be the same type to
// ask these questions.

import type { Override, Verdict } from "../api";

/** Which color a score badge or strip square wears once a hand decision is in play.
 *  A hand SPARE, or a reap the engine honors, shows SOLID -- "you chose this" -- so the
 *  cell states the item's real fate, not just Reaper's first read. A reap the engine
 *  can't honor yet ("refused") reads DASHED RED, never solid: the ask is noted, the file is
 *  still held. Amber is no longer used here at all -- it means only "left for you to decide"
 *  (the abstain `status-look` chip). Anything untouched keeps its scan verdict. Shared by the
 *  score badge and the season strip so the two can never disagree with each other or with
 *  the row's chip. */
export type Fate = Verdict | "reap" | "spare" | "refused";
export function handFate(item: {
  verdict: Verdict;
  override: Override | null;
  override_effective: boolean | null;
}): Fate {
  if (item.override === "spare") return "spare";
  if (item.override === "reap") return item.override_effective === false ? "refused" : "reap";
  return item.verdict;
}

/** Whether the row a card leads with is on the block. One expression for both card
 *  shapes: a show card reads its first (highest-scoring) season, a movie card reads
 *  itself, and neither may drift into asking the question a different way. */
export function isCondemned(item: {
  verdict: Verdict;
  override: Override | null;
  override_effective: boolean | null;
}): boolean {
  // The item's EFFECTIVE fate (will it be removed), not its raw (now pure-policy) verdict: a hand
  // spare and a held hand reap keep it, a honored hand reap condemns it. Routed through handFate so
  // this and the colors can't disagree (rule 49). Drives the card's condemned styling and the
  // which-tab proxy -- NOT hideReap, which asks a different question (see reapIsNoop).
  const fate = handFate(item);
  return fate === "condemn" || fate === "reap";
}

/** Whether a hand Reap on this row would change nothing, so its Reap control is dropped (rule 48).
 *  Reaping is a no-op only when POLICY already condemns the item (its pure verdict is "condemn")
 *  and the operator has not spared it: a spared row -- including a season kept by a whole-show
 *  spare -- can still be flipped to Reap, and a row condemned only by the operator's OWN reap
 *  keeps that control so the reap can be undone (its pure verdict is not "condemn"). The spare-
 *  aware form of rule 48's "own verdict === condemn"; never reimplement it inline. */
export function reapIsNoop(item: { verdict: Verdict; override: Override | null }): boolean {
  return item.verdict === "condemn" && item.override !== "spare";
}

/** Whether a show-level reap actually takes anywhere: true when any season's reap is honored,
 *  false when every one is refused, undefined outside a reap override. Feeds the whole-show
 *  override chip's `effective` flag. Judges over the WHOLE show (`group_seasons`), every lane,
 *  never the tab-filtered page. */
export function groupReapEffective(
  items: ReadonlyArray<{ override: Override | null; override_effective: boolean | null }>,
): boolean | undefined {
  const reaped = items.filter((s) => s.override === "reap");
  if (reaped.length === 0) return undefined;
  return reaped.some((s) => s.override_effective !== false);
}

/** How far a whole-show reap actually reaches across its seasons, for the inherit banner's
 *  wording (rule 61): "all" every inherited reap is honored, "none" every one is held for now,
 *  "some" a mix. Reads the same override/override_effective fields as groupReapEffective, over
 *  the seasons the panel already has. Only meaningful when the show's own override is reap. */
export function showReapReach(
  seasons: ReadonlyArray<{ override: Override | null; override_effective: boolean | null }>,
): "all" | "some" | "none" {
  const reaped = seasons.filter((s) => s.override === "reap");
  if (reaped.length === 0) return "all"; // no inherited reap to hold; nothing to qualify
  const held = reaped.filter((s) => s.override_effective === false).length;
  if (held === 0) return "all";
  if (held === reaped.length) return "none";
  return "some";
}

/** Whether a whole-show reap actually reaches this season, for the card's removal count.
 *
 *  A whole-show decision is not atomic. A season carrying its own spare keeps it (rule 50),
 *  and one the engine refuses to reap -- streaming right now, an unreadable protection --
 *  comes back `override_effective: false` and is dropped from BOTH the server's rollup and
 *  the planner's group expansion. Counting the show whole put "3 of 3 would be removed"
 *  above a chip reading "kept for now" and three dashed-red refused squares, and left it
 *  there after the refetch settled (rules 49/61).
 *
 *  Routed through `handFate`, so the number and the colors beside it cannot disagree, and it
 *  reads correctly in both states the card lives in. BEFORE the refetch the marks still carry
 *  their pre-decision override (`patchShowOverride` writes only `show_override`, by design),
 *  so nothing reads as spared or refused and the count moves the instant the operator clicks.
 *  AFTER it, the inherited reap and its refusals are on the marks and the count settles to
 *  what the server will really plan. */
export function showReapReaches(season: {
  verdict: Verdict;
  override: Override | null;
  override_effective: boolean | null;
}): boolean {
  const fate = handFate(season);
  return fate !== "spare" && fate !== "refused";
}

/** Whether a whole-show Reap would change nothing -- the show analogue of rule 48's
 *  already-condemned test. It decides `hideReap` for a show on both the card and the panel,
 *  so the test lives here once rather than being reimplemented at each surface.
 *
 *  A movie on the Condemned lane is atomically condemned, so its own Reap is a no-op and is
 *  hidden. A show is not atomic: it is on that lane because SOME season is condemned, and a
 *  whole-show Reap still takes the seasons the scan kept. So Reap only falls away once every
 *  season is already headed for removal -- scan-condemned and not hand-spared. A show holding
 *  any hand reap keeps Reap too, so that decision stays toggleable back off.
 *
 *  It must run over the WHOLE show, every lane -- the panel already passes `group.seasons`.
 *  The card once passed only the seasons on the current tab, so on the Condemned lane it saw
 *  a set that was all-condemned by construction and wrongly dropped Reap, hiding the one
 *  control that reaps the show's kept seasons. So the parameter takes the strip-mark shape
 *  too (`group_seasons`), not just a full `Candidate`. */
export function showReapIsNoop(
  seasons: ReadonlyArray<{ verdict: Verdict; override: Override | null }>,
): boolean {
  if (seasons.some((s) => s.override === "reap")) return false;
  return seasons.every((s) => s.verdict === "condemn" && s.override !== "spare");
}
