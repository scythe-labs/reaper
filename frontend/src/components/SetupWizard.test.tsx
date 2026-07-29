// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one behavior that matters on the first-run screen: once the first scan is running you
// can leave for the app immediately, instead of being held on the wizard until it finishes.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { expectNoA11yViolations } from "../test/a11y";
import { testQueryClient } from "../test/queryClient";
import { SetupWizard } from "./SetupWizard";

// The wizard embeds the services and Plex panels; they make their own queries and are not
// what these tests are about, so stub them out.
vi.mock("./Settings", () => ({ PlexPanel: () => null, ServicesPanel: () => null }));

const { apiMock } = vi.hoisted(() => ({
  apiMock: { setupStatus: vi.fn(), scanStatus: vi.fn(), startScan: vi.fn() },
}));
vi.mock("../api", () => ({ api: apiMock }));

const READY = {
  has_radarr: true,
  has_sonarr: false,
  has_tautulli: true,
  has_seerr: false,
  has_scanned: false,
  scan_ready: true,
  complete: false,
};
const IDLE = {
  running: false,
  phase: "idle",
  done: 0,
  total: 0,
  percent: 0,
  detail: "",
  error: null,
  snapshot_id: null,
  followup_queued: false,
};
const RUNNING = {
  ...IDLE,
  running: true,
  phase: "history",
  percent: 3,
  detail: "syncing watch history",
};

function renderWizard(onSkip: () => void = () => {}) {
  const qc = testQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <SetupWizard onSkip={onSkip} />
    </QueryClientProvider>,
  );
}

// This screen replaces the whole app shell, so its landmarks are the only ones on the page --
// which is why these audit `document.body` with `pageLevel`, the way `Login` and the shell do,
// rather than a container inside a page that already has a `<main>` above it. Both branches are
// driven: the wizard renders a different tree when the setup status has not been read, and the
// branch a passing default hands you for free is the one a missing landmark hides behind
// (rule 145).
describe("the first screen a new operator meets", () => {
  it("has no accessibility violations, landmarks included", async () => {
    apiMock.setupStatus.mockResolvedValue(READY);
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderWizard();
    await screen.findByRole("button", { name: /run first scan/i });
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });

  it("has none either when the setup status cannot be read", async () => {
    // App.tsx routes an unreadable status here on the promise that Skip still works, so this
    // branch is reachable on an install that is not fresh at all.
    apiMock.setupStatus.mockRejectedValue(new Error("unreachable"));
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderWizard();
    await screen.findByText(/couldn't check the setup state/i);
    await expectNoA11yViolations(document.body, { pageLevel: true });
  });
});

describe("SetupWizard first scan", () => {
  it("offers Run first scan when connected and idle, with no way out yet", async () => {
    apiMock.setupStatus.mockResolvedValue(READY);
    apiMock.scanStatus.mockResolvedValue(IDLE);
    renderWizard();
    expect(await screen.findByRole("button", { name: /run first scan/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /go to the app/i })).not.toBeInTheDocument();
  });

  it("lets you head into the app the moment the first scan is running", async () => {
    apiMock.setupStatus.mockResolvedValue(READY);
    apiMock.scanStatus.mockResolvedValue(RUNNING);
    const onSkip = vi.fn();
    const person = userEvent.setup();
    renderWizard(onSkip);

    expect(await screen.findByText(/your first scan is running/i)).toBeInTheDocument();
    // No longer trapped: the run button is gone and a way into the app is offered instead.
    expect(screen.queryByRole("button", { name: /run first scan/i })).not.toBeInTheDocument();
    await person.click(screen.getByRole("button", { name: /go to the app/i }));
    expect(onSkip).toHaveBeenCalledTimes(1);
  });
});
