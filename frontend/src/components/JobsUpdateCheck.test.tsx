// SPDX-License-Identifier: AGPL-3.0-or-later
// The update check is the one upkeep job whose result a second query already renders: the
// Jobs row shows what the run found, while the About row, the version pill and the account
// chip's light all read `["update"]`, which holds its answer for half an hour and has nothing
// else to invalidate it. So a finished run has to refresh that query, or the two surfaces
// disagree about the same fact with the newer one on the page the operator is not looking at.
import type { QueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { About, Schedule, ScheduledJob } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_UPDATE, IDLE_SCAN } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { useUpdateStatus } from "../updateStatus";
import { Settings } from "./Settings";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const ABOUT: About = {
  version: "2026.8.1",
  license: "AGPL-3.0-or-later",
  data_dir: "/data",
  reaper_db_bytes: 1024,
  cache_db_bytes: 2048,
};

/** The server's own id for the job (`DEFAULT_MAINTENANCE_CRONS` in services/scheduler.py, and
 *  `UPDATE_CHECK_ID` in JobsPanel.tsx). A fixture that misspells it renders an ordinary row and
 *  the branch this file is about is unreachable, green either way (rule 141). */
const UPDATE_JOB = "check_for_updates";

function schedule(job: Partial<ScheduledJob>): Schedule {
  return {
    jobs: [
      {
        id: UPDATE_JOB,
        cron: "15 4 * * *",
        default_cron: "15 4 * * *",
        next_run_at: null,
        running: false,
        last_run_at: null,
        last_ok: null,
        last_result: null,
        ...job,
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.about.mockResolvedValue(ABOUT);
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  apiMock.safety.mockResolvedValue({ destructive_enabled: false, dry_run: true, reason: null });
  apiMock.latestSnapshot.mockResolvedValue(null);
  apiMock.leavingSoonSettings.mockResolvedValue({ enabled: false });
  apiMock.general.mockResolvedValue(null);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
});

/** Stands in for the account chip in the app's header (`UserMenu.tsx`), which holds the
 *  same `["update"]` query on every page and is therefore what turns an invalidation into a
 *  real re-ask. Settings does not mount the chip, so without this the query has no observer
 *  here and the invalidation would only mark it stale -- a test that passes whether or not the
 *  row invalidates anything (rule 141). */
function UpdateProbe() {
  useUpdateStatus();
  return null;
}

function renderJobs(): QueryClient {
  const queryClient = testQueryClient();
  renderWithProviders(
    <>
      <UpdateProbe />
      <Settings initialPanel="jobs" />
    </>,
    { client: queryClient },
  );
  return queryClient;
}

/** Drives one running -> done transition through the schedule query, the way the row's own
 *  1.5s poll delivers it. */
async function finishTheRun(queryClient: QueryClient): Promise<void> {
  apiMock.schedule.mockResolvedValue(
    schedule({ running: false, last_run_at: "2026-08-03T04:15:00+00:00", last_ok: true }),
  );
  await queryClient.invalidateQueries({ queryKey: ["schedule"] });
  await waitFor(() => expect(apiMock.schedule).toHaveBeenCalledTimes(2));
}

describe("the update check's Jobs row", () => {
  it("refreshes the answer every other update surface renders when the run finishes", async () => {
    apiMock.schedule.mockResolvedValue(schedule({ running: true }));
    const queryClient = renderJobs();
    // Waits on the row the server list produced, so the transition below is one this mount
    // actually watched rather than a state it was rendered into (rule 137).
    expect(await screen.findByText("Check for updates")).toBeInTheDocument();
    await waitFor(() => expect(apiMock.update).toHaveBeenCalledTimes(1));

    // The row itself, with the copy this change adds: the title, the description and the
    // status line the run writes into.
    await expectNoA11yViolations();

    await finishTheRun(queryClient);

    await waitFor(() => expect(apiMock.update).toHaveBeenCalledTimes(2));
  });

  it("leaves the answer alone when a different job finishes", async () => {
    // The invalidation is keyed on this job, not on any row finishing: a ratings refresh says
    // nothing about which Reaper is newest, and re-asking on every job's edge would put a
    // GitHub call behind an unrelated button.
    apiMock.schedule.mockResolvedValue({
      jobs: [{ ...schedule({}).jobs[0], id: "refresh_ratings", running: true }],
    });
    const queryClient = renderJobs();
    expect(await screen.findByText("Refresh IMDb ratings")).toBeInTheDocument();
    await waitFor(() => expect(apiMock.update).toHaveBeenCalledTimes(1));

    apiMock.schedule.mockResolvedValue({
      jobs: [{ ...schedule({}).jobs[0], id: "refresh_ratings", running: false, last_ok: true }],
    });
    await queryClient.invalidateQueries({ queryKey: ["schedule"] });
    await waitFor(() => expect(apiMock.schedule).toHaveBeenCalledTimes(2));

    expect(apiMock.update).toHaveBeenCalledTimes(1);
  });
});
