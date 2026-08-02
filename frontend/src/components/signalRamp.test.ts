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
import { rampEnds, rampSentence, rampUnits, rowRampSentence } from "./signalRamp";

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
