// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The card every setup step is drawn in, and what it does with focus.
//
// Moving between steps unmounts one card and mounts the next, so the button that was pressed
// goes with it and focus falls to `<body>`: a keyboard operator restarts at the top of the
// document and a screen reader is told nothing about where they now are. The wizard was the
// only multi-screen flow in the app with no focus handling at all, while a dozen other sites
// use the helpers in `focus.ts`.
//
// Both directions are driven, because the distinction is the whole design: focusing on a fresh
// load would steal focus from a page nobody has read yet, and not focusing after a press is the
// bug. The card cannot tell those apart on its own, so the wizard tells it.
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { StepCard, StepMovedProvider } from "./SetupStepper";

function renderCard(moved: boolean) {
  return render(
    <main className="setup">
      <StepMovedProvider moved={moved}>
        <StepCard step="connect" title="Connect your library">
          <button type="button">Continue</button>
        </StepCard>
      </StepMovedProvider>
    </main>,
  );
}

describe("the step card", () => {
  it("takes focus to its heading when the operator pressed their way here", () => {
    renderCard(true);

    const heading = screen.getByRole("heading", { level: 2, name: "Connect your library" });
    expect(document.activeElement).toBe(heading);
  });

  it("leaves focus alone when the wizard merely opened on this step", () => {
    renderCard(false);

    expect(document.activeElement).toBe(document.body);
  });

  it("says which step this is, and how many there are", () => {
    renderCard(false);

    expect(screen.getByText("Step 3 of 4")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Setup progress" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderCard(false);
    await expectNoA11yViolations(container);
  });
});
