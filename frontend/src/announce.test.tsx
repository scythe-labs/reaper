// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The region the app speaks its successes into (#175). What is pinned here is the three
// properties that make a polite region actually speak: it exists before the message does, the
// same sentence twice is two announcements, and nothing survives into the next mount.
import { render, screen } from "@testing-library/react";
import { act } from "react";
import { describe, expect, it } from "vitest";
import { announce, Announcer } from "./announce";

/** Both regions, in DOM order. Found by role rather than by class, which is the only way a
 *  test can tell that a reader would reach them at all. */
const regions = () => screen.getAllByRole("status");

/** What the operator would hear: whichever region is currently holding the sentence. */
const spoken = () =>
  regions()
    .map((r) => r.textContent)
    .filter((t) => t !== "")
    .join("|");

describe("the announcer", () => {
  it("is mounted and empty before anything has been said", () => {
    // The load-bearing property, and the reason this is a module rather than a component
    // rendered next to each message: several readers only watch live regions that were
    // already in the DOM, so a region inserted together with its text reads as correct and
    // stays silent. That is why `Notice` had to reach for `role="alert"` instead.
    render(<Announcer />);

    expect(regions()).toHaveLength(2);
    for (const region of regions()) {
      expect(region).toHaveAttribute("aria-live", "polite");
      expect(region).toHaveAttribute("aria-atomic", "true");
      expect(region).toHaveTextContent("");
    }
  });

  it("puts a message into one of the regions", () => {
    render(<Announcer />);

    act(() => announce("Policy saved."));

    expect(spoken()).toBe("Policy saved.");
  });

  it("says the same sentence twice when it happens twice", () => {
    // The defect this shape exists to avoid: saving twice says "Policy saved." twice, and a
    // text node that does not change is not announced -- so a single region would have made
    // the second save exactly as silent as the bug being fixed. The message moves to the
    // OTHER region, so whichever one receives it has changed and speaks.
    render(<Announcer />);

    act(() => announce("Policy saved."));
    const firstHolder = regions().findIndex((r) => r.textContent !== "");

    act(() => announce("Policy saved."));
    const secondHolder = regions().findIndex((r) => r.textContent !== "");

    expect(spoken()).toBe("Policy saved.");
    expect(secondHolder).not.toBe(firstHolder);
  });

  it("leaves only one region holding the message, so it is not read twice", () => {
    render(<Announcer />);

    act(() => announce("Rule added."));
    act(() => announce("Settings saved."));

    expect(regions().filter((r) => r.textContent !== "")).toHaveLength(1);
    expect(spoken()).toBe("Settings saved.");
  });

  it("says nothing for an empty message", () => {
    render(<Announcer />);

    act(() => announce(""));

    expect(spoken()).toBe("");
  });

  it("opens silent again after the last region unmounts", () => {
    // Nothing mounted can hear it, so nothing can be said. Without the reset the next mount
    // opens holding the last thing the previous one announced -- an operator signing back in
    // to "Policy saved.", and, in this suite, one test's sentence read as the next test's.
    const first = render(<Announcer />);
    act(() => announce("Password saved."));
    expect(spoken()).toBe("Password saved.");
    first.unmount();

    render(<Announcer />);

    expect(spoken()).toBe("");
  });
});
