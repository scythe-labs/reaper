// SPDX-License-Identifier: AGPL-3.0-or-later
// The Settings surfaces that keep their content through a failed refetch and must say so
// rather than deny it: About, the Jobs panel (its notice, the scan row's schedule line and the
// Leaving Soon row inside it), and Notifications. React Query keeps the last good row and
// raises `isError` beside it, so a surface testing only `isError` prints "couldn't load this
// page" directly above the page it says did not load. Each is pinned in both directions here:
// the never-loaded sentence for a read that really never landed, and the stale line for one
// that landed and then blinked. A fix that showed the stale line in both cases would pass a
// one-sided test.
import { screen, waitFor } from "@testing-library/react";
import type { QueryClient } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { About, Schedule } from "../api";
import { DEFAULT_UPDATE, IDLE_SCAN } from "../test/apiFixtures";
import { testQueryClient } from "../test/queryClient";
import { renderWithProviders } from "../test/renderWithProviders";
import { Settings } from "./Settings";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
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
      // The scan's real id (`SCAN_ID` in JobsPanel.tsx). A fixture calling it "scan" leaves
      // `jobsById.get(SCAN_ID)` empty, so the scan row renders with no job at all, and the
      // branch these tests are about becomes unreachable. That would pass green either way.
      id: "scheduled_scan",
      cron: "0 3 * * *",
      default_cron: null,
      next_run_at: null,
      running: false,
      last_run_at: null,
      last_ok: null,
      last_result_reason: null,
    },
    {
      id: "refresh_ratings",
      cron: "0 4 * * *",
      default_cron: "0 4 * * *",
      next_run_at: null,
      running: false,
      last_run_at: null,
      last_ok: null,
      last_result_reason: null,
    },
  ],
};

const NEVER_LOADED_ABOUT = /Couldn't load this page/;
const NEVER_LOADED_JOBS = /Couldn't load the upkeep jobs/;
const NEVER_LOADED_SHELF = /Couldn't load the shelf status/;
// Anchored on the period, which is the whole difference: the two sentences open with the same
// eight words, and only the never-loaded one puts a full stop there, while the stale line runs
// on into "just now". Neither sentence offers to reload, since the webhook box below stays on
// screen in every branch and a reload would cost a pasted secret. The period is what still
// separates them, and a `$` would not, since RTL matches on the element's whole text.
const NEVER_LOADED_DISCORD = /Couldn't check whether Discord is connected\./;

// The shared sentence with any noun in it, for the negative assertions: no stale line at all.
const STALE_ANY = /Couldn't check .* just now/;
// And per surface, because `what` is the one thing a caller varies, and the loose form above
// cannot tell a supplied noun from the component's default. Deleting `what` at either call
// site would leave every test in this file green.
const STALE_ABOUT = /Couldn't check these details just now/;
const STALE_JOBS = /Couldn't check these jobs just now/;
const STALE_SHELF = /Couldn't check the shelf status just now/;
const STALE_DISCORD = /Couldn't check whether Discord is connected just now/;

// One sentence, stated in one place, with a noun each caller supplies. A failure here names the
// siblings that would have to move with it by file, since a comment asking a future author to
// remember costs nothing and does nothing.
const WHAT_HINT =
  "The stale line's noun is the `what` prop of StaleReadNotice.tsx, which owns the sentence. " +
  'Sibling call sites: AboutPanel.tsx ("these details"), JobsPanel.tsx ("these jobs" on the ' +
  'panel, "the shelf status" on LeavingSoonRow), NotificationsPanel.tsx ("whether Discord is ' +
  'connected"), ServicesPanel.tsx ("your connections"); in PlexPanel.tsx: the library grid ' +
  '("the library list"), the watch history record and the Leaving Soon group ("the Leaving Soon ' +
  'settings"), pinned in PlexPanel.test.tsx. GeneralPanel.tsx, SecurityPanel.tsx, ' +
  "BackupPanel.tsx and PlexPanel's own status read take the default.";

// The scan row's schedule line, from the fixture's cron. It is rendered from the same held row
// the upkeep rows below it use, so a blinked read must not blank it.
const SCAN_SCHEDULE = /Automatic scan: Every day at 3:00 AM, next not scheduled/;
const SCAN_UNKNOWN = "Couldn't check the schedule.";

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    dry_run: true,
    reason: null,
  });
  apiMock.latestSnapshot.mockResolvedValue(null);
  apiMock.leavingSoonSettings.mockResolvedValue({ enabled: false });
  apiMock.general.mockResolvedValue(null);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
});

function renderPanel(panel: "about" | "jobs" | "notifications"): QueryClient {
  const queryClient = testQueryClient();
  // `App` owns which panel is open, so the address bar can name it (navUrl.ts). Nothing
  // here switches panel, so the owner does nothing.
  renderWithProviders(<Settings panel={panel} onPanelChange={() => {}} />, {
    client: queryClient,
  });
  return queryClient;
}

