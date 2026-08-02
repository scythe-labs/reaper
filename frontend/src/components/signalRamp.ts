// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The line a signal is measured against, said once.
//
// Every built-in signal is a ramp: nothing below one bound, all of its points at the other,
// a straight line between. Until now the app never said where either bound was -- the policy
// card offered "up to 10 points" with no hint of what earns them, and a panel row printed
// "0  IMDb 6.4" with nothing connecting the two numbers (#410).
//
// TWO surfaces state that fact now, and rule 144 is about exactly this pair: one sentence
// about what the app does, written in two places by two authors reading different halves of
// the code, drifts in the reassuring direction. So both read this module, and neither
// spells a bound itself.
//
// **Nothing here recomputes a score.** The panel row is handed the points it earned and the
// weight it could have earned, both from the engine, so it states arithmetic rather than
// performing it; the policy card describes the stored bounds and nothing else. A frontend
// copy of `signals._ramp` would be a second scoring implementation free to disagree with the
// first (rule 3/22's spirit), and the place it would disagree is a number printed beside the
// control an operator tunes deletions with.

// SWEPT (rule 72). Three things on the Policy page are ramps, and only one of them read
// backwards. The built-in signals here phrased their bound as where pressure STARTS ("pays
// nothing at or above 6.0"), with no word for which way it ran, so raising the number made
// the rule harsher while sounding more generous. The other two already name their direction
// and are correct as they stand: a graded rule of the operator's own reads "Size on disk: the
// higher it is (from 20 GB to 80 GB)" and a graded keep reads "the more, the safer (full
// effect at 80 GB)" (`PolicyRuleEditors.describeCondemn`, and the keeps list beside it).
//
// They do NOT get the strip, and that is a deferral rather than an oversight. Both are
// one-line summary rows in a list with a Remove button, not a card an operator tunes, and
// their bounds come from the field vocabulary rather than from the table below -- so a strip
// there is a different design question about a different surface, not this one applied twice.
// The keep bars are not siblings at all: "keep well-rated titles: at least 7.5" is a
// threshold, and it is where the phrasing below was borrowed from.

import { humanDays } from "../format";

/** How a signal's ramp is stored, which decides how many ends the operator can set.
 *
 *  `direct` ramps the value itself, so `floor` and `saturate_at` are independent and both
 *  are real controls.
 *
 *  `shortfall` measures how far BELOW `saturate_at` the value sits and then ramps THAT
 *  between the same two bounds, which works out to depend on `saturate_at - floor` alone.
 *  The two stored numbers therefore carry one degree of freedom between them, and full
 *  points always land at zero whatever the pair. Measured against `evaluate_signal`:
 *  `(0, 60)`, `(10, 70)` and `(40, 100)` produce identical curves across the whole rating
 *  range. So a shortfall signal gets ONE box; a second would provably do nothing. */
export type RampShape = "direct" | "shortfall";

