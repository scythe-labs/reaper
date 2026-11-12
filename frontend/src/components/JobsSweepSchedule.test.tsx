// SPDX-License-Identifier: AGPL-3.0-or-later
// The history sweep is the one upkeep job that does not run daily, and both halves of how that
// reads to the operator are computed rather than written down. Its cron says "every 3 days", a
// shape `describeCron` needs an arm for or it would print the raw cron string; and the preset
// that puts it back to the default takes its label from that same function, rather than
// hardcoding "Every day" for every job regardless of what picking it actually does.
//
// Both are pinned against the server's real default (`DEFAULT_MAINTENANCE_CRONS` in
// services/scheduler.py). A fixture inventing its own cron would prove the formatter works on
// a shape Reaper does not ship.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Schedule, ScheduledJob } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_GENERAL, DEFAULT_UPDATE, IDLE_SCAN } from "../test/apiFixtures";
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
        last_result_reason: null,
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
    // A hardcoded "Every day" label would be true of the other three jobs and false here,
    // which is exactly the drift a written-down label cannot avoid.
    expect(within(select).queryByRole("option", { name: "Every day" })).not.toBeInTheDocument();

    // The schedule editor open over the panel, which is the state this file is the only one
    // to drive for this job.
    await expectNoA11yViolations();
  });
});

/** The test runner's own zone, which the browser formats in when no zone is given. */
const RUNNER_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;

/** Two stored zones, neither the runner's, so a date formatted in the wrong zone shows. */
const SERVER_ZONES = ["Pacific/Kiritimati", "Pacific/Pago_Pago", "Asia/Kathmandu"]
  .filter((zone) => zone !== RUNNER_ZONE)
  .slice(0, 2);

/** Testing Library folds every whitespace run to one space, including the narrow space before
 *  AM and PM, so the expected text is folded the same way. */
const folded = (text: string) => text.replace(/\s+/g, " ");

/** The scan and the sweep, both next due at `at`. */
function dueAt(at: string): Schedule {
  const sweep = schedule({ next_run_at: at }).jobs[0]!;
  return {
    jobs: [{ ...sweep, id: "scheduled_scan", cron: "0 3 * * *", default_cron: "0 3 * * *" }, sweep],
  };
}

describe("a next run more than a day out", () => {
  it.each(SERVER_ZONES)("is written in the stored zone, %s, on both rows", async (zone) => {
    const at = new Date(Date.now() + 3 * 86_400_000).toISOString();
    const inStoredZone = folded(new Date(at).toLocaleString(undefined, { timeZone: zone }));
    // The runner's zone must print this instant differently, or the test proves nothing.
    expect(folded(new Date(at).toLocaleString())).not.toBe(inStoredZone);
    apiMock.general.mockResolvedValue({ ...DEFAULT_GENERAL, timezone: zone });
    apiMock.schedule.mockResolvedValue(dueAt(at));

    renderJobs();

    expect(await screen.findAllByText((text) => text.includes(inStoredZone))).toHaveLength(2);
  });

  it.each([
    ["not loaded", null],
    ["unknown to this browser", { ...DEFAULT_GENERAL, timezone: "Nowhere/Unlisted" }],
  ])("names the browser's zone when the stored zone is %s", async (_case, general) => {
    const at = new Date(Date.now() + 3 * 86_400_000).toISOString();
    const named = folded(new Date(at).toLocaleString(undefined, { timeZoneName: "short" }));
    apiMock.general.mockResolvedValue(general);
    apiMock.schedule.mockResolvedValue(dueAt(at));

    renderJobs();

    expect(await screen.findAllByText((text) => text.includes(named))).toHaveLength(2);
  });
});
