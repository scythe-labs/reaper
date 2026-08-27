// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The line a signal is measured against, said once.
//
// Every built-in signal is a ramp: nothing below one bound, all of its points at the other,
// a straight line between. Two surfaces state where those bounds are: the policy card
// ("up to 10 points") and a panel row ("0  IMDb 6.4"). Both read this module rather than
// spelling a bound themselves, so the same sentence about what the app does is never
// written twice by two authors reading different halves of the code.
//
// Nothing here recomputes a score. The panel row is handed the points it earned and the
// weight it could have earned, both from the engine, so it states arithmetic rather than
// performing it. The policy card describes the stored bounds and nothing else. A frontend
// copy of `signals._ramp` would be a second scoring implementation free to disagree with the
// first, and the place it would disagree is a number printed beside the control an operator
// tunes deletions with.

// Three things on the Policy page are ramps. The built-in signals here must never phrase
// their bound as where pressure starts ("pays nothing at or above 6.0"), with no word for
// which way it runs, because that makes raising the number sound more generous while the
// rule actually gets harsher. The other two name their direction and read correctly: a
// graded rule of the operator's own reads "Size on disk: the higher it is (from 20 GB to 80
// GB)" and a graded keep reads "the more, the safer (full effect at 80 GB)"
// (`PolicyRuleEditors.describeCondemn`, and the keeps list beside it).
//
// They do not get the strip, and that is a deliberate choice, not an oversight. Both are
// one-line summary rows in a list with a Remove button, not a card an operator tunes, and
// their bounds come from the field vocabulary rather than from the table below, so a strip
// there is a different design question about a different surface, not this one applied
// twice. The keep bars are not siblings at all: "keep well-rated titles: at least 7.5" is a
// threshold, and it is where the phrasing below was borrowed from.

import { humanDays } from "../format";
import i18next from "../i18n";

/** How a signal's ramp is stored, which decides how many ends the operator can set.
 *
 *  `direct` ramps the value itself, so `floor` and `saturate_at` are independent and both
 *  are real controls.
 *
 *  `shortfall` measures how far below `saturate_at` the value sits and then ramps that
 *  between the same two bounds, which works out to depend on `saturate_at - floor` alone.
 *  The two stored numbers therefore carry one degree of freedom between them, and full
 *  points always land at zero whatever the pair. Measured against `evaluate_signal`:
 *  `(0, 60)`, `(10, 70)` and `(40, 100)` produce identical curves across the whole rating
 *  range. So a shortfall signal gets one box. A second would provably do nothing. */
export type RampShape = "direct" | "shortfall";

interface RampUnits {
  shape: RampShape;
  /** A stored bound in the units the operator reads: `60` becomes `IMDb 6.0`. */
  say: (stored: number) => string;
  /** The suffix its box wears, and the step that box moves in. */
  unit: string;
  step: number;
  /** Whether this bound's unit is one the operator can change, and which family it is from.
   *
   *  This splits the two number controls on exactly this line: a changeable unit is
   *  `QuantityInput` with a picker, a fixed one is `FixedQuantity` with a suffix. Days are
   *  changeable, and the dormancy gate two controls up already offers days/weeks/months/years
   *  for the same quantity, so a bound spelled "1825 days" beside a gate spelling the same
   *  span "5 years" would be the app disagreeing with itself about one unit.
   *
   *  A rating, a head count and a season's place have no larger unit to offer, so they stay
   *  fixed. Naming the family rather than importing the unit list keeps this module free of
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
   *  it words a bound ("less than a day"), where this words an axis ("today"). */
  scaleFrom: string;
  scaleTo: string;
  /** The number an operator types is not always the number that is stored. */
  toStored: (typed: number) => number;
  fromStored: (stored: number) => number;
  /** The widest value this box can ever show, so it can be sized to hold exactly that.
   *
   *  A fixed box is wrong in both directions here: 3.6rem clipped "1825", and 5rem left
   *  "6.0" floating in a box sized for four characters. Width is the one thing allowed to
   *  vary here, and it varies with what the field can hold rather than with a global guess. */
  widest: string;
  /** The far end of the probe's track, in stored units. Wide enough that the whole ramp
   *  sits inside it with room past the point where it adds in full, so the flat top is
   *  visible rather than implied. */
  probeMax: number;
  /** Convert a bound-unit value into the unit the probe's fact is read in, where the two
   *  differ. Only size differs: its bounds are GB and `evaluate_signal` reads `size_bytes`.
   *  Absent means the two agree, which is every other signal. */
  probeValue?: (bound: number) => number;
  /** Whether the watch mirror caps what this signal can ever read.
   *
   *  Only dormancy is capped: a never-played title is measured from the later of its
   *  arrival and the mirror's edge, because Reaper will not claim a file sat untouched for
   *  five years when it can see one. So the largest value this signal can present is the
   *  reach, and a far end past it can never be earned. The others read facts the mirror
   *  does not bound, and the watcher count's own shortfall has its own policy warning. */
  boundedByHistory?: boolean;
  /** The lowest value this field can actually take, where that is not zero.
   *
   *  A ramp whose floor sits below it adds something to every real item, and "nothing until
   *  X" would then be false in the reassuring direction. `season_rank` ships
   *  floor 0 against a rank that starts at 1, so the newest season on disk already earns a
   *  sixth of the weight while the help text says older seasons carry more pressure than
   *  the newest, which reads as though the newest carries none. */
  first?: number;
}

