// SPDX-License-Identifier: AGPL-3.0-or-later
// The one thing a modal owes a phone: freeze the page behind it while it is up, so a touch
// drag scrolls the panel's own overflow instead of the page underneath (which iOS scrolls
// otherwise, leaving a tall modal impossible to reach the bottom of). These tests pin that the
// shell locks the body on open with position:fixed, the only thing iOS honors and not
// overflow:hidden, restores it (offset and all) on close, and only releases on the last of two
// stacked modals.
import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModalShell } from "./ModalShell";

afterEach(() => {
  // Leave the body pristine even if an assertion threw while it was locked.
  document.body.removeAttribute("style");
});

describe("ModalShell page-scroll lock", () => {
  it("freezes the body while open and restores it, scroll offset and all, on close", () => {
    const scrollTo = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    Object.defineProperty(window, "scrollY", { value: 240, configurable: true });

    const { unmount } = render(
      <ModalShell title="Edit" onClose={() => {}}>
        content
      </ModalShell>,
    );
    expect(document.body.style.position).toBe("fixed");
    expect(document.body.style.top).toBe("-240px");
    expect(document.body.style.width).toBe("100%");

    unmount();
    expect(document.body.style.position).toBe("");
    expect(document.body.style.top).toBe("");
    expect(scrollTo).toHaveBeenCalledWith(0, 240);
    scrollTo.mockRestore();
  });

  it("keeps the lock until the last of two stacked modals closes", () => {
    vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    const outer = render(
      <ModalShell title="A" onClose={() => {}}>
        a
      </ModalShell>,
    );
    const inner = render(
      <ModalShell title="B" onClose={() => {}}>
        b
      </ModalShell>,
    );
    expect(document.body.style.position).toBe("fixed");

    inner.unmount();
    expect(document.body.style.position).toBe("fixed"); // outer is still open

    outer.unmount();
    expect(document.body.style.position).toBe("");
  });
});
