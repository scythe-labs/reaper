// SPDX-License-Identifier: AGPL-3.0-or-later
// The art-then-poster ladder, driven through `WhyHero`, which is one of its two consumers.
// `ReviewQueue`'s `Backdrop` is the other and reaches the same three behaviors through the
// same `useArtFallback` call, which is the reason the hook exists: before it, this ladder was
// written twice and neither copy had a test.
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { WhyHero } from "./WhyPanel";

/** The banner image, which is `aria-hidden` and so is unreachable by role. */
function hero(container: HTMLElement): HTMLImageElement | null {
  return container.querySelector(".why-hero img");
}

describe("the art-then-poster ladder", () => {
  it("asks for the wide art first", () => {
    const { container } = render(<WhyHero posterUrl="/api/poster/1" />);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/1?kind=art");
  });

  it("falls back to the poster when there is no separate art", () => {
    const { container } = render(<WhyHero posterUrl="/api/poster/1" />);
    fireEvent.error(hero(container)!);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/1");
  });

  it("drops the banner when the poster fails too", () => {
    const { container } = render(<WhyHero posterUrl="/api/poster/1" />);
    fireEvent.error(hero(container)!);
    fireEvent.error(hero(container)!);
    expect(hero(container)).toBeNull();
  });

  it("tries the art again for the next item rather than latching", () => {
    // The panel is reused rather than remounted when the next item's detail is already
    // cached. Without the reset the fallback flag stays set and the poster of the previous
    // item is what one failed load leaves under every later title.
    const { container, rerender } = render(<WhyHero posterUrl="/api/poster/1" />);
    fireEvent.error(hero(container)!);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/1");

    rerender(<WhyHero posterUrl="/api/poster/2" />);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/2?kind=art");

    // And the fallback is armed again for the new item, not spent on the old one.
    fireEvent.error(hero(container)!);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/2");
  });

  it("says nothing to a screen reader, on every rung", async () => {
    // The banner is decoration: an empty `alt` and `aria-hidden` on both rungs, so a reader
    // hears the title once rather than twice. Audited at the fallback too, since that is the
    // rung an author editing one `<img>` and not the other would leave behind.
    const { container } = render(<WhyHero posterUrl="/api/poster/1" />);
    await expectNoA11yViolations(container);

    fireEvent.error(hero(container)!);
    await expectNoA11yViolations(container);
  });
});
