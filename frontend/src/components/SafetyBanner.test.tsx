// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The app's one always-visible safety surface. Its whole promise is that it always states
// whether deletion is on, so every branch is driven here, not just the one a change touched.
// A state with no test is one refactor away from silently becoming another state's copy, and
// the question this banner answers is "can this thing delete my library right now?"
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Safety } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { renderWithProviders } from "../test/renderWithProviders";
import { SafetyBanner } from "./SafetyBanner";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const READ_ONLY: Safety = {
  destructive_enabled: false,
  has_password: true,
  recovery_mode: false,
};

function renderBanner() {
  renderWithProviders(<SafetyBanner />);
}

describe("the safety banner", () => {
  beforeEach(() => {
    apiMock.safety.mockReset();
  });

  // The one phone-layout test stubs `matchMedia`; restore it so no later test inherits the
  // narrow layout (rule 133).
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("says read-only when deletion is off", async () => {
    apiMock.safety.mockResolvedValue(READ_ONLY);
    renderBanner();

    expect(await screen.findByText(/read-only/i)).toBeInTheDocument();
  });

  it("says deletion is on when it is armed", async () => {
    apiMock.safety.mockResolvedValue({ ...READ_ONLY, destructive_enabled: true });
    renderBanner();

    expect(await screen.findByText(/deletion is on/i)).toBeInTheDocument();
  });

  it("has no accessibility violations in the state that asks for something", async () => {
    // This is the recovery branch, not one of the resting states. It is the only state
    // carrying an instruction, so a lost name or a color-only signal here costs the operator
    // the action.
    apiMock.safety.mockResolvedValue({ ...READ_ONLY, recovery_mode: true });
    renderBanner();

    await screen.findByText(/recovery mode is on/i);
    await expectNoA11yViolations();
  });

  it("names recovery mode rather than plain read-only", async () => {
    // The server reports `destructive_enabled: false` in recovery mode too, so the read-only
    // branch would otherwise render here, and it points at Policy, Deletion, where the switch
    // refuses the request. The operator would follow the only instruction on screen and be
    // refused. Recovery mode needs its own distinct state so the sentence stays actionable.
    apiMock.safety.mockResolvedValue({ ...READ_ONLY, recovery_mode: true });
    renderBanner();

    expect(await screen.findByText(/recovery mode is on/i)).toBeInTheDocument();
    expect(screen.queryByText(/^read-only/i)).not.toBeInTheDocument();
    // It says what to do about it, and does not offer the control that cannot help.
    expect(screen.getByText(/turn it off and restart/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /deletion/i })).not.toBeInTheDocument();
  });

  it("says the state is unknown rather than disappearing when the read fails", async () => {
    // An absent banner reads as "nothing to worry about." That is the wrong direction to be
    // wrong in, on the one surface that must never make deletion look safer than it is.
    apiMock.safety.mockRejectedValue(new Error("no"));
    renderBanner();

    expect(await screen.findByText(/safety state unknown/i)).toBeInTheDocument();
  });

  it("collapses to one line on a phone and expands on tap", async () => {
    // jsdom ships no `matchMedia`, so `useMediaQuery` reads the wide layout by default. Report
    // the narrow one, which is where the notice collapses to a single tappable line. The state
    // lead still leads, so the regime reads while collapsed. `stubGlobal` restores in afterEach.
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockReturnValue({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    );
    apiMock.safety.mockResolvedValue({ ...READ_ONLY, destructive_enabled: true });
    const user = userEvent.setup();
    renderBanner();

    const toggle = await screen.findByRole("button", { name: /show the full safety notice/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // The regime is still on screen while collapsed.
    expect(screen.getByText(/deletion is on/i)).toBeInTheDocument();

    await user.click(toggle);
    expect(screen.getByRole("button", { name: /show less/i })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });
});
