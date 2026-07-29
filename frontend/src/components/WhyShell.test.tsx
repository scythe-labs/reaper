// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The `.why` panel shell. Six panels render it, and what it owes them changes with the screen:
// on a desktop it is a side panel beside the list and both are usable, on a phone index.css
// gives it `inset: 0; z-index: 50` and it covers the entire application. The contract has to
// follow that, which is why every test here is written twice -- once per side of the boundary.
//
// jsdom has no `matchMedia`, so `useMediaQuery` reports false and an unstubbed test sees the
// wide screen. `stubMatchMedia(true)` is the phone.
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { WhyShell } from "./WhyShell";

/** Report `matches` for every query asked, the way a phone would for the narrow one. */
function stubMatchMedia(matches: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }));
}

// Rule 133: a stub left standing is inherited by the next test in the file.
afterEach(() => {
  vi.unstubAllGlobals();
});

const NAME = "Why this title scored 62";

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)}>Open the panel</button>
      <button onClick={() => setOpen(false)}>Close from outside</button>
      {open && (
        <WhyShell headingId="why-heading" onClose={() => setOpen(false)}>
          <header className="why-head">
            <h2 id="why-heading">{NAME}</h2>
          </header>
          <input aria-label="A box inside the panel" />
          <button>A control inside the panel</button>
        </WhyShell>
      )}
    </>
  );
}

describe("WhyShell on a desktop", () => {
  it("is a named side region, not a dialog, and does not take focus off the card", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open the panel" });
    await user.click(opener);

    // A side panel that claimed role="dialog" would be telling a screen reader the rest of the
    // page is unavailable, which on a wide screen is simply false: the queue is right beside it.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const panel = screen.getByRole("complementary", { name: NAME });
    expect(panel).not.toHaveAttribute("aria-modal");
    // The operator is still standing on the card they pressed.
    expect(opener).toHaveFocus();
  });

  it("leaves focus where it is when it closes with the operator somewhere else", async () => {
    // Handing focus back is a restore, not a yank. The panel never took focus here, so closing
    // it must not drag the operator out of whatever they had moved on to.
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open the panel" }));

    const outside = screen.getByRole("button", { name: "Close from outside" });
    await user.click(outside);

    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(outside).toHaveFocus();
  });
});

describe("WhyShell on a phone", () => {
  it("is a dialog named by its own heading, and moves focus into itself", async () => {
    stubMatchMedia(true);
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open the panel" }));

    const dialog = screen.getByRole("dialog", { name: NAME });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    // The reading starts at the panel's name rather than partway down its controls, and -- the
    // point of the whole fix -- it starts inside the thing now covering the screen.
    expect(dialog).toHaveFocus();
  });

  it("hands focus back to whatever opened it", async () => {
    stubMatchMedia(true);
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open the panel" });
    await user.click(opener);

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("keeps Tab inside itself, so the covered page never takes focus", async () => {
    stubMatchMedia(true);
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open the panel" }));

    const close = screen.getByRole("button", { name: "Close" });
    const last = screen.getByRole("button", { name: "A control inside the panel" });
    last.focus();
    await user.tab();

    // Wrapped back to the panel's first control instead of walking out into the card list
    // underneath, where every control is invisible and one of them is Reap.
    expect(close).toHaveFocus();
  });
});

describe("WhyShell Escape", () => {
  it("closes from inside one of the panel's own fields", async () => {
    // App's review-view handler owned Escape and bailed whenever the press came from an
    // INPUT/TEXTAREA/SELECT -- a bail `j`/`k` need and Escape does not -- so Escape from a box
    // inside the panel did nothing at all.
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open the panel" }));

    const box = screen.getByRole("textbox", { name: "A box inside the panel" });
    await user.click(box);
    expect(box).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  });
});
