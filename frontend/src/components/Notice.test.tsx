// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Pins the Notice component's contract in one place. Building this as a shared component,
// rather than a convention every call site has to follow, means the contract can be proven
// here once instead of checked at every call site by hand.
import { render, screen, within } from "@testing-library/react";
import { userEvent } from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Notice } from "./Notice";
import { SwitchConfirm, useSwitchConfirm } from "./SwitchConfirm";

describe("a notice", () => {
  // A screen reader must be told when a save fails, a password is refused, or a section
  // switch is blocked. This component is what makes that happen.
  it("is announced by default, in both tones", () => {
    render(
      <>
        <Notice tone="error">The scan didn&apos;t start.</Notice>
        <Notice tone="warn">Check this before saving.</Notice>
      </>,
    );
    const announced = screen.getAllByRole("alert");
    expect(announced).toHaveLength(2);
    // The lead word ("Problem:"/"Warning:") is part of what gets announced. Without it, a
    // screen reader has no way to tell "this blocks you" from "this is advice" apart from
    // color, which fails WCAG's use-of-color rule (1.4.1) as much as its status-message rule
    // (4.1.3).
    expect(announced.map((n) => n.textContent)).toEqual([
      "Problem: The scan didn't start.",
      "Warning: Check this before saving.",
    ]);
  });

  it("carries severity in words, not only in the color class", () => {
    // A separate test from the one above: if a later edit drops the lead word, both notices
    // read identically to a screen reader, even though the CSS class still differs.
    render(
      <>
        <Notice tone="error">Same sentence.</Notice>
        <Notice tone="warn">Same sentence.</Notice>
      </>,
    );
    const [problem, warning] = screen.getAllByRole("alert");

    expect(problem).toHaveTextContent(/^Problem:/);
    expect(warning).toHaveTextContent(/^Warning:/);
    expect(problem!.textContent).not.toBe(warning!.textContent);
  });

  // Must use role="alert", never role="status": these notices mount at the same moment they
  // have something to say, and a screen reader announces a newly-inserted polite region
  // unreliably. Checking the role by name stops a later edit from quietly changing it to
  // status.
  it("uses alert rather than a polite region, because it mounts with its text", () => {
    render(<Notice tone="error">Couldn&apos;t save that.</Notice>);
    const el = screen.getByRole("alert");
    expect(el).toHaveAttribute("role", "alert");
    expect(el).not.toHaveAttribute("aria-live");
  });

  // The `standing` prop is the only way to make a notice silent, and it must stay opt-in.
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
  // Driven through `useSwitchConfirm`, the same hook Settings and the policy editor both call,
  // so these tests exercise the real shipped logic rather than a fixture that reimplements it.
  // The harness below stands in for Settings: "general" is the open section, and "Security" is
  // the press being refused.
  function Harness({
    commit = vi.fn(),
    dirty = true,
  }: {
    commit?: (n: string) => void;
    dirty?: boolean;
  }) {
    const confirm = useSwitchConfirm<string>("general", dirty, commit);
    return (
      <>
        <button type="button" onClick={() => confirm.request("security")}>
          Security
        </button>
        <button type="button" onClick={() => confirm.request("general")}>
          General
        </button>
        {confirm.pending !== null && (
          <SwitchConfirm
            nonce={confirm.nonce}
            message="You have unsaved General settings. Switching to Security discards them."
            onDiscard={confirm.discard}
            onKeep={confirm.keep}
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
    // Both ways out sit one Tab away from the alert.
    expect(within(confirm).getByRole("button", { name: "Discard and switch" })).toBeInTheDocument();
    expect(within(confirm).getByRole("button", { name: "Keep editing" })).toBeInTheDocument();
  });

  // This component takes a nonce so pressing the same refused section twice still moves focus.
  // Setting `pendingSwitch` to the value it already holds is not a state change to React, so an
  // effect keyed on that value alone would not fire again on the second press.
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

  it("hands focus back to the control that was pressed when the refusal closes", async () => {
    // Moving focus into the alert must be paired with moving it back out. Without that,
    // `activeElement` becomes `<body>` and the next Tab starts at the masthead, forcing the
    // operator to walk through the whole section rail again to reach the field they were
    // editing.
    const user = userEvent.setup();
    render(<Harness />);

    const rail = screen.getByRole("button", { name: "Security" });
    await user.click(rail);
    expect(screen.getByRole("alert")).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Keep editing" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(rail).toHaveFocus();
  });

  it("moves only when Discard is pressed, and moves to the section that was refused", async () => {
    const user = userEvent.setup();
    const commit = vi.fn();
    render(<Harness commit={commit} />);

    await user.click(screen.getByRole("button", { name: "Security" }));
    expect(commit).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Discard and switch" }));
    expect(commit).toHaveBeenCalledExactlyOnceWith("security");
  });

  // Saving is not a decision to leave the section, so the notice closes and nothing switches.
  it("goes away when the edits do, without switching", async () => {
    const user = userEvent.setup();
    const commit = vi.fn();
    const { rerender } = render(<Harness commit={commit} dirty />);

    await user.click(screen.getByRole("button", { name: "Security" }));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    rerender(<Harness commit={commit} dirty={false} />);

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(commit).not.toHaveBeenCalled();
  });

  it("does not refuse a press of the section already open", async () => {
    const user = userEvent.setup();
    const commit = vi.fn();
    render(<Harness commit={commit} />);

    await user.click(screen.getByRole("button", { name: "General" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(commit).not.toHaveBeenCalled();
  });
});
