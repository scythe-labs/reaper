// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The line a signal is measured against, said once and read by two surfaces.
//
// The bounds below are the SHIPPED defaults out of `engine/policy.py`, and the shapes were
// established by running `engine.signals.evaluate_signal` rather than by reading it: the two
// shortfall signals score identically for any stored pair with the same gap, which is why
// they get one settable end and not two. A fixture invented here instead would prove the
// sentence renders and nothing about whether it is true.
import { describe, expect, it } from "vitest";
import {
  rampEnds,
  rampFill,
  rampSentence,
  rampStrip,
  rampUnits,
  rowRampSentence,
} from "./signalRamp";

describe("what a signal's ramp says", () => {
  it("reads downward for a signal measured on a shortfall", () => {
    // low_rating ships weight 10, saturate_at 60, floor 0. Measured: 6.0 pays nothing, 5.5
    // pays 0.83, 0.0 pays the lot.
    expect(rampSentence("low_rating", 0, 60, 10)).toBe(
      "Pays nothing at IMDb 6.0 or above, and all 10 points at IMDb 0.0.",
    );
  });

  it("reads upward for a signal measured on the value itself", () => {
    // unwatched ships weight 70, floor 365, saturate_at 1825. Measured: 365 pays nothing,
    // 1095 pays 35, 1825 pays 70.
    expect(rampSentence("unwatched", 365, 1825, 70)).toBe(
      "Pays nothing until 1 year, and all 70 points at 5 years.",
    );
  });

  it("counts people down, because fewer watchers is more pressure", () => {
    expect(rampSentence("few_watchers", 0, 3, 20)).toBe(
      "Pays nothing at 3 watchers or above, and all 20 points at 0 watchers.",
    );
  });

  it("refuses to claim a season pays nothing when the shipped floor sits under the first one", () => {
    // season_rank ships floor 0 against a rank that starts at 1, so measured against
    // `evaluate_signal` the NEWEST season on disk already earns 2.5 of its 15 points.
    // "Pays nothing until the newest season" would be false about every season there is,
    // and false in the reassuring direction, which is the one that has to be caught.
    expect(rampSentence("season_rank", 0, 6, 15)).toBe(
      "Pays something from the newest season on, and all 15 points at the 6th-newest season.",
    );
  });

  it("words a season rank past the twenty the scale draws", () => {
    // Nothing caps the far bound: `saturate_at` is `ge=1` with no ceiling and the box passes
    // only a `min`, so 21 saves and has to be worded. The last two digits decide the suffix,
    // because 11-13 take "th" where 21-23 do not.
    expect(rampSentence("season_rank", 2, 21, 15)).toContain("the 21st-newest season");
    expect(rampSentence("season_rank", 2, 22, 15)).toContain("the 22nd-newest season");
    expect(rampSentence("season_rank", 2, 23, 15)).toContain("the 23rd-newest season");
    expect(rampSentence("season_rank", 2, 13, 15)).toContain("the 13th-newest season");
    expect(rampSentence("season_rank", 2, 11, 15)).toContain("the 11th-newest season");
  });

  it("says 'pays nothing until' once the floor is inside the seasons that exist", () => {
    // Raise the floor to a rank a real season can hold and the ordinary sentence is true
    // again, so the branch above is about the shipped default and not about the signal.
    expect(rampSentence("season_rank", 2, 6, 15)).toBe(
      "Pays nothing until the second-newest season, and all 15 points at the 6th-newest season.",
    );
  });

  it("gives a shortfall signal the same line for every pair with the same gap", () => {
    // The engine scores these three identically -- the fraction works out to depend on
    // `saturate_at - floor` alone -- so the operator must be shown one number, not two.
    const said = rampSentence("low_rating", 0, 60, 10);
    expect(rampSentence("low_rating", 10, 70, 10)).toBe(said);
    expect(rampSentence("low_rating", 40, 100, 10)).toBe(said);
  });

  it("keeps the two ends of a direct signal apart", () => {
    // The same check in the other direction: here the pair does NOT collapse, and a UI that
    // offered one box would be dropping a control the engine honors.
    expect(rampSentence("unwatched", 365, 1825, 70)).not.toBe(
      rampSentence("unwatched", 0, 1460, 70),
    );
  });

  it("says nothing at all about a rule it does not know", () => {
    // A rule of the operator's own. It states its own range in the rule editor, and this
    // module inventing one would put a ramp on a yes/no rule that has none.
    expect(rampSentence("my own rule", 0, 60, 10)).toBeNull();
    expect(rampUnits("my own rule")).toBeNull();
    expect(rampEnds("my own rule", 0, 60)).toBeNull();
  });

  it("says nothing about a row the scan never recorded a line for", () => {
    // Null arrives from two places -- a boolean rule, and a row frozen before the fields
    // shipped -- and neither may be guessed into a line.
    expect(rampSentence("low_rating", null, null, 10)).toBeNull();
    expect(rampSentence("low_rating", 0, null, 10)).toBeNull();
    expect(rampSentence("low_rating", null, 60, 10)).toBeNull();
    expect(rampSentence("low_rating", undefined, undefined, 10)).toBeNull();
  });
});