interface RampUnits {
  shape: RampShape;
  /** A stored bound in the units the operator reads: `60` becomes `IMDb 6.0`. */
  say: (stored: number) => string;
  /** The suffix its box wears, and the step that box moves in (rule 40). */
  unit: string;
  step: number;
  /** Whether this bound's unit is one the operator can change, and which family it is from.
   *
   *  Rule 40 splits the two number controls on exactly this line: a CHANGEABLE unit is
   *  `QuantityInput` with a picker, a FIXED one is `FixedQuantity` with a suffix. Days are
   *  changeable and the dormancy gate two controls up already offers days/weeks/months/years
   *  for the same quantity, so a bound spelled "1825 days" beside a gate spelling the same
   *  span "5 years" was the app disagreeing with itself about one unit.
   *
   *  A rating, a head count and a season's place have no larger unit to offer, so they stay
   *  fixed. Naming the FAMILY rather than importing the unit list keeps this module free of
   *  the component it feeds. */
  unitKind: "time" | "size" | "fixed";
  /** How this signal names the thing being previewed: "A title rated", "A title untouched
   *  for". Worded per signal because "a title rated 2 people" is not a sentence, and one
   *  generic lead across five different facts is how a page ends up reading like a form. */
  lead: string;
  /** The near bound's label, phrased as the bar a title CLEARS rather than as where
   *  pressure starts.
   *
   *  "Pays nothing at or above 6.0" made the control read backwards: raise the number and
   *  the rule gets harsher, which is the opposite of what a higher rating usually means. It
   *  is not a rating being judged, it is the bar to be left alone over, and a bar raised is
   *  a bar fewer titles clear. Reaper's keep rules already say it this way ("keep well-rated
   *  titles: at least 7.5") and nobody misreads those. */
  nearLabel: string;
  /** The far bound's label, on the two-ended signals only. */
  farLabel?: string;
  /** The ends of the strip's scale, in the operator's words. `say(0)` cannot supply these:
   *  it words a BOUND ("less than a day"), where this words an axis ("today"). */
  scaleFrom: string;
  scaleTo: string;
  /** The number an operator types is not always the number that is stored. */
  toStored: (typed: number) => number;
  fromStored: (stored: number) => number;
  /** The widest value this box can ever show, so it can be sized to hold exactly that.
   *
   *  A fixed box is wrong in both directions here: 3.6rem clipped "1825", and 5rem left
   *  "6.0" floating in a box sized for four characters. Width is the one thing rule 40 lets
   *  vary, so it varies with what the FIELD can hold rather than with a global guess. */
  widest: string;
  /** The far end of the probe's track, in stored units. Wide enough that the whole ramp
   *  sits inside it with room past the point where it pays in full, so the flat top is
   *  visible rather than implied. */
  probeMax: number;
  /** Whether the watch mirror caps what this signal can ever read.
   *
   *  Only dormancy is capped: a never-played title is measured from the LATER of its
   *  arrival and the mirror's edge, because Reaper will not claim a file sat untouched for
   *  five years when it can see one. So the largest value this signal can present IS the
   *  reach, and a far end past it can never be earned. The others read facts the mirror
   *  does not bound, and the watcher count's own shortfall has its own policy warning. */
  boundedByHistory?: boolean;
  /** The lowest value this field can actually take, where that is not zero.
   *
   *  A ramp whose floor sits BELOW it never pays nothing for any real item, and "pays
   *  nothing until X" would then be false in the reassuring direction. `season_rank` ships
   *  floor 0 against a rank that starts at 1, so the newest season on disk already earns a
   *  sixth of the weight while the help text says older seasons carry more pressure than
   *  the newest -- which reads as though the newest carries none. */
  first?: number;
}

const WHOLE = { step: 1, toStored: Math.round, fromStored: (v: number) => v };

/** Keyed by `SignalId`. A key absent from here is a rule of the operator's own, which
 *  states its own range in the rule editor and is not described by this module. */
const RAMPS: Record<string, RampUnits> = {
  unwatched: {
    unitKind: "time",
    nearLabel: "Left alone until",
    farLabel: "Full points at",
    scaleFrom: "today",
    scaleTo: "10 years",
    widest: "3650",
    lead: "A title untouched for",
    shape: "direct",
    say: humanDays,
    unit: "days",
    probeMax: 3650,
    boundedByHistory: true,
    ...WHOLE,
  },
  season_rank: {
    unitKind: "fixed",
    nearLabel: "Left alone until",
    farLabel: "Full points at",
    scaleFrom: "newest",
    scaleTo: "20th-newest",
    widest: "20",
    lead: "A season that is",
    shape: "direct",
    say: (n) => (n === 1 ? "the newest season" : `the ${ordinal(n)}-newest season`),
    unit: "seasons",
    probeMax: 20,
    first: 1,
    ...WHOLE,
  },
  few_watchers: {
    unitKind: "fixed",
    nearLabel: "Enough watchers to leave it alone",
    scaleFrom: "nobody",
    scaleTo: "25 watchers",
    widest: "25",
    lead: "A title watched by",
    shape: "shortfall",
    say: (n) => (n === 1 ? "1 watcher" : `${n} watchers`),
    unit: "people",
    probeMax: 25,
    ...WHOLE,
  },
  low_rating: {
    unitKind: "fixed",
    nearLabel: "Good enough to leave alone",
    scaleFrom: "IMDb 0.0",
    scaleTo: "10.0",
    widest: "10.0",
    lead: "A title rated",
    shape: "shortfall",
    say: (n) => `IMDb ${(n / 10).toFixed(1)}`,
    unit: "IMDb",
    probeMax: 100,
    step: 0.1,
    // Stored in tenths because the policy body is integers-only: floats do not canonicalise,
    // and an unstable hash would void approvals at random (the same reason coverage is bp).
    toStored: (typed) => Math.round(typed * 10),
    fromStored: (stored) => stored / 10,
  },
  size: {
    unitKind: "size",
    nearLabel: "Left alone until",
    farLabel: "Full points at",
    scaleFrom: "empty",
    scaleTo: "200 GB",
    widest: "200",
    lead: "A title taking",
    shape: "direct",
    say: (n) => `${Math.round(n / 1e9)} GB`,
    unit: "GB",
    probeMax: 200e9,
    step: 1,
    toStored: (typed) => Math.round(typed * 1e9),
    fromStored: (stored) => Math.round(stored / 1e9),
  },
};

