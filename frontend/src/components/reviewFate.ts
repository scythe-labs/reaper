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

import { spareRemaining } from "../format";
import type { Override, Verdict } from "../api";

/** Which color a score badge or strip square wears once a hand decision is in play.
 *  A hand SPARE, or a reap the engine honors, shows SOLID -- "you chose this" -- so the
 *  cell states the item's real fate, not just Reaper's first read. A reap the engine
 *  can't honor yet ("refused") reads DASHED RED, never solid: the ask is noted, the file is
 *  still held. Amber is no longer used here at all -- it means only "left for you to decide"
 *  (the abstain `status-look` chip). Anything untouched keeps its scan verdict. Shared by the
 *  score badge and the season strip so the two can never disagree with each other or with
 *  the row's chip.
 *
 *  `"spare-expired"` is the fourth: a hand spare whose clock has passed. It is DASHED GREEN,
 *  and it is neither of the two states it sits between. Not solid green -- the operator's
 *  decision has run out, and solid means "you chose this and it holds". Not the scan verdict
 *  either -- the item is genuinely still kept, because only a scan realizes a spare's expiry
 *  (`whitelist.purge_expired_spares`), so until then the planner, the ledger and the executor
 *  all still read that spare. Painting it by verdict would tell the operator the item is back
 *  on the block when nothing will reap it. The dashed treatment is the one this app already
 *  uses for a decision whose effect is pending a scan, worn today by the held reap. */
export type Fate = Verdict | "reap" | "spare" | "spare-expired" | "refused";
export function handFate(item: {
  verdict: Verdict;
  override: Override | null;
  override_effective: boolean | null;
  /** When the LAST spare covering the item stops keeping it (ISO), null for a forever spare.
   *
   *  Deliberately NOT `spare_expires_at`, which is the spare in force by precedence -- an
   *  item's own key beating its show's. Precedence answers which row Reaper is reading; it
   *  does not answer when the file stops being kept, because when the winning spare expires
   *  the other one is still on file. A season spared 10 days inside a show spared forever is
   *  kept forever, and reading the own key alone painted it dashed "expired" over a file
   *  nothing would remove. The server derives this (`whitelist.covering_spare_expiry`), so
   *  the strip mark can answer it without being handed its show's decision.
   *
   *  Optional only for the yes/no callers below (`isCondemned`, `showReapReaches`), which
   *  answer the same for both spare states, so threading an expiry through them would be
   *  noise. Any caller that COLORS a cell must pass it: omitted, an expired spare reads as
   *  `"spare"` and paints the solid "you chose this" green, which is the one thing it is not.
   *  That is the pre-expiry behavior and the keep direction, so it fails safe rather than
   *  loud -- which is exactly why a new fate-bearing surface has to be checked against this
   *  line rather than trusted to error.
   *
   *  Being optional does NOT make it absent at runtime: a caller passes a whole mark or
   *  candidate, which carries the field whatever the parameter type names. A yes/no caller
   *  that treats the two spare states differently is therefore a bug the types cannot catch,
   *  and `showReapReaches` was one -- so every consumer of this function's result enumerates
   *  BOTH spare states, or says in its own doc why one answer covers them. */
  spare_covers_until?: string | null;
}): Fate {
  if (item.override === "spare") {
    return spareRemaining(item.spare_covers_until ?? null).expired ? "spare-expired" : "spare";
  }
  if (item.override === "reap") return item.override_effective === false ? "refused" : "reap";
  return item.verdict;
}

/** Which of the queue's three lanes an item is actually in -- the one answer to "where would I
 *  find this in the list".
 *
 *  `handFate` collapsed to the three stored verdicts, which is precisely what the server's tab
 *  filter does: `condemned.effective_verdict` calls itself the backend twin of `handFate`
 *  "collapsed to the three stored lanes", and the queue's lane query routes through it. So a
 *  hand spare -- and a hand reap the engine will not honor yet -- KEEP the file and read as
 *  Sanctuary however the scan first judged it; an honored hand reap reads as Condemned; an
 *  untouched item keeps its scan verdict. The two must stay collapsed the same way or a jump
 *  lands the operator on a lane the item is not in, which is the failure this answers.
 *
 *  It speaks for ONE item. A show is in every lane one of its seasons is in, so a caller opening
 *  a whole group names the lane it means rather than asking this. */
export function laneOf(item: {
  verdict: Verdict;
  override: Override | null;
  override_effective: boolean | null;
  /** Passed through to `handFate`. Both spare states land on the same lane -- an expired spare
   *  is still a spare until a scan purges it, and the server reads the whitelist row the same
   *  way -- so this changes no answer here; it is declared so a caller cannot quietly hand over
   *  a candidate whose expiry the type dropped on the floor. */
  spare_covers_until?: string | null;
}): Verdict {
  const fate = handFate(item);
  if (fate === "spare" || fate === "spare-expired" || fate === "refused") return "protect";
  if (fate === "reap") return "condemn";
  return fate;
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
  // spare and a held hand reap keep it, a honored hand reap condemns it. Asked as "is its lane the
  // condemned one", so the card's styling and the tab it can be found on are one fact and not two
  // that can drift -- this was the "which-tab proxy" in its own right, beside a laneOf that
  // answered the same question a second way. Reaches handFate through laneOf, so it still cannot
  // disagree with the colors (rule 49). NOT hideReap, which asks a different question (see
  // reapIsNoop).
  return laneOf(item) === "condemn";
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
 *  override chip's `effective` flag. Judges over the WHOLE show (the rollup's `seasons`),
 *  every lane, never the tab-filtered page. */
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
 *  EVERY spare state keeps its season, expired ones included: the server drops a season from
 *  the show's rollup on `override != "spare"` alone, and the planner reads the same live
 *  whitelist, where an expired row is still a spare until a scan purges it. So this asks
 *  "is it spared at all", not "is the spare live" -- reading only `"spare"` counted an
 *  expired one as removable and printed a number the planner would not act on, beside the
 *  dashed-green square that says the season is kept (rules 30/62).
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
  /** Passed through to `handFate`, which needs it to tell an expired spare from a live one.
   *  Both keep the season, so this changes no answer here -- it is declared so a caller
   *  cannot quietly hand over a mark whose expiry the type dropped on the floor. */
  spare_covers_until?: string | null;
}): boolean {
  const fate = handFate(season);
  return fate !== "spare" && fate !== "spare-expired" && fate !== "refused";
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
 *  too (the rollup's `seasons`), not just a full `Candidate`. */
export function showReapIsNoop(
  seasons: ReadonlyArray<{ verdict: Verdict; override: Override | null }>,
): boolean {
  if (seasons.some((s) => s.override === "reap")) return false;
  return seasons.every((s) => s.verdict === "condemn" && s.override !== "spare");
}
