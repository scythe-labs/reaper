// SPDX-License-Identifier: AGPL-3.0-or-later
// The art-then-poster fallback, driven through `WhyHero`, one of its two consumers.
// `ReviewQueue`'s `Backdrop` is the other, and both reach the same behavior through the same
// `useArtFallback` hook, so a fix here covers both.
import { act, fireEvent, render, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { useArtFallback } from "./artFallback";
import { WhyHero } from "./WhyPanel";

/** The banner image, which is `aria-hidden` and so is unreachable by role. */
function hero(container: HTMLElement): HTMLImageElement | null {
  return container.querySelector(".why-hero img");
}

describe("the art-then-poster ladder", () => {
  it("asks for the wide art first", () => {
    const { container } = render(<WhyHero posterUrl="/api/poster/1.jpg" />);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/1.jpg?kind=art");
  });

  it("falls back to the poster when there is no separate art", () => {
    const { container } = render(<WhyHero posterUrl="/api/poster/1.jpg" />);
    fireEvent.error(hero(container)!);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/1.jpg");
  });

  it("climbs the same ladder when the server answers its one-pixel placeholder", () => {
    const { container } = render(<WhyHero posterUrl="/api/poster/1.jpg" />);
    const placeholder = () => {
      const img = hero(container)!;
      Object.defineProperty(img, "naturalWidth", { value: 1, configurable: true });
      fireEvent.load(img);
    };
    placeholder();
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/1.jpg");
    placeholder();
    expect(hero(container)).toBeNull();
  });

  it("drops the banner when the poster fails too", () => {
    const { container } = render(<WhyHero posterUrl="/api/poster/1.jpg" />);
    fireEvent.error(hero(container)!);
    fireEvent.error(hero(container)!);
    expect(hero(container)).toBeNull();
  });

  it("tries the art again for the next item rather than latching", () => {
    // The panel is reused rather than remounted when the next item's detail is already
    // cached. Without a reset, the fallback flag from a failed load on one item would stay
    // set and show that item's poster under every title that follows it.
    const { container, rerender } = render(<WhyHero posterUrl="/api/poster/1.jpg" />);
    fireEvent.error(hero(container)!);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/1.jpg");

    rerender(<WhyHero posterUrl="/api/poster/2.jpg" />);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/2.jpg?kind=art");

    // And the fallback is armed again for the new item.
    fireEvent.error(hero(container)!);
    expect(hero(container)?.getAttribute("src")).toBe("/api/poster/2.jpg");
  });

  it("says nothing to a screen reader, on every rung", async () => {
    // The banner is decoration. An empty `alt` and `aria-hidden` sit on both the art and the
    // poster step, so a reader hears the title once, not twice. This is audited at the
    // fallback step too, since that is the one an author editing `WhyHero`'s <img> and leaving
    // `Backdrop`'s alone would miss.
    const { container } = render(<WhyHero posterUrl="/api/poster/1.jpg" />);
    await expectNoA11yViolations(container);

    fireEvent.error(hero(container)!);
    await expectNoA11yViolations(container);
  });

  it("asks for nothing when there is no poster to ask for", () => {
    // No component can reach this branch directly. `Backdrop` takes a nullable url, and
    // `WhyHero` only mounts behind a `poster_url &&` guard. The hook is driven directly here
    // instead, to catch a null url that would otherwise silently render `src="null?kind=art"`
    // with nothing in the component tree noticing.
    const { result } = renderHook(() => useArtFallback(null));
    expect(result.current.src).toBeNull();

    act(() => result.current.onError());
    expect(result.current.src).toBeNull();
  });
});
