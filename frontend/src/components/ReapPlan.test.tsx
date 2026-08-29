// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Reap tab's idle and blocked states: the summary card's tiles, help sentence and
// blockers notice, the head actions, the standalone practice run, and the history card.
// Confirmation and execution stay ReapConfirm.tsx's own tests; this file proves that pressing
// the head Reap button hands it a freshly built run and opens it.
//
// Reaping, done, history paging and the run detail sheet (Phase 3) share this file: all four
// are driven off the same `["reapStatus"]` poll and the executed-history/outcomes reads, so
// their fixtures live beside the idle ones above rather than in a second file.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ReapBreakdown,
  ReapStatus,
  Run,
  RunOutcomeRead,
  RunReport,
  RunSummary,
  SetupStatus,
} from "../api";
import { bytes, count } from "../format";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_PROFILE, DEFAULT_SNAPSHOT, IDLE_SCAN, READY_SETUP } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { ReapPlan } from "./ReapPlan";
import { ACK_KEY } from "./runAck";

const { apiMock } = await vi.hoisted(async () => ({
  apiMock: (await import("../test/apiMock")).makeApiMock(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const GB = 1024 ** 3;

function breakdown(overrides: Partial<ReapBreakdown> = {}): ReapBreakdown {
  return {
    has_snapshot: true,
    policy_condemned: 50,
    policy_condemned_bytes: 500 * GB,
    hand_spared: 0,
    spares_expired: 0,
    hand_reaped: 0,
    hand_reaped_bytes: 0,
    hand_reaped_held: 0,
    will_reap: 47,
    will_reap_bytes: 470 * GB,
    will_reap_unknown: 0,
    movies: 38,
    movies_unknown: 0,
    seasons: 9,
    seasons_unknown: 0,
    condemned_by: [{ id: "unwatched", count: 40 }],
    ...overrides,
  };
}

const run: Run = {
  id: 12,
  snapshot_id: 1,
  state: "planned",
  item_count: 47,
  total_bytes: 470 * GB,
  confirmation_phrase: "REAP 47 TITLES 470 GB",
  held_back_unknown_size: 0,
  step_count: 0,
  steps: [],
};

const idleReapStatus: ReapStatus = {
  running: false,
  run_id: null,
  stopping: false,
  phase: "idle",
  done: 0,
  total: 0,
  deleted_items: 0,
  deleted_bytes: 0,
  skipped: 0,
  title: "",
  error_reason: null,
};

function dryReport(overrides: Partial<RunReport> = {}): RunReport {
  return {
    run_id: run.id,
    dry_run: true,
    state: "completed",
    aborted_reason: null,
    would_delete_items: 47,
    deleted_bytes: 470 * GB,
    deleted_unmeasured: 0,
    skipped: 0,
    outcomes: [],
    ...overrides,
  };
}

function reapStatus(overrides: Partial<ReapStatus> = {}): ReapStatus {
  return { ...idleReapStatus, ...overrides };
}

function outcome(overrides: Partial<RunOutcomeRead> = {}): RunOutcomeRead {
  return {
    media_key: "radarr:1:10",
    title: "Some Movie",
    kind: "radarr_delete",
    size_bytes: 2 * GB,
    state: "verified",
    error_reason: null,
    is_canary: false,
    file_removed: false,
    ...overrides,
  };
}

/** Answers `api.runOutcomes` off one full, oldest-first list, the same shape the real
 *  paged route serves: a page from `offset`, and `outcome_count` is the whole list's size
 *  regardless of how much of it that page covers. */
function mockOutcomes(items: RunOutcomeRead[]) {
  apiMock.runOutcomes.mockImplementation((_id: number, offset = 0, limit = 50) =>
    Promise.resolve({
      outcomes: items.slice(offset, offset + limit),
      outcome_count: items.length,
      offset,
    }),
  );
}

/** Answers `api.runs` off one full, newest-first list, paging it the way the real envelope
 *  route does: a page from `offset`, and `total` is the whole list's size regardless of how
 *  much of it that page covers. */
function mockHistory(rows: RunSummary[]) {
  apiMock.runs.mockImplementation((offset = 0, limit = 50) =>
    Promise.resolve({ runs: rows.slice(offset, offset + limit), total: rows.length }),
  );
}

function summary(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 30,
    state: "completed",
    approved_at: "2026-01-05T12:00:00+00:00",
    finished_at: "2026-01-05T12:05:00+00:00",
    aborted_reason: null,
    deleted_items: 44,
    deleted_bytes: 289 * GB,
    deleted_unmeasured: 0,
    skipped: 0,
    ...overrides,
  };
}

function renderPlan() {
  return renderWithProviders(
    <ReapPlan
      onGoToSecurity={() => {}}
      onGoToServices={() => {}}
      onGoToPlexSettings={() => {}}
      onGoToReview={() => {}}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // The persisted run ack: dropped between tests so a Done click in one cannot hide the
  // result a later test expects to see.
  window.localStorage.removeItem(ACK_KEY);
  apiMock.safety.mockResolvedValue({ destructive_enabled: true });
  apiMock.setupStatus.mockResolvedValue(READY_SETUP);
  apiMock.latestSnapshot.mockResolvedValue(DEFAULT_SNAPSHOT);
  apiMock.reapBreakdown.mockResolvedValue(breakdown());
  apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.runs.mockResolvedValue({ runs: [], total: 0 });
  // Only reached once a run exists to confirm (createRun -> ReapConfirm, or the standalone
  // practice run). Answered here so every test has a working default.
  apiMock.createRun.mockResolvedValue(run);
  apiMock.dryRun.mockResolvedValue(dryReport());
  apiMock.run.mockResolvedValue(run);
  apiMock.reapStatus.mockResolvedValue(idleReapStatus);
  apiMock.plexTrash.mockResolvedValue({
    configured: true,
    trashed: 0,
    sections_unreadable: 0,
    empties_after_scan: false,
  });
});

describe("idle", () => {
  it("has no accessibility violations", async () => {
    const { container } = renderPlan();
    await screen.findByText("titles");
    await expectNoA11yViolations(container);
  });

  it("renders four stat tiles and the help sentence, from the breakdown", async () => {
    renderPlan();

    const tiles = (await screen.findByText("titles")).closest(".fair-stat") as HTMLElement;
    expect(within(tiles).getByText("47")).toBeInTheDocument();

    const gbTile = screen.getByText("to free").closest(".fair-stat") as HTMLElement;
    expect(gbTile.textContent).toMatch(/470 GiB/);

    const moviesTile = screen.getByText("movies").closest(".fair-stat") as HTMLElement;
    expect(within(moviesTile).getByText("38")).toBeInTheDocument();

    const seasonsTile = screen.getByText("seasons").closest(".fair-stat") as HTMLElement;
    expect(within(seasonsTile).getByText("9")).toBeInTheDocument();

    const help = document.querySelector(".reap-summary-help") as HTMLElement;
    expect(help.textContent).toContain("See every title in Review.");
    expect(within(help).getByRole("button", { name: "Review" })).toBeInTheDocument();
    // Nothing with an unknown size on this fixture, so the held-back clause stays off.
    expect(help.textContent).not.toMatch(/held back/);
  });

  it("names how many are held back when the allowance holds them back", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      breakdown({ will_reap: 50, will_reap_unknown: 3, movies_unknown: 3 }),
    );
    renderPlan();
    expect(await screen.findByText(/3 titles with unknown size are held back/)).toBeInTheDocument();
    // The tiles subtract the held-back three: 50 - 3 = 47, never the raw 50.
    const tiles = (await screen.findByText("titles")).closest(".fair-stat") as HTMLElement;
    expect(within(tiles).getByText("47")).toBeInTheDocument();
  });

  it("says they are included, up to the allowance, once it admits them", async () => {
    apiMock.profile.mockResolvedValue({ ...DEFAULT_PROFILE, max_unmeasured_per_run: 10 });
    apiMock.reapBreakdown.mockResolvedValue(
      breakdown({ will_reap: 50, will_reap_unknown: 3, movies_unknown: 3 }),
    );
    renderPlan();
    expect(
      await screen.findByText(/3 titles with unknown size are included, up to your allowance/),
    ).toBeInTheDocument();
  });
});

describe("blocked", () => {
  it("renders only the failing checks, each with its fix link, and disables Reap while Practice stays enabled", async () => {
    const setup: SetupStatus = { ...READY_SETUP, plex_linked: false, reap_ready: false };
    apiMock.setupStatus.mockResolvedValue(setup);
    renderPlan();

    expect(
      await screen.findByText(/Reaper can't remove anything until Plex is connected/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Plex in Settings" })).toBeInTheDocument();
    // Password, Tautulli and Radarr/Sonarr all still check out on this fixture, so only Plex's
    // line renders.
    expect(screen.queryByText(/set a password/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Tautulli is connected/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Radarr or Sonarr is connected/i)).not.toBeInTheDocument();

    const reapButton = await screen.findByRole("button", { name: /^Reap 47 titles…$/ });
    expect(reapButton).toBeDisabled();
    const practiceButton = screen.getByRole("button", { name: "Practice run" });
    await waitFor(() => expect(practiceButton).toBeEnabled());
  });

  it("names every failing check when more than one fails", async () => {
    apiMock.setupStatus.mockResolvedValue({
      ...READY_SETUP,
      plex_linked: false,
      has_tautulli: false,
      scan_ready: false,
      reap_ready: false,
    });
    renderPlan();
    expect(await screen.findByText(/until Plex is connected/)).toBeInTheDocument();
    expect(screen.getByText(/until Tautulli is connected/)).toBeInTheDocument();
  });

  it("disables Reap while deletion is off, with nothing extra said here (the banner already says so)", async () => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    renderPlan();
    const reapButton = await screen.findByRole("button", { name: /^Reap 47 titles…$/ });
    await waitFor(() => expect(screen.getByText("titles")).toBeInTheDocument());
    expect(reapButton).toBeDisabled();
    expect(screen.queryByText(/deletion is off/i)).not.toBeInTheDocument();
  });
});

describe("an unread setup state", () => {
  it("is never shown as passed", async () => {
    apiMock.setupStatus.mockRejectedValue(new Error("nope"));
    renderPlan();

    expect(
      await screen.findByText(/couldn't check whether Plex and Tautulli are connected/i),
    ).toBeInTheDocument();
    // Not the same thing as a specific failing check: no fix-link list renders over an unread
    // state, since there is nothing yet to say failed.
    expect(
      screen.queryByRole("button", { name: "Connect Plex in Settings" }),
    ).not.toBeInTheDocument();
    const reapButton = await screen.findByRole("button", { name: /^Reap 47 titles…$/ });
    expect(reapButton).toBeDisabled();
  });
});

describe("the head Reap button", () => {
  it("builds a plan and opens the confirmation sheet", async () => {
    const user = userEvent.setup();
    renderPlan();

    const reapButton = await screen.findByRole("button", { name: /^Reap 47 titles…$/ });
    await waitFor(() => expect(reapButton).toBeEnabled());
    await user.click(reapButton);

    expect(apiMock.createRun).toHaveBeenCalledWith("all");
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(run.confirmation_phrase)).toBeInTheDocument();
  });

  it("closes on Escape, back to the plan", async () => {
    const user = userEvent.setup();
    renderPlan();

    const reapButton = await screen.findByRole("button", { name: /^Reap 47 titles…$/ });
    await waitFor(() => expect(reapButton).toBeEnabled());
    await user.click(reapButton);
    await screen.findByRole("dialog");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByRole("button", { name: /^Reap 47 titles…$/ })).toBeInTheDocument();
  });
});

describe("the standalone practice run", () => {
  it("shows a spinner line, then the pass result, and Dismiss clears it", async () => {
    // dryRun is held open on purpose: the default mock resolves on the next microtask, which
    // `await user.click` already flushes, so the mutation can settle before this test gets a
    // turn to look and the spinner window closes unobserved. Holding it open makes the pending
    // state something the test can actually land on, rather than reaching for fake timers.
    let resolveDryRun: (value: RunReport) => void = () => {};
    apiMock.dryRun.mockReturnValue(
      new Promise<RunReport>((resolve) => {
        resolveDryRun = resolve;
      }),
    );
    const user = userEvent.setup();
    renderPlan();

    const practiceButton = await screen.findByRole("button", { name: "Practice run" });
    await waitFor(() => expect(practiceButton).toBeEnabled());
    await user.click(practiceButton);

    expect(
      await screen.findByText(/walking every safety check without deleting anything/),
    ).toBeInTheDocument();

    await act(async () => {
      resolveDryRun(dryReport());
    });

    const pass = await screen.findByText(/Practice run passed\. Nothing was deleted\./);
    expect(pass.textContent).toContain("This run would remove 47 titles");
    expect(apiMock.createRun).toHaveBeenCalledWith("all");
    expect(apiMock.dryRun).toHaveBeenCalledWith(run.id);

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/Practice run passed/)).not.toBeInTheDocument();
  });

  it("names how many checks would keep titles, when any would", async () => {
    apiMock.dryRun.mockResolvedValue(dryReport({ skipped: 3 }));
    const user = userEvent.setup();
    renderPlan();

    const practiceButton = await screen.findByRole("button", { name: "Practice run" });
    await waitFor(() => expect(practiceButton).toBeEnabled());
    await user.click(practiceButton);
    const pass = await screen.findByText(/Practice run passed\. Nothing was deleted\./);
    expect(pass.textContent).toContain("3 would be kept by checks.");
  });

  it("reports a failed practice run in the shared error tone", async () => {
    apiMock.dryRun.mockRejectedValue(new Error("Radarr is unreachable."));
    const user = userEvent.setup();
    renderPlan();

    const practiceButton = await screen.findByRole("button", { name: "Practice run" });
    await waitFor(() => expect(practiceButton).toBeEnabled());
    await user.click(practiceButton);
    const failure = await screen.findByText(/The practice run failed/);
    expect(failure.closest(".notice")).toHaveClass("notice-error");
  });
});

