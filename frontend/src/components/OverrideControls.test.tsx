// SPDX-License-Identifier: AGPL-3.0-or-later
// The Spare button's own copy. One rule carries all of it: undecided, the button says what a
// press WILL do (the operator's default length); spared, it says what is in force on THIS item.
// The bug that motivated it: a 90-day spare under a Forever default left the button reading
// "∞ Spared" -- the wrong glyph, and no sign of when the spare ends.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { OverrideControls } from "./OverrideControls";
import { QueueSettingsContext, type QueueSettings } from "./queueSettings";

vi.mock("../api", () => ({ api: { general: vi.fn(), profile: vi.fn() } }));

/** An ISO instant `days` from now. Negative is a spare whose count has already run down. */
function inDays(days: number): string {
  return new Date(Date.now() + days * 86_400_000).toISOString();
}

function draw(
  props: Partial<Parameters<typeof OverrideControls>[0]> = {},
  defaultSpareDays = 0,
) {
  const shared: QueueSettings = {
    defaultSpareDays,
    unmeasured: { holdsBack: true, isPending: false, isError: false },
  };
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <QueueSettingsContext.Provider value={shared}>
        <OverrideControls
          override={null}
          onSet={vi.fn()}
          onClear={vi.fn()}
          pending={false}
          {...props}
        />
      </QueueSettingsContext.Provider>
    </QueryClientProvider>,
  );
}

/** The ∞ the forever spare wears. Both glyphs are aria-hidden, so they never reach a name. */
const forever = (c: HTMLElement) => c.querySelector(".infinity") !== null;

describe("the Spare button, undecided", () => {
  it("says what a press will do: ∞ and 'Spare' under a forever default", () => {
    const { container } = draw({}, 0);
    expect(screen.getByRole("button", { name: "Spare" })).toBeTruthy();
    expect(forever(container)).toBe(true);
  });

  it("wears the clock when the default is a set number of days", () => {
    const { container } = draw({}, 30);
    expect(screen.getByRole("button", { name: "Spare" })).toBeTruthy();
    expect(forever(container)).toBe(false);
  });
});

describe("the Spare button, spared", () => {
  it("keeps ∞ and the plain word for a forever spare", () => {
    const { container } = draw({ override: "spare", spareExpiresAt: null });
    expect(screen.getByRole("button", { name: "Spared" })).toBeTruthy();
    expect(forever(container)).toBe(true);
  });

  it("counts down in the panel footer: 'Spared 87d'", () => {
    draw({ override: "spare", spareExpiresAt: inDays(87), roomy: true });
    expect(screen.getByRole("button", { name: "Spared 87d" })).toBeTruthy();
  });

  it("shows the bare count on the fixed narrow tracks, named in full for a screen reader", () => {
    // The visible label is just "87d" (rule 51 leaves no room for the word), so the accessible
    // name carries the rest -- with the visible text still inside it (WCAG 2.5.3).
    const { container } = draw({ override: "spare", spareExpiresAt: inDays(87) });
    const btn = screen.getByRole("button", { name: "Spared 87d left" });
    expect(btn.textContent).toContain("87d");
    expect(btn.textContent).not.toContain("Spared");
    expect(forever(container)).toBe(false);
  });

  it("describes the ITEM's spare, not the default a press would have applied", () => {
    // The original bug: default Forever + a timed spare rendered "∞ Spared". The glyph must
    // follow the spare in force, so a timed spare wears the clock whatever the default is.
    const { container } = draw({ override: "spare", spareExpiresAt: inDays(87) }, 0);
    expect(forever(container)).toBe(false);
  });

  it("puts the end date in the tooltip, which the button has no room to show", () => {
    draw({ override: "spare", spareExpiresAt: inDays(87), roomy: true });
    const btn = screen.getByRole("button", { name: "Spared 87d" });
    expect(btn.getAttribute("title")).toMatch(/^Kept until .+\. Click to let Reaper judge it again$/);
  });
});

describe("a spare whose count has run down", () => {
  // It is still kept: the queue, planner and executor read every spare on file, and only a scan
  // realizes the expiry. So the button keeps saying Spared -- it just has nothing left to count,
  // and gets no word of its own for the gap. Deleting the empty-phrase branch in `spareRemaining`
  // would surface "expired" here, which is the state this pins shut.
  it("rests on the plain word, with no count and no word for the gap", () => {
    for (const roomy of [true, false]) {
      const { container, unmount } = draw({
        override: "spare",
        spareExpiresAt: inDays(-3),
        roomy,
      });
      const btn = screen.getByRole("button", { name: "Spared" });
      expect(btn.textContent).not.toMatch(/\d/);
      expect(btn.textContent?.toLowerCase()).not.toContain("expired");
      // Not ∞ either: it was a timed spare, and saying "forever" would be the other lie.
      expect(forever(container)).toBe(false);
      unmount();
    }
  });
});
