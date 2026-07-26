// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The one behavior that matters on the first-run screen: once the first scan is running you
// can leave for the app immediately, instead of being held on the wizard until it finishes.
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
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