const WHOLE = { step: 1, toStored: Math.round, fromStored: (v: number) => v };

/** `RampUnits`, except `nearLabel`/`farLabel`/`lead`/`scaleFrom`/`scaleTo` hold a catalog key
 *  rather than English text. `rampUnits()` below is the one place that resolves them, at
 *  call time, so a language change is picked up on the next render rather than frozen at
 *  whatever language was active when this module first loaded (the same reason `say` is a
 *  function and not a precomputed string). */
type RampSpec = Omit<RampUnits, "nearLabel" | "farLabel" | "lead" | "scaleFrom" | "scaleTo"> & {
  nearLabel: string;
  farLabel?: string;
  lead: string;
  scaleFrom: string;
  scaleTo: string;
};

/** Keyed by `SignalId`. A key absent from here is a rule of the operator's own, which
 *  states its own range in the rule editor and is not described by this module. */
const RAMP_SPECS: Record<string, RampSpec> = {
  unwatched: {
    unitKind: "time",
    nearLabel: "signals.ramp.unwatched.nearLabel",
    farLabel: "signals.ramp.unwatched.farLabel",
    scaleFrom: "signals.ramp.unwatched.scaleFrom",
    scaleTo: "signals.ramp.unwatched.scaleTo",
    widest: "3650",
    lead: "signals.ramp.unwatched.lead",
    shape: "direct",
    say: humanDays,
    unit: "days",
    probeMax: 3650,
    boundedByHistory: true,
    ...WHOLE,
  },
  season_rank: {
    unitKind: "fixed",
    nearLabel: "signals.ramp.season_rank.nearLabel",
    farLabel: "signals.ramp.season_rank.farLabel",
    scaleFrom: "signals.ramp.season_rank.scaleFrom",
    scaleTo: "signals.ramp.season_rank.scaleTo",
    widest: "20",
    lead: "signals.ramp.season_rank.lead",
    shape: "direct",
    // `saturate_at` takes no ceiling (`ge=1`, and the box passes only a `min`), so the
    // catalog message words every number an operator can save, not just the twenty the
    // scale draws: ICU selectordinal keys 11th and 13th apart from 21st and 23rd.
    say: (n) => i18next.t("signals.nthNewestSeason", { n }),
    unit: "seasons",
    probeMax: 20,
    first: 1,
    ...WHOLE,
  },
  few_watchers: {
    unitKind: "fixed",
    nearLabel: "signals.ramp.few_watchers.nearLabel",
    scaleFrom: "signals.ramp.few_watchers.scaleFrom",
    scaleTo: "signals.ramp.few_watchers.scaleTo",
    widest: "25",
    lead: "signals.ramp.few_watchers.lead",
    shape: "shortfall",
    say: (n) => i18next.t("signals.watcherCount", { n }),
    unit: "people",
    probeMax: 25,
    ...WHOLE,
  },
  low_rating: {
    unitKind: "fixed",
    nearLabel: "signals.ramp.low_rating.nearLabel",
    scaleFrom: "signals.ramp.low_rating.scaleFrom",
    scaleTo: "signals.ramp.low_rating.scaleTo",
    widest: "10.0",
    lead: "signals.ramp.low_rating.lead",
    shape: "shortfall",
    say: (n) => i18next.t("signals.ratingValue", { value: (n / 10).toFixed(1) }),
    unit: "IMDb",
    probeMax: 100,
    step: 0.1,
    // Stored in tenths because the policy body is integers-only: floats do not canonicalise,
    // and an unstable hash would void approvals at random. Evidence coverage is stored as
    // whole basis points for the same reason.
    toStored: (typed) => Math.round(typed * 10),
    fromStored: (stored) => stored / 10,
  },
  size: {
    // Stored and compared in gigabytes, not bytes. This is the one entry where the bound's
    // unit differs from the fact's: `_branch_signal` rescales `size_bytes` to GB before
    // ramping it, so `floor` and `saturate_at` here must stay in GB or the signal would pay
    // zero at every real file size while its weight stays in the score denominator.
    // `unitKind: "fixed"` follows because there is one unit now, so this puts it in a
    // suffix rather than a picker.
    unitKind: "fixed",
    nearLabel: "signals.ramp.size.nearLabel",
    farLabel: "signals.ramp.size.farLabel",
    scaleFrom: "signals.ramp.size.scaleFrom",
    scaleTo: "signals.ramp.size.scaleTo",
    widest: "200",
    lead: "signals.ramp.size.lead",
    shape: "direct",
    say: (n) => i18next.t("signals.sizeValueGb", { value: Math.round(n) }),
    unit: "GB",
    probeMax: 200,
    step: 1,
    toStored: (typed) => Math.round(typed),
    fromStored: (stored) => Math.round(stored),
    // The probe sends a title's fact, and the engine reads that one in bytes
    // (`preview.READS` maps `"size"` to `size_bytes`) before applying the same rescale. So
    // this is the one signal whose probe value is not in the unit its bounds are.
    probeValue: (gb) => gb * 1e9,
  },
};

