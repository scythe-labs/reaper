// SPDX-License-Identifier: AGPL-3.0-or-later
// The spared chip's countdown, and the date behind it. A chip has one line, so it says the
// clause and keeps the day in its tooltip -- which makes the tooltip the thing that can go
// stale: past the expiry the item is still kept (only a scan realizes a spare's clock), and
// "Kept until <a day last week>" reads as a promise about a day already gone.
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { OverrideChip } from "./StatusChip";

/** An ISO instant `days` from now. Negative is a spare whose count has already run down. */
function inDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString();
}

function chip(props: Partial<Parameters<typeof OverrideChip>[0]> = {}) {
  const { container } = render(<OverrideChip override="spare" {...props} />);
  return container.querySelector("span")!;
}

describe("the spared chip", () => {
  it("promises the keep outright when the spare is forever", () => {
    const el = chip({ spareCoversUntil: null });
    expect(el.textContent).toBe("Spared by hand · will be kept");
    // Nothing to date, so nothing in the tooltip.
    expect(el.getAttribute("title")).toBeNull();
  });

  it("counts down while time remains, with the day in the tooltip", () => {
    const el = chip({ spareCoversUntil: inDays(27) });
    expect(el.textContent).toBe("Spared by hand · 27 days left");
    expect(el.getAttribute("title")).toMatch(/^Kept until /);
  });

  it("says the spare expired, on the dashed fill, once the clock has passed", () => {
    // Still "Spared by hand": the item really is still kept, because only a scan realizes a
    // spare's clock. What changes is the clause and the fill -- dashed rather than solid,
    // since this is no longer a live decision.
    const el = chip({ spareCoversUntil: inDays(-3) });
    expect(el.textContent).toBe("Spared by hand · expired");
    expect(el.className).toContain("chip-spare-expired");
    expect(el.className).not.toContain("chip-hand-spare");
  });

  it("never dates an expired spare with a keep-until day already gone", () => {
    // The bug this pins: "Kept until Jul 22" shown on Jul 25 reads as a promise about a day
    // that has passed. The tooltip carries the whole sentence instead, so the date appears
    // only in a form that stays true.
    const el = chip({ spareCoversUntil: inDays(-3) });
    const title = el.getAttribute("title") ?? "";
    expect(title).not.toMatch(/^Kept until/);
    expect(title).toMatch(
      /^Your spare expired on .+\. Still kept until the next scan judges it again$/,
    );
  });

  it("qualifies a whole-show claim with its exceptions, over the countdown", () => {
    // A mixed show needs qualifying before it needs a clock: one season inside goes the other
    // way, so the unqualified "27 days left" would be the wrong thing to lead with.
    const el = chip({ spareCoversUntil: inDays(27), exceptions: 2 });
    expect(el.textContent).toBe("Spared by hand · except 2 seasons");
  });
});
