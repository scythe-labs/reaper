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

describe("a spare whose clock has passed", () => {
  // Its own state, not a variant of either neighbor. The item is genuinely still kept -- the
  // planner, the ledger and the executor all read every spare on file, and only a scan realizes
  // the expiry -- so the button must not go dark as though policy had it back. But the decision
  // has run out, so it must not wear the solid fill that means "you chose this and it holds".
  it("says so, and wears the dashed fill rather than the solid one", () => {
    const { container } = draw({ override: "spare", spareExpiresAt: inDays(-3) });
    const btn = screen.getByRole("button", { name: "Spared, expired" });
    expect(btn.className).toContain("expired");
    // Still `active` and still aria-pressed: the item IS spared, and clicking still clears it.
    expect(btn.className).toContain("active");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    // Not ∞ either: it was a timed spare, and saying "forever" would be the other lie.
    expect(forever(container)).toBe(false);
  });

  it("abbreviates on the fixed track and spells it out in the footer", () => {
    const narrow = draw({ override: "spare", spareExpiresAt: inDays(-3) });
    expect(screen.getByRole("button", { name: "Spared, expired" }).textContent).toContain(
      "Expired",
    );
    narrow.unmount();
    // The footer has the room, so it takes the fuller label -- and needs no aria-label, because
    // the visible text already names the state.
    draw({ override: "spare", spareExpiresAt: inDays(-3), roomy: true });
    const wide = screen.getByRole("button", { name: "Spare expired" });
    expect(wide.getAttribute("aria-label")).toBeNull();
  });

  it("tells the operator the file is still kept, and never dates it in the past tense", () => {
    // The whole point of drawing this state: an expired spare that read as a plain "Spared"
    // left no way to know it had run out, and a "Kept until <a day last week>" tooltip was a
    // promise about a day already gone.
    draw({ override: "spare", spareExpiresAt: inDays(-3), roomy: true });
    const title = screen.getByRole("button", { name: "Spare expired" }).getAttribute("title") ?? "";
    expect(title).not.toMatch(/^Kept until/);
    expect(title).toMatch(
      /^Your spare expired on .+\. Still kept until the next scan judges it again$/,
    );
  });
});