describe("past reaps", () => {
  it("shows the persisted totals for a finished run", async () => {
    mockHistory([summary()]);
    renderPlan();
    expect(await screen.findByText("Run 30")).toBeInTheDocument();
    expect(screen.getByText(/289 GiB freed, 44 removed/)).toBeInTheDocument();
  });

  it("shows no numbers at all for a run still executing, never zeros, and pins it as a plain, non-clickable row", async () => {
    // GET /api/runs itself now leaves a still-PLANNED run out (`executed_only=true`), so this
    // page never filters the list it is handed; an "executing" row is the one state that
    // still has no persisted totals AND belongs on the list, so it is the one to prove both
    // halves on: no numbers, and no button (its live view is this page's own left column).
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: true, run_id: 31, phase: "reaping", total: 10 }),
    );
    mockOutcomes([]);
    mockHistory([
      summary({
        id: 31,
        state: "executing",
        finished_at: null,
        deleted_items: null,
        deleted_bytes: null,
        deleted_unmeasured: null,
        skipped: null,
      }),
    ]);
    renderPlan();
    const row = (await screen.findByText("Run 31")).closest(".reap-run") as HTMLElement;
    expect(row.textContent).not.toMatch(/freed|removed|\b0\b/);
    expect(row.textContent).toContain("running now");
    expect(row.tagName).toBe("DIV");
  });

  it("does not pin a run a crash left executing: no running-now claim, and its record still opens", async () => {
    // Nothing reconciles a run whose process died mid-flight, so its row stays stored as
    // executing forever. Only the run the status poll actually claims is pinned; this one
    // stays an ordinary row, or the record of what it removed is unreachable for good.
    const user = userEvent.setup();
    mockOutcomes([]);
    mockHistory([
      summary({
        id: 29,
        state: "executing",
        finished_at: null,
        deleted_items: null,
        deleted_bytes: null,
        deleted_unmeasured: null,
        skipped: null,
      }),
    ]);
    renderPlan();
    const row = (await screen.findByText("Run 29")).closest(".reap-run") as HTMLElement;
    expect(row.textContent).not.toContain("running now");
    expect(row.tagName).toBe("BUTTON");
    await user.click(row);
    expect(await screen.findByRole("dialog", { name: "Run 29" })).toBeInTheDocument();
  });

  it("shows the aborted reason instead of a freed/removed count", async () => {
    mockHistory([
      summary({
        id: 32,
        state: "aborted",
        deleted_items: 0,
        deleted_bytes: 0,
        aborted_reason: { k: "legacy", p: { text: "Over the size cap for one run." } },
      }),
    ]);
    renderPlan();
    const row = (await screen.findByText("Run 32")).closest(".reap-run") as HTMLElement;
    expect(row.textContent).toContain("Over the size cap for one run.");
  });

  it("says there is nothing yet, on a fresh install", async () => {
    apiMock.runs.mockResolvedValue({ runs: [], total: 0 });
    renderPlan();
    expect(await screen.findByText("No reaps yet.")).toBeInTheDocument();
  });
});

