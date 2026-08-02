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
  /** The number an operator types is not always the number that is stored. */
  toStored: (typed: number) => number;
  fromStored: (stored: number) => number;
}

const WHOLE = { step: 1, toStored: Math.round, fromStored: (v: number) => v };

/** Keyed by `SignalId`. A key absent from here is a rule of the operator's own, which
 *  states its own range in the rule editor and is not described by this module. */
const RAMPS: Record<string, RampUnits> = {
  unwatched: { shape: "direct", say: humanDays, unit: "days", ...WHOLE },
  season_rank: {
    shape: "direct",
    say: (n) => (n === 1 ? "the newest season" : `the ${ordinal(n)}-newest season`),
    unit: "seasons",
    ...WHOLE,
  },
  few_watchers: {
    shape: "shortfall",
    say: (n) => (n === 1 ? "1 watcher" : `${n} watchers`),
    unit: "people",
    ...WHOLE,
  },
  low_rating: {
    shape: "shortfall",
    say: (n) => `IMDb ${(n / 10).toFixed(1)}`,
    unit: "IMDb",
    step: 0.1,
    // Stored in tenths because the policy body is integers-only: floats do not canonicalise,
    // and an unstable hash would void approvals at random (the same reason coverage is bp).
    toStored: (typed) => Math.round(typed * 10),
    fromStored: (stored) => stored / 10,
  },
  size: {
    shape: "direct",
    say: (n) => `${Math.round(n / 1e9)} GB`,
    unit: "GB",
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
  const points = `all ${weight} points`;
  return ends.shape === "shortfall"
    ? `Pays nothing at ${ends.earnsFrom} or above, and ${points} at ${ends.earnsAll}.`
    : `Pays nothing until ${ends.earnsFrom}, and ${points} at ${ends.earnsAll}.`;
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
  // "0" and "<1" the same way the row's own points column says them, so the sentence and
  // the number above it can never read as two different answers.
  const points = added <= 0 ? "0" : added < 1 ? "less than 1" : String(Math.round(added));
  return `${ramp} This one added ${points}.`;
}