/** Renders Jobs on a good schedule read, then blinks it: the refetch fails and React Query keeps
 *  the last good row. Waits on the row the server list produced, not on the absence of an error,
 *  because a negative assertion made before the read lands would pass for the wrong reason. */
async function jobsAfterFailedScheduleRefetch(): Promise<void> {
  apiMock.schedule.mockResolvedValue(SCHEDULE);
  const queryClient = renderPanel("jobs");
  expect(await screen.findByText("Refresh IMDb ratings")).toBeInTheDocument();

  apiMock.schedule.mockRejectedValue(new Error("boom"));
  await queryClient.invalidateQueries({ queryKey: ["schedule"] });
  await waitFor(() => expect(apiMock.schedule).toHaveBeenCalledTimes(2));
  await screen.findByText(STALE_ANY);
}

describe("AboutPanel through a failed refetch", () => {
  it("says the read is stale, not that the page never loaded", async () => {
    apiMock.about.mockResolvedValue(ABOUT);
    const queryClient = renderPanel("about");
    expect(await screen.findByText("Reaper 1.2.3")).toBeInTheDocument();

    // The refetch fails. The last good row survives it, so the panel is still fully drawn.
    apiMock.about.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["about"] });
    await waitFor(() => expect(apiMock.about).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(STALE_ABOUT);
    expect(stale).toHaveClass("notice-warn");
    // The claim that matters: it must not say the page failed to load, above the loaded page.
    expect(screen.queryByText(NEVER_LOADED_ABOUT)).toBeNull();
    expect(screen.getByText("Reaper 1.2.3")).toBeInTheDocument();
  });

  it("still says the page never loaded when the first read is the one that fails", async () => {
    apiMock.about.mockRejectedValue(new Error("boom"));
    renderPanel("about");

    expect(await screen.findByText(NEVER_LOADED_ABOUT)).toBeInTheDocument();
    // The stale line would be false here: nothing below it to be out of date.
    expect(screen.queryByText(STALE_ANY)).toBeNull();
  });
});

describe("JobsPanel through a failed refetch", () => {
  it("says the read is stale while the job rows are still on screen", async () => {
    await jobsAfterFailedScheduleRefetch();

    const stale = screen.getByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(STALE_JOBS);
    expect(stale).toHaveClass("notice-warn");
    expect(screen.queryByText(NEVER_LOADED_JOBS)).toBeNull();
    // The rows the sentence would have been talking over are still there.
    expect(screen.getByText("Refresh IMDb ratings")).toBeInTheDocument();
  });

  it("puts that line above the rows it says may be out of date", async () => {
    await jobsAfterFailedScheduleRefetch();

    // The sentence points at "what's below", and `.panel` is plain block flow, so DOM order is
    // the order it is read in. The rows sit below the notice, with the schedule editor and
    // nothing else below them.
    const stale = screen.getByText(STALE_ANY);
    const firstRow = screen.getByText("Refresh IMDb ratings");
    expect(stale.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the scan row's schedule, which is drawn from the same held row", async () => {
    await jobsAfterFailedScheduleRefetch();

    // The upkeep rows beside it go on printing next-run times off this same held answer, and the
    // notice above already says the rows may be out of date. Blanking this one line would say
    // the schedule is unknown while the panel still displays it.
    expect(screen.getByText(SCAN_SCHEDULE)).toBeInTheDocument();
    expect(screen.queryByText(SCAN_UNKNOWN)).toBeNull();
  });

  it("says it once when the shelf read fails alongside the schedule", async () => {
    // The two reads on this panel are independent polls: the schedule's runs every 1.5s while
    // any job does, and the shelf row's does not. So they can fail apart, and they can also
    // fail together, which must not print two near-identical amber lines, one of them inside a
    // row belonging to the other.
    apiMock.schedule.mockResolvedValue(SCHEDULE);
    apiMock.leavingSoonSettings.mockResolvedValue({ enabled: true, last: null });
    const queryClient = renderPanel("jobs");
    expect(await screen.findByText("Refresh IMDb ratings")).toBeInTheDocument();

    apiMock.schedule.mockRejectedValue(new Error("boom"));
    apiMock.leavingSoonSettings.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["schedule"] });
    await queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] });
    await waitFor(() => expect(apiMock.schedule).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(apiMock.leavingSoonSettings).toHaveBeenCalled());

    const lines = await screen.findAllByText(STALE_ANY);
    expect(lines, WHAT_HINT).toHaveLength(1);
    expect(lines[0]).toHaveTextContent(STALE_JOBS);
    // The row's own line is the one that goes: the panel is speaking for it now.
    expect(screen.queryByText(STALE_SHELF)).toBeNull();
    // Both rows are still on screen underneath it, which is why the panel keeps its surface.
    expect(screen.getByText("Refresh IMDb ratings")).toBeInTheDocument();
  });

  it("still says the jobs never loaded when the first read is the one that fails", async () => {
    apiMock.schedule.mockRejectedValue(new Error("boom"));
    renderPanel("jobs");

    expect(await screen.findByText(NEVER_LOADED_JOBS)).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
    // With no row ever held, the scan line has nothing to fall back on and says so.
    expect(screen.getByText(SCAN_UNKNOWN)).toBeInTheDocument();
  });
});