describe("what a panel row adds to that", () => {
  it("states the points the engine recorded, never its own arithmetic", () => {
    expect(rowRampSentence("low_rating", 0, 60, 10, 0)).toBe(
      "Pays nothing at IMDb 6.0 or above, and all 10 points at IMDb 0.0. This one added 0.",
    );
  });

  it("says 'less than 1' rather than rounding a real contribution away to zero", () => {
    // 0.83 is what an IMDb 5.5 earns against a 10-point rule. Printing "0" beside a bar
    // with red in it would read as a broken bar.
    expect(rowRampSentence("low_rating", 0, 60, 10, 0.83)).toContain("This one added less than 1.");
  });

  it("rounds a whole contribution to the number the row's own column shows", () => {
    expect(rowRampSentence("unwatched", 365, 1825, 70, 35)).toContain("This one added 35.");
  });

  it("stays silent wherever the ramp is", () => {
    expect(rowRampSentence("low_rating", null, null, 10, 0)).toBeNull();
  });
});

// The strip is what makes the DIRECTION visible, and the direction is the thing the words
// could never carry: a rating charges the further LEFT a title sits, dormancy the further
// RIGHT. Reading them off two pictures leaning opposite ways needs no rule held in the head.
describe("the strip drawn under a signal's bounds", () => {
  it("charges everything below the bar on a shortfall signal", () => {
    // low_rating ships weight 10, floor 0, saturate 80 on a 0-10 scale.
    const strip = rampStrip("low_rating", 0, 80)!;

    expect(strip.fillFrom).toBe(0);
    expect(strip.fillTo).toBe(80);
    expect(strip.bar).toBe(80);
    // Hardest at 0.0, which is where it pays in full.
    expect(strip.deepEnd).toBe("left");
  });

  it("charges everything past the bar on a direct signal, and keeps charging", () => {
    // unwatched ships floor 365, saturate 1825, on a 3650-day track. The fill runs to the
    // END rather than stopping at 1825: past the far bound it still pays in full, and a fill
    // that stopped there would draw the oldest titles as though they were left alone.
    const strip = rampStrip("unwatched", 365, 1825)!;

    expect(strip.fillFrom).toBe(10);
    expect(strip.fillTo).toBe(100);
    expect(strip.bar).toBe(10);
    expect(strip.deepEnd).toBe("right");
  });

  it("leans the two shapes opposite ways", () => {
    // The whole point, stated as a test: one picture cannot be mistaken for the other.
    expect(rampStrip("low_rating", 0, 80)!.deepEnd).not.toBe(
      rampStrip("unwatched", 365, 1825)!.deepEnd,
    );
  });

  it("moves the bar with the bound, on the gap a shortfall signal actually stores", () => {
    // (0,55) and (10,65) are the same curve, so they must draw the same picture.
    expect(rampStrip("low_rating", 0, 55)!.bar).toBe(55);
    expect(rampStrip("low_rating", 10, 65)!.bar).toBe(55);
  });

  it("rounds what it hands to a style attribute", () => {
    // 55/100*100 is 55.00000000000001 in binary floating point, and that tail reaches the DOM.
    expect(rampStrip("low_rating", 0, 55)!.bar).toBe(55);
  });

  it("draws nothing where there is no ramp to draw", () => {
    expect(rampStrip("my own rule", 0, 80)).toBeNull();
    expect(rampStrip("low_rating", null, null)).toBeNull();
  });
});

// A direct ramp pays its whole weight from the far bound ONWARD, so the picture has a flat
// top. Running one gradient edge to edge drew that flat top as a colour still deepening, and
// the section key underneath asserts the opposite in words ("deepest where it pays in full").
describe("where the fill reaches full strength", () => {
  it("saturates at the far bound on a direct signal, not at the end of the track", () => {
    // unwatched ships floor 365, saturate 1825, track 3650. Full points land at 1825, which
    // is half way along the track, NOT at 3650 where the fill happens to stop.
    expect(rampStrip("unwatched", 365, 1825)!.deepAt).toBe(50);
  });

  it("saturates at the fill's own edge on a shortfall signal", () => {
    // A shortfall pays most at zero, which is where the fill already starts, so deep point
    // and edge coincide and there is no flat region to place.
    expect(rampStrip("low_rating", 0, 80)!.deepAt).toBe(0);
  });

  it("places the gradient stop inside the fill, so the flat top is drawn flat", () => {
    // The fill box spans 10% -> 100% of the track and saturates at 50%, which is (50-10)/90
    // = 44.44% of the way into the box. Everything past that stop holds full colour, which
    // is what "pays in full and keeps paying" looks like.
    expect(rampFill(rampStrip("unwatched", 365, 1825)!)).toBe(
      "linear-gradient(to right, color-mix(in srgb, var(--condemn) 6%, transparent), var(--condemn) 44.44%)",
    );
  });

  it("keeps a shortfall's deep end on the left", () => {
    // Measured the other way round: the stop sits at 100% of a `to left` gradient, which is
    // the box's LEFT edge, where a rating of 0.0 pays all 10 points.
    expect(rampFill(rampStrip("low_rating", 0, 80)!)).toBe(
      "linear-gradient(to left, color-mix(in srgb, var(--condemn) 6%, transparent), var(--condemn) 100%)",
    );
  });

  it("puts full colour at the far bound wherever the operator moves it", () => {
    // The bound is what decides the stop, so a shorter ramp saturates sooner. 365 -> 730 on
    // the same 3650 track is 20% of the track and (20-10)/90 = 11.11% into the fill.
    expect(rampStrip("unwatched", 365, 730)!.deepAt).toBe(20);
    expect(rampFill(rampStrip("unwatched", 365, 730)!)).toContain("var(--condemn) 11.11%");
  });
});