describe("reaping", () => {
  it("swaps to the reaping layout, every number the shared reap-status poll's own set", async () => {
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({
        running: true,
        run_id: 12,
        phase: "reaping",
        done: 3,
        total: 10,
        deleted_items: 2,
        deleted_bytes: 6 * GB,
        skipped: 1,
        title: "Some Movie",
      }),
    );
    mockOutcomes([
      outcome({ media_key: "a", title: "Movie A", state: "verified", size_bytes: 2 * GB }),
      outcome({
        media_key: "b",
        title: "Movie B",
        state: "skipped",
        size_bytes: null,
        error_reason: { k: "legacy", p: { text: "You spared this by hand." } },
      }),
    ]);
    renderPlan();

    // The idle head actions are gone; the graceful Stop replaces them.
    expect(await screen.findByRole("button", { name: "Stop, keep the rest" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap 47 titles…$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Practice run" })).not.toBeInTheDocument();

    expect(screen.getByText("freed so far").closest(".fair-stat")!.textContent).toContain(
      bytes(6 * GB),
    );
    // The removed tile is the true removal count, never the walk's own `done`, which also
    // counts vetoed and failed items.
    const removedTile = screen.getByText("removed").closest(".fair-stat") as HTMLElement;
    expect(within(removedTile).getByText("2")).toBeInTheDocument();
    const keptTile = screen.getByText("kept by checks").closest(".fair-stat") as HTMLElement;
    expect(within(keptTile).getByText("1")).toBeInTheDocument();
    expect(screen.getByText("Now removing: Some Movie")).toBeInTheDocument();

    await screen.findByText("Item status, 2 handled");
    expect(screen.getByText("Movie A")).toBeInTheDocument();
    expect(screen.getByText(", kept: You spared this by hand.")).toBeInTheDocument();
    expect(screen.getByText("You can leave this page. The reap keeps going.")).toBeInTheDocument();
  });

  it("carries an accessible value text on the progress bar", async () => {
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: true, run_id: 12, phase: "reaping", done: 3, total: 10 }),
    );
    mockOutcomes([]);
    renderPlan();

    const bar = await screen.findByRole("progressbar", { name: "Reaping" });
    expect(bar).toHaveAttribute("aria-valuetext", "3 of 10 handled");
  });

  it("does not count a vetoed item twice, so the bar cannot finish while files are still going", async () => {
    // The executor's `done` already counts every walked item, vetoed ones included, and
    // `skipped` counts the vetoes again. Adding the two pinned the bar at 100% with
    // "Finishing up" while deletes were still being sent.
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({
        running: true,
        run_id: 12,
        phase: "reaping",
        done: 8,
        total: 10,
        skipped: 2,
        title: "Movie J",
      }),
    );
    mockOutcomes([]);
    renderPlan();

    const bar = await screen.findByRole("progressbar", { name: "Reaping" });
    expect(bar).toHaveAttribute("aria-valuenow", "80");
    expect(screen.getByText("Now removing: Movie J")).toBeInTheDocument();
    expect(screen.queryByText(/Finishing up/)).not.toBeInTheDocument();
  });

  it("says it is finishing up once every item is handled but the run keeps going", async () => {
    // The last item is deleted, but the run stays alive while Plex settles and the trash purge
    // runs, which can be several seconds. The card says so rather than sitting on a stale "Now
    // removing" line that reads as a hang.
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({
        running: true,
        run_id: 12,
        phase: "reaping",
        done: 10,
        total: 10,
        title: "Some Movie",
      }),
    );
    mockOutcomes([]);
    renderPlan();

    expect(await screen.findByText(/Finishing up/)).toBeInTheDocument();
    expect(screen.queryByText(/Now removing/)).not.toBeInTheDocument();
  });

  it("Stop asks the server to halt the run, gracefully, and disables while it is in flight", async () => {
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: true, run_id: 12, phase: "reaping", total: 10 }),
    );
    mockOutcomes([]);
    let resolveStop: (v: ReapStatus) => void = () => {};
    apiMock.stopRun.mockReturnValue(
      new Promise<ReapStatus>((resolve) => {
        resolveStop = resolve;
      }),
    );
    const user = userEvent.setup();
    renderPlan();

    const stopButton = await screen.findByRole("button", { name: "Stop, keep the rest" });
    await user.click(stopButton);
    expect(apiMock.stopRun).toHaveBeenCalledWith(12);
    expect(stopButton).toBeDisabled();

    await act(async () => {
      resolveStop(reapStatus({ running: true, run_id: 12, stopping: true }));
    });
  });
});

