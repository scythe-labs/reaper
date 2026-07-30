// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Which stale-read lines a panel draws when several of its reads can fail at once (#198).
//
// The rule is a pure function, so it is driven here directly rather than only through the two
// panels that use it: the panel tests can each reach a handful of states, and the states that
// matter are the boundaries between one line and several. The panels then prove the wiring --
// PlexPanel.test.tsx and SettingsStaleRead.test.tsx both drive a real multi-read failure.

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StaleReadSlot, collapseStaleReads } from "./StaleReadNotice";

const PANEL = "the Plex settings";
const FIRST = "these settings";
const SECOND = "the library list";
const THIRD = "the watch history record";

/** The reads a panel hands over, with the named ones in the failed-refetch state. */
function reads(...failing: string[]) {
  return [FIRST, SECOND, THIRD].map((what) => ({ what, stale: failing.includes(what) }));
}

describe("collapseStaleReads", () => {
  it("draws nothing while every read is healthy", () => {
    const plan = collapseStaleReads(PANEL, reads());
    expect(plan.at(FIRST)).toBeNull();
    expect(plan.at(SECOND)).toBeNull();
    expect(plan.at(THIRD)).toBeNull();
  });

  it("leaves one failure in its own slot, in its own words", () => {
    // The precise noun is more use to the operator than the panel's, and it belongs beside the
    // group it is about, so a lone failure is not made vaguer by the collapse.
    const plan = collapseStaleReads(PANEL, reads(SECOND));
    expect(plan.at(SECOND)).toBe(SECOND);
    expect(plan.at(FIRST)).toBeNull();
    expect(plan.at(THIRD)).toBeNull();
  });

  it("collapses two failures into one line naming the panel", () => {
    const plan = collapseStaleReads(PANEL, reads(SECOND, THIRD));
    expect(plan.at(FIRST)).toBe(PANEL);
    expect(plan.at(SECOND)).toBeNull();
    expect(plan.at(THIRD)).toBeNull();
  });

  it("puts the collapsed line in the FIRST read's slot, whichever reads failed", () => {
    // The slot is the one every caller places above the groups it covers, and the sentence says
    // what's BELOW may be out of date. Drawing it in a failing group's slot would leave it
    // speaking for the groups above it.
    for (const failing of [
      [SECOND, THIRD],
      [FIRST, THIRD],
      [FIRST, SECOND, THIRD],
    ]) {
      const plan = collapseStaleReads(PANEL, reads(...failing));
      expect(plan.at(FIRST), failing.join("+")).toBe(PANEL);
      expect(plan.at(SECOND), failing.join("+")).toBeNull();
      expect(plan.at(THIRD), failing.join("+")).toBeNull();
    }
  });

  it("says nothing about a slot it was never given", () => {
    // A caller that renames a slot on one side only gets silence rather than a stray line.
    const plan = collapseStaleReads(PANEL, reads(SECOND));
    expect(plan.at("a slot nobody declared")).toBeNull();
  });
});

describe("StaleReadSlot", () => {
  it("draws the noun the plan gives it", () => {
    render(<StaleReadSlot plan={collapseStaleReads(PANEL, reads(SECOND))} slot={SECOND} />);
    expect(screen.getByText(/Couldn't check the library list just now/)).toHaveClass("notice-warn");
  });

  it("draws the panel's noun in the first slot once several have failed", () => {
    render(<StaleReadSlot plan={collapseStaleReads(PANEL, reads(SECOND, THIRD))} slot={FIRST} />);
    expect(screen.getByText(/Couldn't check the Plex settings just now/)).toBeInTheDocument();
  });

  it("draws nothing for a slot the plan is silent about", () => {
    const { container } = render(
      <StaleReadSlot plan={collapseStaleReads(PANEL, reads(SECOND, THIRD))} slot={SECOND} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