/** What the operator can set on this signal, or `null` where this module has nothing to
 *  say: a rule of their own, or a signal id it does not know. Every label is resolved
 *  through the catalog here, at call time, so every reader of this function's result gets
 *  the current language whatever the language was when the module first loaded. */
export function rampUnits(id: string): RampUnits | null {
  const spec = RAMP_SPECS[id];
  if (!spec) return null;
  return {
    ...spec,
    nearLabel: i18next.t(spec.nearLabel),
    // `exactOptionalPropertyTypes` reads an explicit `farLabel: undefined` as a value, not an
    // absence, so the key is genuinely omitted rather than set to undefined.
    ...(spec.farLabel === undefined ? {} : { farLabel: i18next.t(spec.farLabel) }),
    lead: i18next.t(spec.lead),
    scaleFrom: i18next.t(spec.scaleFrom),
    scaleTo: i18next.t(spec.scaleTo),
  };
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
  const units = RAMP_SPECS[id];
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

/** The two ends of the scale, in the operator's words.
 *
 *  The second half of a row's line, and never rendered on its own: it says what each end of
 *  the scale is worth without naming what those points are points of, which is the job of
 *  the clause `rowRampSentence` puts in front of it.
 *
 *  Reads as a pair either way round, because the shortfall signals run downward: "nothing at
 *  IMDb 6.0 or above, all 10 at IMDb 0.0". */
export function rampScale(
  id: string,
  floor: number | null | undefined,
  saturate: number | null | undefined,
  weight: number,
): string | null {
  const ends = rampEnds(id, floor, saturate);
  if (!ends) return null;
  const units = RAMP_SPECS[id];
  if (ends.shape === "shortfall") {
    return i18next.t("signals.scaleAboveOrEqual", {
      from: ends.earnsFrom,
      weight,
      to: ends.earnsAll,
    });
  }
  // A floor below the first value the field can take means nothing real ever lands under
  // it, so "nothing until" would be false about every item there is. Say what happens
  // instead of what does not.
  if (units?.first !== undefined && (floor ?? 0) < units.first) {
    return i18next.t("signals.scaleEvenFirst", {
      first: units.say(units.first),
      weight,
      to: ends.earnsAll,
    });
  }
  return i18next.t("signals.scaleUntil", { from: ends.earnsFrom, weight, to: ends.earnsAll });
}

/** The strip under a signal's bounds: where it charges, and how hard, drawn to scale.
 *
 *  This replaces stating the range as a sentence, rather than joining it: "Nothing at IMDb
 *  6.0 or above, all 10 at IMDb 0.0" is exactly what this draws, and showing both would say
 *  one thing twice in two grammars.
 *
 *  It is what makes the direction visible. Dormancy charges more the further right a title
 *  sits, and a rating charges more the further left, which no shared sentence can carry
 *  without the operator holding two rules in their head. Two strips leaning opposite ways
 *  need no holding at all.
 *
 *  Percentages of the track, so the caller only has to place them. `deepAt` is where the
 *  gradient reaches full strength: the point the signal adds its whole weight, which for a
 *  direct ramp is inside the fill and for a shortfall is at the fill's outer edge. */
export interface RampStrip {
  fillFrom: number;
  fillTo: number;
  /** Where the bar sits: the bound a title has to clear to be left alone. */
  bar: number;
  /** Which end of the fill is at full strength. */
  deepEnd: "left" | "right";
  /** Where the gradient reaches full strength, as a percentage of the track. A direct ramp
   *  saturates inside its fill and is flat-full from there to the end, so this is the only
   *  thing that puts "adds in full" where the operator can see it. */
  deepAt: number;
  scaleFrom: string;
  scaleTo: string;
}

export function rampStrip(
  id: string,
  floor: number | null | undefined,
  saturate: number | null | undefined,
): RampStrip | null {
  const units = rampUnits(id);
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
      // A shortfall adds most at zero, which is the fill's outer edge, so the deep point
      // and the edge coincide and the gradient has no flat region to draw.
      deepAt: 0,
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
    deepAt: pct(saturate),
    scaleFrom: units.scaleFrom,
    scaleTo: units.scaleTo,
  };
}