describe("done", () => {
  it("shows the result read back from what the run persisted, never the in-memory report, with Done back to idle", async () => {
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: false, run_id: 12, phase: "complete" }),
    );
    mockHistory([
      summary({
        id: 12,
        state: "completed",
        deleted_items: 44,
        deleted_bytes: 289 * GB,
        skipped: 1,
      }),
    ]);
    mockOutcomes([
      outcome({ media_key: "a", title: "Movie A", state: "verified", size_bytes: 2 * GB }),
      outcome({
        media_key: "b",
        title: "Movie B",
        state: "skipped",
        size_bytes: null,
        error_reason: { k: "legacy", p: { text: "You spared this by hand." } },
      }),
    ]);
    const user = userEvent.setup();
    renderPlan();

    expect(await screen.findByText("Reap finished")).toBeInTheDocument();
    expect(screen.getByText("freed").closest(".fair-stat")!.textContent).toContain(bytes(289 * GB));
    const removedTile = screen.getByText("removed").closest(".fair-stat") as HTMLElement;
    expect(within(removedTile).getByText(count(44))).toBeInTheDocument();
    const keptTile = screen.getByText("kept by checks").closest(".fair-stat") as HTMLElement;
    expect(within(keptTile).getByText(count(1))).toBeInTheDocument();

    expect(await screen.findByText("Kept by checks")).toBeInTheDocument();
    expect(screen.getByText(", kept: You spared this by hand.")).toBeInTheDocument();

    // The head no longer offers Reap or Practice while the result is showing.
    expect(screen.queryByRole("button", { name: /^Reap 47 titles…$/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByRole("button", { name: /^Reap 47 titles…$/ })).toBeInTheDocument();
    expect(screen.queryByText("Reap finished")).not.toBeInTheDocument();
  });

  it("files a failed item under its own list, and one whose file is gone reads removed, never kept", async () => {
    // A FAILED step is not a check that kept the file: filing it under "Kept by checks"
    // claims a protection fired when nothing was checked, and when the delete landed before
    // the failure (the journal's file_removed stamp), it tells the operator a file that is
    // off disk still exists.
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: false, run_id: 12, phase: "complete" }),
    );
    mockHistory([
      summary({ id: 12, state: "completed", deleted_items: 2, deleted_bytes: GB, skipped: 0 }),
    ]);
    mockOutcomes([
      outcome({
        media_key: "a",
        title: "Movie A",
        state: "failed",
        file_removed: true,
        error_reason: { k: "legacy", p: { text: "The import exclusion was not confirmed." } },
      }),
      outcome({
        media_key: "b",
        title: "Movie B",
        state: "failed",
        file_removed: false,
        error_reason: { k: "legacy", p: { text: "Radarr did not respond." } },
      }),
    ]);
    renderPlan();

    expect(await screen.findByText("Needs a look")).toBeInTheDocument();
    expect(screen.queryByText("Kept by checks")).not.toBeInTheDocument();
    expect(
      screen.getByText(", removed. The import exclusion was not confirmed."),
    ).toBeInTheDocument();
    expect(screen.getByText(", failed: Radarr did not respond.")).toBeInTheDocument();
    expect(screen.queryByText(/, kept:/)).not.toBeInTheDocument();
  });

  it("keeps a dismissed result dismissed across a reload", async () => {
    // The status poll reports the last run forever, so without a persisted ack every
    // refresh resurrected the result the operator already pressed Done on.
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: false, run_id: 12, phase: "complete" }),
    );
    mockHistory([
      summary({ id: 12, state: "completed", deleted_items: 7, deleted_bytes: GB, skipped: 0 }),
    ]);
    mockOutcomes([]);
    const user = userEvent.setup();
    const { unmount } = renderPlan();

    await user.click(await screen.findByRole("button", { name: "Done" }));
    expect(screen.queryByText("Reap finished")).not.toBeInTheDocument();

    // A fresh mount stands in for the page reload that used to bring the card back.
    unmount();
    renderPlan();
    expect(await screen.findByRole("button", { name: /^Reap 47 titles…$/ })).toBeInTheDocument();
    expect(screen.queryByText("Reap finished")).not.toBeInTheDocument();
  });

  it("shows the truncated stop note on the done card, not the detail sheet's below tail", async () => {
    // The operator-stop note is the truncated head here, because the done card has no item list
    // beneath it to point at. The "titles below" tail belongs to the detail sheet alone.
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: false, run_id: 13, phase: "aborted" }),
    );
    mockHistory([
      summary({
        id: 13,
        state: "aborted",
        deleted_items: 5,
        deleted_bytes: 10 * GB,
        skipped: 0,
        aborted_reason: { k: "error.reap.stopped_by_operator", p: {} },
      }),
    ]);
    mockOutcomes([]);
    renderPlan();

    expect(await screen.findByText("Reap finished")).toBeInTheDocument();
    expect(screen.getByText("You stopped this run…")).toBeInTheDocument();
    expect(screen.queryByText(/titles below/)).not.toBeInTheDocument();
  });

  it("does not show the done card for a run refused before it ever left PLANNED (phase error)", async () => {
    // A changed manifest or policy refuses before the claim, so the run's own row never
    // leaves PLANNED, `executed_only` leaves it out of the history list entirely, and there
    // is nothing this card could show. The page falls back to idle rather than a permanent
    // "Loading…" over a run it can never find.
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({
        running: false,
        run_id: 14,
        phase: "error",
        error_reason: { k: "legacy", p: { text: "The plan changed." } },
      }),
    );
    renderPlan();

    await screen.findByRole("button", { name: /^Reap 47 titles…$/ });
    expect(screen.queryByText("Reap finished")).not.toBeInTheDocument();
  });
});

