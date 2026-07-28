// SPDX-License-Identifier: AGPL-3.0-or-later
// The two Settings panels that hold no draft and still keep their surface through a failed
// refetch: About and Jobs. React Query keeps the last good row and raises `isError` beside it,
// so a panel testing only `isError` prints "couldn't load this page" directly above the page it
// says did not load. Each panel is pinned in both directions here -- the never-loaded sentence
// for a read that really never landed, the stale line for one that landed and then blinked --
// because a fix that showed the stale line in both cases would pass a one-sided test (#140).
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { About, Schedule } from "../api";
import { IDLE_SCAN } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { Settings } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    // Rule 135: the mock answers everything the shell mounts, not only the panel under test.
    // The masthead's safety read and the scan snapshot ride along on every panel.
    about: vi.fn(),
    safety: vi.fn(),
    schedule: vi.fn(),
    latestSnapshot: vi.fn(),
    leavingSoonSettings: vi.fn(),
    general: vi.fn(),
    scanStatus: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const ABOUT: About = {
  version: "1.2.3",
  license: "AGPL-3.0-or-later",
  data_dir: "/data",
  reaper_db_bytes: 1024,
  cache_db_bytes: 2048,
};

const SCHEDULE: Schedule = {
  jobs: [
    {
      id: "scan",
      cron: "0 3 * * *",
      default_cron: null,
      next_run_at: null,
      running: false,
      last_run_at: null,
      last_ok: null,
      last_result: null,
    },
    {
      id: "refresh_ratings",
      cron: "0 4 * * *",
      default_cron: "0 4 * * *",
      next_run_at: null,
      running: false,
      last_run_at: null,
      last_ok: null,
      last_result: null,
    },
  ],
};

const NEVER_LOADED_ABOUT = /Couldn't load this page/;
const NEVER_LOADED_JOBS = /Couldn't load the upkeep jobs/;
const STALE = /Couldn't check .* just now/;

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    dry_run: true,
    reason: null,
  });
  apiMock.latestSnapshot.mockResolvedValue(null);
  apiMock.leavingSoonSettings.mockResolvedValue({ enabled: false });
  apiMock.general.mockResolvedValue(null);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
});

describe("AboutPanel through a failed refetch", () => {
  it("says the read is stale, not that the page never loaded", async () => {
    apiMock.about.mockResolvedValue(ABOUT);
    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <Settings initialPanel="about" />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("Reaper 1.2.3")).toBeInTheDocument();

    // The refetch fails. The last good row survives it, so the panel is still fully drawn.
    apiMock.about.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["about"] });
    await waitFor(() => expect(apiMock.about).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE);
    expect(stale).toHaveClass("notice-warn");
    // The claim that matters: it does NOT say the page failed to load, above the loaded page.
    expect(screen.queryByText(NEVER_LOADED_ABOUT)).toBeNull();
    expect(screen.getByText("Reaper 1.2.3")).toBeInTheDocument();
  });

  it("still says the page never loaded when the first read is the one that fails", async () => {
    apiMock.about.mockRejectedValue(new Error("boom"));
    render(
      <QueryClientProvider client={testQueryClient()}>
        <Settings initialPanel="about" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(NEVER_LOADED_ABOUT)).toBeInTheDocument();
    // The stale line would be false here: nothing below it to be out of date.
    expect(screen.queryByText(STALE)).toBeNull();
  });
});

describe("JobsPanel through a failed refetch", () => {
  it("says the read is stale while the job rows are still on screen", async () => {
    apiMock.schedule.mockResolvedValue(SCHEDULE);
    const queryClient = testQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <Settings initialPanel="jobs" />
      </QueryClientProvider>,
    );
    // Wait on the row the server list produced, not on the absence of an error: a negative
    // assertion made before the read lands passes for the wrong reason (rule 137).
    expect(await screen.findByText("Refresh IMDb ratings")).toBeInTheDocument();

    apiMock.schedule.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["schedule"] });
    await waitFor(() => expect(apiMock.schedule).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE);
    expect(stale).toHaveClass("notice-warn");
    expect(screen.queryByText(NEVER_LOADED_JOBS)).toBeNull();
    // The rows the sentence would have been talking over are still there.
    expect(screen.getByText("Refresh IMDb ratings")).toBeInTheDocument();
  });

  it("still says the jobs never loaded when the first read is the one that fails", async () => {
    apiMock.schedule.mockRejectedValue(new Error("boom"));
    render(
      <QueryClientProvider client={testQueryClient()}>
        <Settings initialPanel="jobs" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(NEVER_LOADED_JOBS)).toBeInTheDocument();
    expect(screen.queryByText(STALE)).toBeNull();
  });
});