/** The fill's `background`, with full strength placed at `deepAt` instead of at whichever
 *  edge the gradient happens to end on.
 *
 *  A direct ramp adds its whole weight from `saturate_at` onward, so the picture has to go
 *  flat-full there and stay flat to the end of the track. Running one gradient edge to edge
 *  would draw that flat top as a color still deepening, well past the point where the signal
 *  already adds its full weight. A CSS gradient holds its last stop's color to the end of
 *  the box, so placing that stop at `deepAt` is the whole fix. */
export function rampFill(strip: RampStrip): string {
  const faint = "color-mix(in srgb, var(--condemn) 6%, transparent)";
  const span = strip.fillTo - strip.fillFrom;
  // A zero-width fill has no inside to place a stop in. Either end reads the same.
  const within = span <= 0 ? 0 : ((strip.deepAt - strip.fillFrom) / span) * 100;
  const at = Math.round(Math.max(0, Math.min(100, within)) * 100) / 100;
  return strip.deepEnd === "left"
    ? `linear-gradient(to left, ${faint}, var(--condemn) ${100 - at}%)`
    : `linear-gradient(to right, ${faint}, var(--condemn) ${at}%)`;
}

/** Points said the way both surfaces say them, so a probe and a row never disagree about
 *  how to spell the same number. Whole where it is whole, one place where it is not, and
 *  "less than 1" rather than a "0" beside a bar with color in it. */
export function sayPoints(points: number): string {
  if (points <= 0) return "0";
  if (points < 1) return i18next.t("signals.pointsLessThanOne");
  return Number.isInteger(points) ? String(points) : points.toFixed(1);
}

/** What the engine answered, in the pieces the card bolds.
 *
 *  Returned in parts rather than as one string so the two numbers can be picked out: the
 *  value being tried and what it earns are what the sentence is for, and running them
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
  const units = rampUnits(id);
  if (!units) return null;
  return {
    lead: units.lead,
    value: units.say(value),
    points: sayPoints(points),
    weight,
  };
}

/** What this row did, then the scale it did it on, for the explanation panel.
 *
 *  The result leads, because it is the question the row was opened to ask. Trailing it
 *  behind two clauses of scale would make the reader hold "pays nothing at IMDb 6.0 or
 *  above" before reaching the one number the row is about, and the scale would open on the
 *  end where nothing happens, a negative to carry for no gain.
 *
 *  That lead is also the policy card's grammar, said the same way beside the control that
 *  sets these bounds ("A title rated IMDb 5.5 adds less than 1 of these 10 points",
 *  `probeSaid`). One rule described on two pages is two copies free to drift, and the drift
 *  is invisible because each page reads correct alone.
 *
 *  `added` and `weight` both come from the stored explanation. This states them. It does
 *  not check them, because the arithmetic that produced `added` ran in the engine against
 *  facts this side of the wire never sees. */
export function rowRampSentence(
  id: string,
  floor: number | null | undefined,
  saturate: number | null | undefined,
  weight: number,
  added: number,
): string | null {
  const scale = rampScale(id, floor, saturate, weight);
  if (!scale) return null;
  return i18next.t("signals.addedOfPoints", {
    added: sayPoints(Math.round(added * 10) / 10),
    weight,
    scale,
  });
}
