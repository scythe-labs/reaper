// SPDX-License-Identifier: AGPL-3.0-or-later
// The history sweep is the one upkeep job that does not run daily, and both halves of how that
// reads to the operator are computed rather than written down. Its cron says "every 3 days", a
// shape `describeCron` had no arm for and would otherwise print raw; and the preset that puts
// it back to the default takes its LABEL from that same function, after reading "Every day" for
// every job regardless of what picking it did (rule 144).
//
// Both are pinned against the server's real default (`DEFAULT_MAINTENANCE_CRONS` in
// services/scheduler.py). A fixture inventing its own cron would prove the formatter works on
// a shape we do not ship (rule 141).
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Schedule, ScheduledJob } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_UPDATE, IDLE_SCAN } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { Settings } from "./Settings";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

/** The server's own id and default for the sweep, spelled as `scheduler.py` spells them. */
const SWEEP_JOB = "full_history_sweep";
const SWEEP_DEFAULT = "0 5 */3 * *";

function schedule(job: Partial<ScheduledJob> = {}): Schedule {
  return {
    jobs: [
      {
        id: SWEEP_JOB,
        cron: SWEEP_DEFAULT,
        default_cron: SWEEP_DEFAULT,
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
  apiMock.about.mockResolvedValue({
    version: "2026.8.1",
    license: "AGPL-3.0-or-later",
    data_dir: "/data",
    reaper_db_bytes: 1024,
    cache_db_bytes: 2048,
  });
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  apiMock.safety.mockResolvedValue({ destructive_enabled: false, dry_run: true, reason: null });
  apiMock.latestSnapshot.mockResolvedValue(null);
  apiMock.leavingSoonSettings.mockResolvedValue({ enabled: false });
  apiMock.general.mockResolvedValue(null);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
  apiMock.schedule.mockResolvedValue(schedule());
});

function renderJobs(): void {
  renderWithProviders(<Settings panel="jobs" onPanelChange={() => {}} />);
}

describe("the history sweep's schedule, as the operator reads it", () => {
  it("says every 3 days in words, not a raw cron line", async () => {
    renderJobs();

    // Not "Custom (0 5 */3 * *)", which is what a missing day-step arm prints.
    expect(await screen.findByText(/Every 3 days at 5:00 AM/)).toBeInTheDocument();
    expect(screen.queryByText(/0 5 \*\/3/)).not.toBeInTheDocument();
  });

  it("labels the default preset with what picking it actually does", async () => {
    renderJobs();

    const edit = await screen.findByRole("button", { name: /Edit.*history/i });
    await userEvent.click(edit);

    const select = await screen.findByRole("combobox", { name: "How often" });
    expect(within(select).getByRole("option", { name: /Every 3 days at 5:00 AM/ })).toHaveValue(
      SWEEP_DEFAULT,
    );
    // The label this replaced. It was true of the other three jobs and false here, which is
    // exactly the drift a written-down label cannot avoid.
    expect(within(select).queryByRole("option", { name: "Every day" })).not.toBeInTheDocument();

    // The schedule editor open over the panel, which is the state this file is the only one
    // to drive for this job.
    await expectNoA11yViolations();
  });
});