describe("the Leaving Soon row through a failed refetch", () => {
  it("keeps the row and says the shelf status is stale", async () => {
    apiMock.schedule.mockResolvedValue(SCHEDULE);
    apiMock.leavingSoonSettings.mockResolvedValue({
      enabled: true,
      allow_unarmed: false,
      name: "Leaving Soon",
      applied_name: "Leaving Soon",
      last: null,
    });
    const queryClient = renderPanel("jobs");
    // Settle the state this button's existence depends on before reaching for it. `findByRole`
    // with a name matcher recomputes accessible names across the whole Settings tree on every
    // poll, which is the most expensive query Testing Library has, and here it also has to wait
    // for the shelf read to land. That can lose the race on a loaded CI runner, leaving the
    // panel stuck on "Loading the upkeep jobs…".
    //
    // "Runs after every scan" is the cheap text the enabled branch renders, so waiting on it
    // settles the same read and takes the button synchronously once it holds. It also pins the
    // right branch: the row's OFF branch draws a disabled "Update now" too, so waiting on the
    // button alone would not prove the ON branch had actually loaded.
    //
    // Bumping the timeout instead would hide the next regression in this chain rather than
    // pinning it.
    //
    // A generic latency-margin sweep does not catch this failure, because the reads still land
    // inside the test's time budget. What runs out is the budget spent polling between them,
    // which is query cost under a starved CPU rather than notification latency.
    await screen.findByText("Runs after every scan");
    expect(screen.getByRole("button", { name: /^Update now/ })).toBeInTheDocument();

    // "Update now" invalidates exactly this key when it succeeds, so a blinked refetch here
    // follows a shelf update that worked. It must not answer that by declaring the shelf
    // unknown.
    apiMock.leavingSoonSettings.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["leaving-soon-settings"] });
    await waitFor(() => expect(apiMock.leavingSoonSettings).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(STALE_SHELF);
    expect(stale).toHaveClass("notice-warn");
    expect(screen.queryByText(NEVER_LOADED_SHELF)).toBeNull();
    // The row itself, with the action that just succeeded, is still on screen.
    expect(screen.getByRole("button", { name: /^Update now/ })).toBeEnabled();
    expect(screen.getByText("Runs after every scan")).toBeInTheDocument();
  });

  it("still says the shelf status never loaded when the first read is the one that fails", async () => {
    apiMock.schedule.mockResolvedValue(SCHEDULE);
    apiMock.leavingSoonSettings.mockRejectedValue(new Error("boom"));
    renderPanel("jobs");

    expect(await screen.findByText(NEVER_LOADED_SHELF)).toBeInTheDocument();
    expect(screen.queryByText(STALE_ANY)).toBeNull();
  });
});

describe("NotificationsPanel through a failed refetch", () => {
  it("says the read is stale, not that the check never ran", async () => {
    apiMock.notifications.mockResolvedValue({ has_webhook: true });
    const queryClient = renderPanel("notifications");
    expect(await screen.findByText(/Discord connected/)).toBeInTheDocument();

    // Saving or removing a webhook invalidates this key, so its own success path reaches here.
    apiMock.notifications.mockRejectedValue(new Error("boom"));
    await queryClient.invalidateQueries({ queryKey: ["notifications"] });
    await waitFor(() => expect(apiMock.notifications).toHaveBeenCalledTimes(2));

    const stale = await screen.findByText(STALE_ANY);
    expect(stale, WHAT_HINT).toHaveTextContent(STALE_DISCORD);
    expect(stale).toHaveClass("notice-warn");
    expect(screen.queryByText(NEVER_LOADED_DISCORD)).toBeNull();
    // The three controls below are still derived from the held answer, and the line above them
    // agrees that the data was actually read.
    expect(screen.getByText(/Discord connected/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send test" })).toBeEnabled();
  });

  it("still says the check never ran when the first read is the one that fails", async () => {
    apiMock.notifications.mockRejectedValue(new Error("boom"));
    renderPanel("notifications");

    expect(await screen.findByText(NEVER_LOADED_DISCORD)).toBeInTheDocument();
    // Nothing was ever read, so nothing below it can be called out of date. Remove and the test
    // send stay unavailable, rather than acting on a webhook nobody confirmed exists.
    expect(screen.queryByText(STALE_ANY)).toBeNull();
    expect(screen.queryByRole("button", { name: "Remove" })).toBeNull();
    expect(screen.getByRole("button", { name: "Send test" })).toBeDisabled();
  });
});
