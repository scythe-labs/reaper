// SPDX-License-Identifier: AGPL-3.0-or-later
// What the shelf row says about the pass behind it.
//
// Two things, and they are one subject: which record it answers with, and whose words it
// answers in.
//
// Every skip in `leaving_soon.after_scan` returns before the pass writes its record. The row
// must not answer for a skipped scan using the last COMPLETED pass's own green dot, timestamp,
// and counts. The skip is recorded separately and never cleared, so what retires it is a later
// pass carrying a later timestamp, which is why the row must compare the two rather than just
// preferring whichever exists, and why both directions of that comparison are driven here.
//
// The words shown must be the server's own sentence, not one composed from the response's
// counts and flags: composing locally can say the shelves failed while the stored row it sits
// on is green, when no library is turned on.
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LeavingSoonResult, LeavingSoonSettings, Schedule } from "../api";
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

const EMPTY_SCHEDULE: Schedule = { jobs: [] };

/** The completed pass. Its counts are the ones still on the shelf whatever happened after,
 *  because a skipped pass writes nothing to Plex. */
const COMPLETED = {
  at: "2026-08-03T02:00:00+00:00",
  movies: 280,
  seasons: 311,
  applied: true,
  ok: true,
  result_reason: { k: "shelf_updated", p: { added: 4, removed: 1 } },
};

/** Deliberately AFTER `COMPLETED`, since the row's whole decision is which is newer. A fixture
 *  sharing the completed pass's instant would make the comparison unfalsifiable: an untouched
 *  ordering would look the same as a correct one. `result_reason` is a real catalog code:
 *  `error.leaving_soon.skip_unreachable` composes to exactly "Reaper couldn't reach Plex," which
 *  is what the assertions below read, so this fixture proves the real composer renders it
 *  rather than transcribing it. */
const SKIPPED = {
  at: "2026-08-04T20:06:00+00:00",
  result_reason: { k: "error.leaving_soon.skip_unreachable", p: {} },
};

/** The catalog's own sentence for `jobs.result.shelf_no_libraries`
 *  (`LeavingSoonResult.summary`), composed the same way the row composes it. */
const NO_LIBRARIES = "No libraries are turned on, so no shelf was updated";

/** What the route returns for that pass. Nothing was written and no library failed, so a row
 *  reasoning from the write counts rather than from `ok` and `result_reason` reads this as a
 *  clean preview: reverting the fix flashes a green "Preview only, nothing written" against
 *  this exact payload. */
const NO_LIBRARY_PASS: LeavingSoonResult = {
  ok: false,
  result_reason: { k: "shelf_no_libraries", p: null },
};

function shelf(over: Partial<LeavingSoonSettings> = {}): LeavingSoonSettings {
  return {
    enabled: true,
    allow_unarmed: false,
    name: "Leaving Soon",
    applied_name: "Leaving Soon",
    last: COMPLETED,
    last_skip: null,
    ...over,
  };
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
  // `App` owns which panel is open, so the address bar can name it (navUrl.ts). Nothing
  // here switches panel, so the owner does nothing.
  renderWithProviders(<Settings panel="jobs" onPanelChange={() => {}} />);
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
    // The reason is named outright: it is the only thing on the row telling the operator what
    // to go and fix, and "failed" alone would read the same for either skip.
    expect(status).toContain("Reaper couldn't reach Plex");
    // The counts survive, because the shelf still holds them -- a skipped pass wrote nothing.
    // Past tense is the correction: they stop reading as this scan's outcome.
    expect(counts).toContain("280");
    expect(counts).toContain("were on the shelves at the last update");

    await expectNoA11yViolations();
  });

  it("goes back to the completed pass once a later one lands", async () => {
    // The direction that proves the row COMPARES the two, rather than merely preferring a skip
    // whenever one exists. Nothing clears the skip record, so the row must still report a
    // recovered shelf as recovered, not as permanently broken.
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

  it("rests on the reason the pass stored, in the pass's own words", async () => {
    // One pass, one sentence: the row reads it rather than deriving a second one. Checks the
    // wiring from the stored summary to the screen; the sentence itself is the service's, and
    // `tests/test_leaving_soon.py` owns its wording.
    apiMock.leavingSoonSettings.mockResolvedValue(
      shelf({
        last: { ...COMPLETED, ok: false, result_reason: { k: "shelf_no_libraries", p: null } },
      }),
    );

    const { status } = await shelfRow();

    expect(status).toContain("Last run failed");
    expect(status).toContain(NO_LIBRARIES);
  });

  it("reports a skip on an install whose shelf has never completed a pass", async () => {
    // `last` is null until the first pass lands, so the row has no second timestamp to compare
    // against. It must still speak here: this is the shape where the shelf has been broken
    // from the day it was switched on, which is when silence misleads most.
    apiMock.leavingSoonSettings.mockResolvedValue(shelf({ last: null, last_skip: SKIPPED }));

    const { status, counts } = await shelfRow();

    expect(status).toContain("Last run failed");
    expect(status).toContain("Reaper couldn't reach Plex");
    // No completed pass means no counts to qualify, so that line is absent rather than
    // reporting a shelf nobody has ever measured.
    expect(counts).toBe("");
  });
});

