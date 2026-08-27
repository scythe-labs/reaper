// SPDX-License-Identifier: AGPL-3.0-or-later
// @vitest-environment node
//
// `format.humanDays` (the frontend) must say a day count the same way `clock.humanize_days`
// (the server) does, since both appear on the policy page for the same number.
//
// The table below is the real output of running `clock.humanize_days` in Python for each input,
// transcribed by hand. Comparing against that output, rather than a second implementation of
// the same logic, is what catches the two functions drifting apart.
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
    // Every caller uses this to describe a length of time ("untouched for ...", "goes back
    // ..."), where "today" would read as broken English. The server picks the same wording.
    expect(humanDays(0)).toBe("less than a day");
    expect(humanDays(-3)).toBe("less than a day");
  });
});
