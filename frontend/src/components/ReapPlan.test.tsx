// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The Reap tab's idle and blocked states: the summary card's tiles, help sentence and
// blockers notice, the head actions, the standalone practice run, and the history card.
// Confirmation and execution stay ReapConfirm.tsx's own tests; this file proves that pressing
// the head Reap button hands it a freshly built run and opens it.
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReapBreakdown, ReapStatus, Run, RunReport, RunSummary, SetupStatus } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { DEFAULT_PROFILE, DEFAULT_SNAPSHOT, IDLE_SCAN, READY_SETUP } from "../test/apiFixtures";
import { renderWithProviders } from "../test/renderWithProviders";
import { ReapPlan } from "./ReapPlan";

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
  report: null,
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
  apiMock.safety.mockResolvedValue({ destructive_enabled: true });
  apiMock.setupStatus.mockResolvedValue(READY_SETUP);
  apiMock.latestSnapshot.mockResolvedValue(DEFAULT_SNAPSHOT);
  apiMock.reapBreakdown.mockResolvedValue(breakdown());
  apiMock.profile.mockResolvedValue(DEFAULT_PROFILE);
  apiMock.scanStatus.mockResolvedValue(IDLE_SCAN);
  apiMock.runs.mockResolvedValue([]);
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

  it("shows the persisted totals for a finished run", async () => {
    apiMock.runs.mockResolvedValue([summary()]);
    renderPlan();
    expect(await screen.findByText("Run 30")).toBeInTheDocument();
    expect(screen.getByText(/289 GiB freed, 44 removed/)).toBeInTheDocument();
  });

  it("shows no numbers at all for a run with nothing persisted yet, never zeros", async () => {
    // "executing", not "planned": a still-running run is a real past-reaps row (the condemn
    // dot), where a plan nobody ever executed is filtered out entirely (below). Both carry
    // null totals, and this is the state that still renders one.
    apiMock.runs.mockResolvedValue([
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
  });

  it("hides a plan that was built and never executed", async () => {
    // Every "Reap N titles…" press and every standalone practice run creates a run row
    // (POST /api/runs) before anything is confirmed or sent, so the list would otherwise fill
    // with plans nobody ever acted on. A "planned" run is not a past reap.
    apiMock.runs.mockResolvedValue([
      summary({
        id: 40,
        state: "planned",
        finished_at: null,
        deleted_items: null,
        deleted_bytes: null,
        deleted_unmeasured: null,
        skipped: null,
      }),
      summary({ id: 30 }),
    ]);
    renderPlan();
    expect(await screen.findByText("Run 30")).toBeInTheDocument();
    expect(screen.queryByText("Run 40")).not.toBeInTheDocument();
  });

  it("shows the empty state when only planned runs exist", async () => {
    apiMock.runs.mockResolvedValue([
      summary({
        id: 41,
        state: "planned",
        finished_at: null,
        deleted_items: null,
        deleted_bytes: null,
        deleted_unmeasured: null,
        skipped: null,
      }),
    ]);
    renderPlan();
    expect(await screen.findByText("No reaps yet.")).toBeInTheDocument();
    expect(screen.queryByText("Run 41")).not.toBeInTheDocument();
  });

  it("shows the aborted reason instead of a freed/removed count", async () => {
    apiMock.runs.mockResolvedValue([
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
    apiMock.runs.mockResolvedValue([]);
    renderPlan();
    expect(await screen.findByText("No reaps yet.")).toBeInTheDocument();
  });
});