describe("the shelf row while a rename is still outstanding", () => {
  /** Reads the whole row, since the rename line sits beside the schedule rather than in the
   *  status sentence. */
  async function rowText(): Promise<string> {
    renderWithProviders(<Settings panel="jobs" onPanelChange={() => {}} />);
    const title = await screen.findByText("Update Leaving Soon shelf");
    const row = title.closest(".jobrow");
    expect(row, "the shelf row is not on the Jobs panel").not.toBeNull();
    await waitFor(() => expect(row?.querySelector(".jobrow-last")).not.toBeNull());
    return row?.textContent ?? "";
  }

  it("names the shelf Plex still shows, beside the button that would move it", async () => {
    // Saving a name stores it and nothing else: moving the shelf is a whole-library reconcile
    // per library. Until a pass runs, the operator's library still shows the OLD name, and
    // this row is where they can act on that.
    apiMock.leavingSoonSettings.mockResolvedValue(
      shelf({ name: "Last chance", applied_name: "Leaving Soon" }),
    );

    expect(await rowText()).toContain('Plex still shows "Leaving Soon". This update renames it.');

    await expectNoA11yViolations();
  });

  it("says nothing once the pass has carried it across", async () => {
    apiMock.leavingSoonSettings.mockResolvedValue(
      shelf({ name: "Last chance", applied_name: "Last chance" }),
    );

    expect(await rowText()).not.toContain("Plex still shows");
  });

  it("says nothing while the shelf is off", async () => {
    // No pass runs with the shelf off, so the stored and applied names would disagree forever,
    // and the sentence would describe a shelf that is not in the library at all.
    apiMock.leavingSoonSettings.mockResolvedValue(
      shelf({ enabled: false, name: "Last chance", applied_name: "Leaving Soon" }),
    );
    renderWithProviders(<Settings panel="jobs" onPanelChange={() => {}} />);
    const title = await screen.findByText("Update Leaving Soon shelf");

    expect(title.closest(".jobrow")?.textContent ?? "").not.toContain("Plex still shows");
  });
});

describe("the shelf row's confirmation after Update now", () => {
  it("flashes what the pass said, and calls it a failure when the pass did", async () => {
    // This is the one an operator meets first. The chip must derive from the pass's own
    // outcome, not from fields describing only the WRITES, which would read a pass like this
    // one as a clean preview and flash a green tick over a pass that updated nothing.
    const user = userEvent.setup();
    apiMock.leavingSoonSettings.mockResolvedValue(shelf({ last: null }));
    // Held open, then released: the chip only exists on a running -> finished transition the
    // row watches, so a mock that answers inside the click could land both states in one
    // commit and leave nothing to flash.
    let finish!: (result: LeavingSoonResult) => void;
    apiMock.syncLeavingSoon.mockReturnValue(
      new Promise<LeavingSoonResult>((resolve) => {
        finish = resolve;
      }),
    );
    renderWithProviders(<Settings panel="jobs" onPanelChange={() => {}} />);

    // The enabled branch's own text, so the wait settles the read this button's existence
    // depends on rather than racing it (the off branch draws a disabled "Update now" too).
    await screen.findByText("Runs after every scan");
    const button = screen.getByRole("button", { name: /^Update now/ });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    // The pass is in flight: the row disables its own button while it runs, and that is the
    // state the chip below is a transition OUT of.
    await waitFor(() => expect(button).toBeDisabled());
    await act(async () => {
      finish(NO_LIBRARY_PASS);
    });

    const chip = await screen.findByText(NO_LIBRARIES, { exact: false, selector: ".flash-chip" });
    // The words, and the claim the tick makes about them. Both come off the response, so a
    // sentence rendered under the wrong lead is as wrong as the wrong sentence.
    expect(chip.textContent).toContain("Failed:");
    expect(chip.closest(".jobrow-last")).toHaveClass("is-flash-fail");
  });
});
