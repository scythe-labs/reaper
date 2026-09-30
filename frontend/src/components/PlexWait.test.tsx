// SPDX-License-Identifier: AGPL-3.0-or-later
// What the app shows while Reaper waits for Plex to finish the scans a reap asked for.
//
// The server holds the wait and refuses a scan start until it ends. These tests pin the other
// half: every control that would start a scan or read Plex's library is off and says why, and
// the controls that do not read Plex stay on.
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReapStatus } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_UPDATE, IDLE_SCAN } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { ReapBar } from "./ReapBar";
import { Settings } from "./Settings";
import { StaleNotice } from "./PolicySimulator";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const WAITING = { ...IDLE_SCAN, waiting_for_plex_since: "2026-09-29T10:00:00Z" };

const ENDED_REAP: ReapStatus = {
  running: false,
  run_id: 16,
  stopping: false,
  phase: "complete",
  done: 531,
  total: 531,
  deleted_items: 531,
  deleted_bytes: 6.2e12,
  binned_bytes: 0,
  skipped: 0,
  title: "",
  error_reason: null,
};

const JOBS = {
  jobs: [
    {
      id: "refresh_ratings",
      cron: "30 3 * * *",
      default_cron: "30 3 * * *",
      next_run_at: null,
      last_run_at: null,
      last_ok: null,
      last_result_reason: null,
      running: false,
      enabled: true,
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.about.mockResolvedValue(null);
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  apiMock.safety.mockResolvedValue({ destructive_enabled: false, dry_run: true, reason: null });
  apiMock.latestSnapshot.mockResolvedValue(null);
  apiMock.schedule.mockResolvedValue(JOBS);
  apiMock.general.mockResolvedValue(null);
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
  apiMock.leavingSoonSettings.mockResolvedValue({
    enabled: true,
    allow_unarmed: false,
    name: "Leaving Soon",
    applied_name: "Leaving Soon",
    last: null,
    last_skip: null,
  });
});

describe("the reap bar while Plex catches up", () => {
  it("turns amber and says scans start when Plex is done", async () => {
    apiMock.reapStatus.mockResolvedValue(ENDED_REAP);
    apiMock.scanStatus.mockResolvedValue(WAITING);

    const { container } = renderWithProviders(<ReapBar onGoToReap={() => {}} />);

    expect(await screen.findByText(/Waiting for Plex to finish updating/)).toBeInTheDocument();
    expect(container.querySelector(".reap-bar.waiting")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeEnabled();
    await expectNoA11yViolations(container);
  });

  it("returns to the green bar once the wait is over", async () => {
    apiMock.reapStatus.mockResolvedValue(ENDED_REAP);
    apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);

    const { container } = renderWithProviders(<ReapBar onGoToReap={() => {}} />);

    await screen.findByText("Reaped.");
    expect(container.querySelector(".reap-bar.done")).not.toBeNull();
    expect(screen.queryByText(/Waiting for Plex/)).toBeNull();
  });
});

describe("Settings, Jobs while Plex catches up", () => {
  it("turns off the scan and the shelf update and leaves the other jobs on", async () => {
    apiMock.scanStatus.mockResolvedValue(WAITING);

    renderWithProviders(<Settings panel="jobs" onPanelChange={() => {}} />);

    expect(await screen.findByText(/Jobs that read Plex wait until it's done/)).toBeInTheDocument();
    const rowOf = (title: string) => {
      const row = screen.getByText(title).closest(".jobrow");
      expect(row, `no row titled ${title}`).not.toBeNull();
      return row as HTMLElement;
    };
    await waitFor(() => {
      expect(rowOf("Update Leaving Soon shelf").querySelector(".slot-act button")).toBeDisabled();
    });
    expect(
      rowOf("Update library and apply policy").querySelector(".slot-act button"),
    ).toBeDisabled();
    expect(rowOf("Update Leaving Soon shelf")).toHaveTextContent("Waits for Plex");
    expect(rowOf("Update library and apply policy")).toHaveTextContent(
      "Starts on its own when Plex is done",
    );
    expect(rowOf("Refresh IMDb ratings").querySelector(".slot-act button")).toBeEnabled();
    await expectNoA11yViolations();
  });

  it("shows no notice and keeps both buttons on when Plex is not being waited for", async () => {
    apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);

    renderWithProviders(<Settings panel="jobs" onPanelChange={() => {}} />);

    const shelf = (await screen.findByText("Update Leaving Soon shelf")).closest(".jobrow");
    await waitFor(() => expect(shelf?.querySelector(".slot-act button")).toBeEnabled());
    expect(screen.queryByText(/Jobs that read Plex wait/)).toBeNull();
  });
});

describe("a Scan button elsewhere", () => {
  const props = {
    scanning: false,
    followupQueued: false,
    starting: false,
    startError: null,
    onScan: () => {},
    percent: 0,
    detail: "",
    staleKind: null,
    staleReason: null,
  };

  it("is off and gives the reason while Plex catches up", () => {
    render(<StaleNotice {...props} waitingForPlex />);

    expect(screen.getByRole("button", { name: "Scan now" })).toBeDisabled();
    expect(screen.getByText("Waits for Plex to finish updating")).toBeInTheDocument();
  });

  it("is on otherwise", () => {
    render(<StaleNotice {...props} />);

    expect(screen.getByRole("button", { name: "Scan now" })).toBeEnabled();
    expect(screen.queryByText("Waits for Plex to finish updating")).toBeNull();
  });
});
