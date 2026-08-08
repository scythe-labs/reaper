// SPDX-License-Identifier: AGPL-3.0-or-later
// What the shelf row says about a scan that skipped it.
//
// Every skip in `leaving_soon.after_scan` returns before the pass writes its record, so this
// row re-read the last COMPLETED pass and answered for the scan with it: a green dot, an old
// timestamp, and counts, under a line reading "Runs after every scan". The skip is recorded
// separately and never cleared, so what retires it is a later pass carrying a later timestamp
// -- which makes the row's own comparison the whole mechanism, and the reason both directions
// are driven here rather than only the failing one.
import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LeavingSoonSettings, Schedule } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_UPDATE, IDLE_SCAN } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { Settings } from "./Settings";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    // Rule 135: the mock answers everything the shell mounts, not only the row under test.
    about: vi.fn(),
    update: vi.fn(),
    safety: vi.fn(),
    schedule: vi.fn(),
    latestSnapshot: vi.fn(),
    leavingSoonSettings: vi.fn(),
    general: vi.fn(),
    scanStatus: vi.fn(),
    notifications: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const EMPTY_SCHEDULE: Schedule = { jobs: [] };

/** The completed pass. Its counts are the ones still on the shelf whatever happened after,
 *  because a skipped pass writes nothing to Plex. */
const COMPLETED = {
  at: "2026-08-03T02:00:00+00:00",
  movies: 280,
  seasons: 311,
  applied: true,
  ok: true,
  result: "4 added, 1 cleared",
};

/** Deliberately AFTER `COMPLETED`, since the row's whole decision is which is newer. A fixture
 *  sharing the completed pass's instant would make the comparison unfalsifiable (rule 141). */
const SKIPPED = { at: "2026-08-04T20:06:00+00:00", result: "Reaper couldn't reach Plex" };

function shelf(over: Partial<LeavingSoonSettings> = {}): LeavingSoonSettings {
  return { enabled: true, allow_unarmed: false, last: COMPLETED, last_skip: null, ...over };
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.about.mockResolvedValue(null);
  apiMock.update.mockResolvedValue(DEFAULT_UPDATE);
  apiMock.safety.mockResolvedValue({ destructive_enabled: false, dry_run: true, reason: null });
  apiMock.latestSnapshot.mockResolvedValue(null);
  apiMock.schedule.mockResolvedValue(EMPTY_SCHEDULE);
  apiMock.general.mockResolvedValue(null);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.notifications.mockResolvedValue({ has_webhook: false });
});

/** The row's own status line and its counts line, read off the DOM the way `JobStatus.test`
 *  reads them: the sentence is built from nested spans, so no single text node holds it. */
async function shelfRow(): Promise<{ status: string; counts: string }> {
  renderWithProviders(<Settings initialPanel="jobs" />);
  const title = await screen.findByText("Update Leaving Soon shelf");
  const row = title.closest(".jobrow");
  // Not an optional chain into the assertions below: a selector that stopped matching would
  // hand every one of them `undefined`, and "does not contain" passes against that.
  expect(row, "the shelf row is not on the Jobs panel").not.toBeNull();
  await waitFor(() => expect(row?.querySelector(".jobrow-last")).not.toBeNull());
  return {
    status: row?.querySelector(".jobrow-last")?.textContent ?? "",
    counts: row?.querySelector(".jobrow-meta")?.textContent ?? "",
  };
}

describe("the shelf row after a scan that skipped the update", () => {
  it("reports the skip instead of the pass before it, and says why", async () => {
    apiMock.leavingSoonSettings.mockResolvedValue(shelf({ last_skip: SKIPPED }));

    const { status, counts } = await shelfRow();

    expect(status).toContain("Last run failed");
    // The reason, named outright: it is the only thing on the row telling the operator what to
    // go and fix, and "failed" alone would read the same for either skip.
    expect(status).toContain("Reaper couldn't reach Plex");
    // The counts survive, because the shelf still holds them -- a skipped pass wrote nothing.
    // Past tense is the correction: they stop reading as this scan's outcome.
    expect(counts).toContain("280");
    expect(counts).toContain("were on the shelves at the last update");

    await expectNoA11yViolations();
  });

  it("goes back to the completed pass once a later one lands", async () => {
    // The direction that proves the row COMPARES the two rather than merely preferring a skip
    // whenever one exists. Nothing clears the skip record, so without this the row would
    // report a shelf that recovered as still broken, forever.
    apiMock.leavingSoonSettings.mockResolvedValue(
      shelf({
        last: { ...COMPLETED, at: "2026-08-04T22:30:00+00:00" },
        last_skip: SKIPPED,
      }),
    );

    const { status, counts } = await shelfRow();

    expect(status).not.toContain("failed");
    expect(status).not.toContain("Reaper couldn't reach Plex");
    expect(counts).toContain("on the shelves");
    expect(counts).not.toContain("at the last update");
  });

  it("reads exactly as before when no scan has ever skipped the shelf", async () => {
    apiMock.leavingSoonSettings.mockResolvedValue(shelf());

    const { status, counts } = await shelfRow();

    expect(status).toContain("Last run");
    expect(status).not.toContain("failed");
    expect(counts).toContain("280 movies and 311 seasons on the shelves");
  });

  it("reports a skip on an install whose shelf has never completed a pass", async () => {
    // `last` is null until the first pass lands, and the row's comparison has no second
    // timestamp to make. It still has to speak: this is the shape where the shelf has been
    // broken from the day it was switched on, which is when silence misleads most.
    apiMock.leavingSoonSettings.mockResolvedValue(shelf({ last: null, last_skip: SKIPPED }));

    const { status, counts } = await shelfRow();

    expect(status).toContain("Last run failed");
    expect(status).toContain("Reaper couldn't reach Plex");
    // No completed pass means no counts to qualify, so that line is absent rather than
    // reporting a shelf nobody has ever measured.
    expect(counts).toBe("");
  });
});