function ordinal(n: number): string {
  if (n === 2) return "second";
  if (n === 3) return "third";
  return `${n}th`;
}

/** What the operator can set on this signal, or `null` where this module has nothing to
 *  say -- a rule of their own, or a signal id it does not know. */
export function rampUnits(id: string): RampUnits | null {
  return RAMPS[id] ?? null;
}

/** The two ends of a ramp, in the operator's units.
 *
 *  `null` where there is no line to state, and the two causes are deliberately not told
 *  apart: a rule with no ramp, and a row frozen before the scan recorded one. Both mean the
 *  surface says nothing, which is the only honest answer for either. */
export function rampEnds(
  id: string,
  floor: number | null | undefined,
  saturate: number | null | undefined,
): { earnsFrom: string; earnsAll: string; shape: RampShape } | null {
  const units = RAMPS[id];
  if (!units || floor == null || saturate == null) return null;
  if (units.shape === "shortfall") {
    // The line is the gap, and full points land at zero. Both facts come off the arithmetic
    // in `RampShape`, never off `floor` alone, which on its own says nothing usable here.
    return {
      earnsFrom: units.say(saturate - floor),
      earnsAll: units.say(0),
      shape: "shortfall",
    };
  }
  return { earnsFrom: units.say(floor), earnsAll: units.say(saturate), shape: "direct" };
}

/** The whole ramp as one sentence, for the policy card that sets it.
 *
 *  Reads as a pair either way round, because the shortfall signals run downward: "pays
 *  nothing at IMDb 6.0 or above, and all 10 points at IMDb 0.0". */
export function rampSentence(
  id: string,
  floor: number | null | undefined,
  saturate: number | null | undefined,
  weight: number,
): string | null {
  const ends = rampEnds(id, floor, saturate);
  if (!ends) return null;
  const units = RAMPS[id];
  const points = `all ${weight} points`;
  if (ends.shape === "shortfall") {
    return `Pays nothing at ${ends.earnsFrom} or above, and ${points} at ${ends.earnsAll}.`;
  }
  // A floor below the first value the field can take means nothing real ever lands under
  // it, so "pays nothing until" would be false about every item there is. Say what happens
  // instead of what does not.
  if (units?.first !== undefined && (floor ?? 0) < units.first) {
    return `Pays something from ${units.say(units.first)} on, and ${points} at ${ends.earnsAll}.`;
  }
  return `Pays nothing until ${ends.earnsFrom}, and ${points} at ${ends.earnsAll}.`;
}

/** Reaper's own bounds for a signal, worded so the way back says where it goes.
 *
 *  "Put back Reaper's 1 year to 5 years" rather than a bare "Reset": the number the operator
 *  is undoing to is the whole of what they need to decide whether to press it, and a button
 *  that hides its own outcome is one they have to press to find out. */
export function rampDefaultSaid(
  id: string,
  shipped: { floor: number; saturate_at: number },
): string {
  const ends = rampEnds(id, shipped.floor, shipped.saturate_at);
  if (!ends) return "default";
  return ends.shape === "shortfall" ? ends.earnsFrom : `${ends.earnsFrom} to ${ends.earnsAll}`;
}

