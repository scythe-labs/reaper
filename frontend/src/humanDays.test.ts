// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The browser's day wording against the server's.
//
// Both sit on the policy page at once: `clock.humanize_days` words the history warnings and
// `format.humanDays` words the controls beside them. They disagreed -- "1 year, 1 month" and
// "400 days" about the same number -- because this side only humanized exact multiples of a
// year or a month, and a real watch history is never a round number (#410).
//
// The table below is the OUTPUT of `uv run python -c "from reaper.clock import humanize_days"`
// for each input, transcribed. A test that asserted this side against itself would keep
// agreeing while the pair drifted, which is rule 144's failure exactly.
import { describe, expect, it } from "vitest";
import { humanDays } from "./format";

const AS_THE_SERVER_SAYS_IT: [number, string][] = [
  [30, "1 month"],
  [365, "1 year"],
  [400, "1 year, 1 month"],
  [730, "2 years"],
  [1095, "3 years"],
  [1825, "5 years"],
  [2060, "5 years, 7 months"],
  [3125, "8 years, 6 months"],
  [3650, "10 years"],
];

describe("a day count in words", () => {
  it.each(AS_THE_SERVER_SAYS_IT)("says %i the way the server says it", (days, said) => {
    expect(humanDays(days)).toBe(said);
  });

  it("words a plain handful of days", () => {
    expect(humanDays(5)).toBe("5 days");
    expect(humanDays(1)).toBe("1 day");
  });

  it("never renders a length as a date", () => {
    // Every caller drops this into a slot wanting a LENGTH ("untouched for ...", "goes back
    // ..."), where "today" reads as broken English. The server's wording, for the same reason.
    expect(humanDays(0)).toBe("less than a day");
    expect(humanDays(-3)).toBe("less than a day");
  });
});
