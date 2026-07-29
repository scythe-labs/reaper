// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The notice contract, pinned once. Rule 18's point in doing this as a component rather than a
// convention is that the invariant is provable in one place instead of at 109 call sites, so
// this file is what makes the rest of the sweep hold.
import { render, screen, within } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Notice } from "./Notice";
import { SwitchConfirm } from "./SwitchConfirm";

describe("a notice", () => {
  // The defect this whole component exists for: 109 hand-rolled notices, zero live regions.
  // A screen reader was told nothing when a save failed, when a password was refused, or when
  // a section switch was blocked.
  it("is announced by default, in both tones", () => {
    render(
      <>
        <Notice tone="error">The scan didn&apos;t start.</Notice>
        <Notice tone="warn">Check this before saving.</Notice>
      </>,
    );
    const announced = screen.getAllByRole("alert");
    expect(announced).toHaveLength(2);
    expect(announced.map((n) => n.textContent)).toEqual([
      "The scan didn't start.",
      "Check this before saving.",
    ]);
  });

  // `role="alert"` and not `role="status"` is the load-bearing choice, and it is not style: these
  // notices are mounted at the moment they have something to say, and a polite region inserted
  // together with its text is unreliably announced. Asserting the role by name is what stops a
  // later edit "tidying" it to status and silently reverting the fix.
  it("uses alert rather than a polite region, because it mounts with its text", () => {
    render(<Notice tone="error">Couldn&apos;t save that.</Notice>);
    const el = screen.getByRole("alert");
    expect(el).toHaveAttribute("role", "alert");
    expect(el).not.toHaveAttribute("aria-live");
  });

  // The opt-out has to stay an opt-out: silent only where the author said so.
  it("is silent only when the call site declares it standing", () => {
    render(
      <Notice tone="warn" standing>
        Your rules add up to 104 points.
      </Notice>,
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/104 points/)).toBeInTheDocument();
  });

  it("keeps the tone and layout classes the stylesheet is written against", () => {
    render(
      <Notice tone="error" inline className="budget-notice" as="span">
        x
      </Notice>,
    );
    const el = screen.getByRole("alert");
    expect(el.tagName.toLowerCase()).toBe("span");
    expect(el.className.split(" ").sort()).toEqual(
      ["budget-notice", "notice", "notice-error", "notice-inline"].sort(),
    );
  });
});

describe("the confirm that refuses a switch away from unsaved edits", () => {
  function Harness({ onDiscard = vi.fn() }: { onDiscard?: () => void }) {
    const [nonce, setNonce] = useState(0);
    const [pending, setPending] = useState(false);
    return (
      <>
        <button
          type="button"
          onClick={() => {
            setPending(true);
            setNonce((n) => n + 1);
          }}
        >
          Security
        </button>
        {pending && (
          <SwitchConfirm
            nonce={nonce}
            message="You have unsaved General settings. Switching to Security discards them."
            onDiscard={onDiscard}
            onKeep={() => setPending(false)}
          />
        )}
      </>
    );
  }

  it("moves focus into the refusal, so the press does not read as a dead control", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Security" }));

    const confirm = screen.getByRole("alert");
    expect(confirm).toHaveFocus();
    // Both ways out are now one Tab away instead of past the whole section rail.
    expect(within(confirm).getByRole("button", { name: "Discard and switch" })).toBeInTheDocument();
    expect(within(confirm).getByRole("button", { name: "Keep editing" })).toBeInTheDocument();
  });

  // The nonce is the whole reason this component takes one. Pressing the same refused section a
  // second time sets `pendingSwitch` to the value it already holds, which React treats as no
  // change at all -- so an effect keyed on the pending value would not re-fire, and the second
  // press stayed the literal no-op the issue measured (byte-identical DOM, same activeElement).
  it("moves focus again on a repeat press, which changes no state at all", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Security" }));
    // Take focus back out to the rail, the way Tab or a second press would leave it.
    const rail = screen.getByRole("button", { name: "Security" });
    rail.focus();
    expect(rail).toHaveFocus();

    await user.click(rail);
    expect(screen.getByRole("alert")).toHaveFocus();
  });
});