describe("reload", () => {
  it("reload mid-run lands on the reaping layout purely from the poll, no local state needed", async () => {
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: true, run_id: 20, phase: "reaping", done: 1, total: 5 }),
    );
    mockOutcomes([]);
    renderPlan();

    expect(await screen.findByRole("button", { name: "Stop, keep the rest" })).toBeInTheDocument();
  });

  it("reload after a run finished shows the done card immediately, from the same poll ReapBar reads", async () => {
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: false, run_id: 21, phase: "complete" }),
    );
    mockHistory([summary({ id: 21, deleted_items: 9, deleted_bytes: 4 * GB, skipped: 0 })]);
    mockOutcomes([]);
    renderPlan();

    expect(await screen.findByText("Reap finished")).toBeInTheDocument();
  });
});

describe("history paging", () => {
  function manyRuns(n: number): RunSummary[] {
    return Array.from({ length: n }, (_, i) => summary({ id: 200 - i }));
  }

  it("Show 50 more pages the whole history via offset, and the count updates", async () => {
    mockHistory(manyRuns(60));
    const user = userEvent.setup();
    renderPlan();

    expect(await screen.findByText(`Showing ${count(50)} of ${count(60)}`)).toBeInTheDocument();
    const more = screen.getByRole("button", { name: "Show 50 more" });
    expect(more).toBeEnabled();

    await user.click(more);

    await waitFor(() => expect(apiMock.runs).toHaveBeenCalledWith(50, 50, true));
    expect(await screen.findByText(`Showing ${count(60)} of ${count(60)}`)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show 50 more" })).toBeDisabled();
  });
});

describe("run detail sheet", () => {
  it("opens a read-only detail with the persisted totals and outcomes, and closes on Escape and the scrim", async () => {
    mockHistory([summary({ id: 55, deleted_items: 8, deleted_bytes: 3 * GB, skipped: 2 })]);
    mockOutcomes([
      outcome({ media_key: "a", title: "Movie A", state: "verified", size_bytes: 3 * GB }),
      outcome({
        media_key: "b",
        title: "Movie B",
        state: "skipped",
        size_bytes: null,
        error_reason: { k: "legacy", p: { text: "You spared this by hand." } },
      }),
    ]);
    const user = userEvent.setup();
    const { container } = renderPlan();

    await user.click(await screen.findByRole("button", { name: /^Run 55/ }));
    const dialog = await screen.findByRole("dialog", { name: "Run 55" });
    expect(within(dialog).getByText("Item status")).toBeInTheDocument();
    expect(within(dialog).getByText("Movie A")).toBeInTheDocument();
    expect(within(dialog).getByText(", kept: You spared this by hand.")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(await screen.findByRole("button", { name: /^Run 55/ }));
    await screen.findByRole("dialog", { name: "Run 55" });
    await user.click(container.querySelector(".modal-scrim")!);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("gives the operator-stop note its full 'titles below' tail here, where the list is beneath it", async () => {
    mockHistory([
      summary({
        id: 60,
        state: "aborted",
        deleted_items: 3,
        deleted_bytes: 5 * GB,
        skipped: 0,
        aborted_reason: { k: "error.reap.stopped_by_operator", p: {} },
      }),
    ]);
    mockOutcomes([
      outcome({ media_key: "a", title: "Movie A", state: "verified", size_bytes: 5 * GB }),
    ]);
    const user = userEvent.setup();
    renderPlan();

    await user.click(await screen.findByRole("button", { name: /^Run 60/ }));
    const dialog = await screen.findByRole("dialog", { name: "Run 60" });
    expect(
      within(dialog).getByText(
        "You stopped this run. The titles below were the only ones removed.",
      ),
    ).toBeInTheDocument();
  });

  it("the pinned running-now row does not open a detail", async () => {
    // Pinned means the status poll claims this exact run; a stored-executing row the poll
    // does not claim stays openable (the crashed-run test above).
    apiMock.reapStatus.mockResolvedValue(
      reapStatus({ running: true, run_id: 56, phase: "reaping", total: 10 }),
    );
    mockOutcomes([]);
    mockHistory([
      summary({
        id: 56,
        state: "executing",
        finished_at: null,
        deleted_items: null,
        deleted_bytes: null,
        deleted_unmeasured: null,
        skipped: null,
      }),
    ]);
    renderPlan();

    await screen.findByText("Run 56");
    expect(screen.queryByRole("button", { name: /^Run 56/ })).not.toBeInTheDocument();
  });
});