/** The strip under a signal's bounds: where it charges, and how hard, drawn to scale.
 *
 *  It REPLACES the sentence that used to state the range, rather than joining it. "Pays
 *  nothing at IMDb 6.0 or above, and all 10 points at IMDb 0.0" is exactly what this draws,
 *  and keeping both said one thing twice in two grammars.
 *
 *  It is what makes the direction visible. Dormancy charges more the further RIGHT a title
 *  sits and a rating charges more the further LEFT, which no shared sentence can carry
 *  without the operator holding two rules in their head; two strips leaning opposite ways
 *  need no holding at all.
 *
 *  Percentages of the track, so the caller only has to place them. `deepAt` is where the
 *  gradient reaches full strength: the point the signal pays its whole weight, which for a
 *  direct ramp is inside the fill and for a shortfall is at the fill's outer edge. */
export interface RampStrip {
  fillFrom: number;
  fillTo: number;
  /** Where the bar sits: the bound a title has to clear to be left alone. */
  bar: number;
  /** Which end of the fill is at full strength. */
  deepEnd: "left" | "right";
  scaleFrom: string;
  scaleTo: string;
}

export function rampStrip(
  id: string,
  floor: number | null | undefined,
  saturate: number | null | undefined,
): RampStrip | null {
  const units = RAMPS[id];
  if (!units || floor == null || saturate == null) return null;
  // Rounded, because these land in inline styles: `55 / 100 * 100` is 55.00000000000001 in
  // binary floating point, and a browser reads that as 55% while every reader of the DOM
  // sees the tail. Two places is finer than a pixel on any track this wide.
  const pct = (v: number) =>
    Math.round(Math.max(0, Math.min(100, (v / units.probeMax) * 100)) * 100) / 100;

  if (units.shape === "shortfall") {
    // The bar is the gap, and it charges everything BELOW it, hardest at zero.
    const bar = pct(saturate - floor);
    return {
      fillFrom: 0,
      fillTo: bar,
      bar,
      deepEnd: "left",
      scaleFrom: units.scaleFrom,
      scaleTo: units.scaleTo,
    };
  }
  // Charges everything past the near bound, reaching full at the far one and staying there,
  // which is why the fill runs to the end of the track rather than stopping at `saturate`.
  return {
    fillFrom: pct(floor),
    fillTo: 100,
    bar: pct(floor),
    deepEnd: "right",
    scaleFrom: units.scaleFrom,
    scaleTo: units.scaleTo,
  };
}

/** Points said the way both surfaces say them, so a probe and a row never disagree about
 *  how to spell the same number. Whole where it is whole, one place where it is not, and
 *  "less than 1" rather than a "0" beside a bar with color in it. */
export function sayPoints(points: number): string {
  if (points <= 0) return "0";
  if (points < 1) return "less than 1";
  return Number.isInteger(points) ? String(points) : points.toFixed(1);
}

/** What the engine answered, in the pieces the card bolds.
 *
 *  Returned in parts rather than as one string so the two numbers can be picked out: the
 *  value being tried and what it earns are what the sentence is FOR, and running them
 *  together with the words makes an operator re-read a line they should take at a glance.
 *
 *  The points come from the server (`POST /api/policy/probe`), never from arithmetic here:
 *  a local copy of the ramp beside the control that tunes deletions is a second scorer free
 *  to drift from the one that decides. This only words the answer. */
export interface ProbeSaid {
  /** The subject, worded by the signal: "A title rated", "A title untouched for". */
  lead: string;
  value: string;
  points: string;
  weight: number;
}

export function probeSaid(
  id: string,
  value: number,
  points: number,
  weight: number,
): ProbeSaid | null {
  const units = RAMPS[id];
  if (!units) return null;
  return {
    lead: units.lead,
    value: units.say(value),
    points: sayPoints(points),
    weight,
  };
}

/** The same sentence with what it did to one title, for the row in the explanation panel.
 *
 *  `added` and `weight` both come from the stored explanation. This states them; it does
 *  not check them, because the arithmetic that produced `added` ran in the engine against
 *  facts this side of the wire never sees. */
export function rowRampSentence(
  id: string,
  floor: number | null | undefined,
  saturate: number | null | undefined,
  weight: number,
  added: number,
): string | null {
  const ramp = rampSentence(id, floor, saturate, weight);
  if (!ramp) return null;
  return `${ramp} This one added ${sayPoints(Math.round(added * 10) / 10)}.`;
}
