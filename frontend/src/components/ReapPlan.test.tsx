// SPDX-License-Identifier: AGPL-3.0-or-later
// A plan is frozen against the scan it was built from. If a newer scan has landed since, the
// plan can list titles that scan would now protect, so the warning has to sit in the summary
// that carries Execute, next to the button that rebuilds it. These pin that it is there, and
// that "we could not check" is said out loud rather than rendering nothing.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Run, Snapshot } from "../api";
import { ReapPlan } from "./ReapPlan";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    safety: vi.fn(),
    createRun: vi.fn(),
    run: vi.fn(),
    dryRun: vi.fn(),
    runs: vi.fn(),
    latestSnapshot: vi.fn(),
    reapBreakdown: vi.fn(),
    plexTrash: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: apiMock,
}));

const run = {
  id: 3,
  snapshot_id: 1,
  policy_hash: "p",
  state: "planned",
  item_count: 2,
  total_bytes: 1024 ** 3,
  held_back_unknown_size: 0,
  confirmation_phrase: "REAP 2 ITEMS 1 GB",
  approved_manifest_hash: "m",
  approved_by: "owner",
  approved_at: "2026-01-01T00:00:00+00:00",
  steps: [],
} as Run;

const snapshot: Snapshot = {
  id: 2,
  created_at: "2026-01-02T00:00:00+00:00",
  policy_hash: "p",
  horizon_at: "2025-01-01T00:00:00+00:00",
  item_count: 10,
  degraded: false,
  degraded_reason: null,
  condemned: 2,
  protected: 3,
  abstained: 5,
  unknown_size_items: 0,
  reclaimable_bytes: 0,
};

async function buildPlan() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const { container } = render(
    <QueryClientProvider client={queryClient}>
      <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />
    </QueryClientProvider>,
  );
  const person = userEvent.setup();
  await person.click(screen.getByRole("button", { name: /build a plan/i }));
  const summary = await screen.findByText(run.confirmation_phrase);
  return { person, container, summary: summary.closest(".plan-summary") as HTMLElement };
}

describe("ReapPlan staleness", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    apiMock.createRun.mockResolvedValue(run);
    apiMock.run.mockResolvedValue(run);
    apiMock.runs.mockResolvedValue([run]);
    // An empty, readable trash, so the Plex-trash warning stays out of the way of
    // every test that is about something else.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    apiMock.reapBreakdown.mockResolvedValue({
      has_snapshot: true,
      policy_condemned: 2,
      policy_condemned_bytes: 1024 ** 3,
      policy_condemned_unknown: 0,
      hand_spared: 0,
      hand_reaped: 0,
      hand_reaped_bytes: 0,
      hand_reaped_unknown: 0,
      will_reap: 2,
      will_reap_bytes: 1024 ** 3,
      will_reap_unknown: 0,
      movies: 2,
      seasons: 0,
      condemned_by: [],
    });
    apiMock.latestSnapshot.mockResolvedValue({ ...snapshot, id: run.snapshot_id });
  });

  it("says nothing when the plan came from the newest scan", async () => {
    const { summary } = await buildPlan();
    expect(within(summary).queryByText(/older scan/i)).not.toBeInTheDocument();
  });

  it("warns beside Execute, with the rebuild, when a newer scan has landed", async () => {
    apiMock.latestSnapshot.mockResolvedValue(snapshot);
    const { summary } = await buildPlan();
    expect(await within(summary).findByText(/came from an older scan/i)).toBeInTheDocument();
    expect(within(summary).getByRole("button", { name: /build a new plan/i })).toBeInTheDocument();
  });

  it("says it could not check when the scan can't be read", async () => {
    apiMock.latestSnapshot.mockRejectedValue(new Error("boom"));
    const { summary } = await buildPlan();
    expect(await within(summary).findByText(/couldn't check/i)).toBeInTheDocument();
  });

  it("leaves the staleness wording out of the history list", async () => {
    apiMock.latestSnapshot.mockResolvedValue(snapshot);
    const { container } = await buildPlan();
    const history = container.querySelector(".run-history") as HTMLElement;
    expect(within(history).queryByText(/older scan/i)).not.toBeInTheDocument();
  });
});

describe("the plan the page is showing", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: true });
    apiMock.createRun.mockResolvedValue(run);
    apiMock.run.mockResolvedValue(run);
    apiMock.runs.mockResolvedValue([run]);
    // An empty, readable trash, so the Plex-trash warning stays out of the way of
    // every test that is about something else.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: false });
    apiMock.latestSnapshot.mockResolvedValue({ ...snapshot, id: run.snapshot_id });
  });

  it("stops offering Execute once the run it shows has been spent", async () => {
    // The plan is read through the cache, not captured: a run that has since completed
    // must not keep a live Execute over it (B-15).
    apiMock.run.mockResolvedValue({ ...run, state: "completed" });
    const { summary } = await buildPlan();
    await waitFor(() =>
      expect(within(summary).queryByRole("button", { name: /execute/i })).not.toBeInTheDocument(),
    );
  });

  it("says so when the plan behind a history row can't be loaded", async () => {
    // Everything about a plan -- the phrase, the count, Execute, the steps -- hangs off this
    // one query, so a failed fetch used to unmount all of it with no message and no retry, and
    // clicking a history row simply looked like it did nothing (rule 36).
    apiMock.run.mockRejectedValue(new Error("the server dropped it"));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />
      </QueryClientProvider>,
    );
    const person = userEvent.setup();
    await person.click(await screen.findByRole("button", { name: `#${run.id}` }));

    expect(await screen.findByText(/couldn't load this plan/i)).toBeInTheDocument();
  });

  it("won't offer to build a plan from a scan that came back incomplete", async () => {
    // The planner refuses a degraded snapshot outright, so the page says so up front rather
    // than trading the click for a 422 (PR-8).
    apiMock.latestSnapshot.mockResolvedValue({
      ...snapshot,
      id: run.snapshot_id,
      degraded: true,
      degraded_reason: "A source didn't answer.",
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ReapPlan onGoToDeletion={() => {}} onGoToPlexSettings={() => {}} onGoToReview={() => {}} />
      </QueryClientProvider>,
    );
    expect(await screen.findByText(/came back incomplete/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /build a plan/i })).toBeDisabled();
  });
});

describe("what a practice run reports", () => {
  beforeEach(() => {
    apiMock.safety.mockResolvedValue({ destructive_enabled: false });
    apiMock.createRun.mockResolvedValue(run);
    apiMock.run.mockResolvedValue(run);
    apiMock.runs.mockResolvedValue([run]);
    // An empty, readable trash, so the Plex-trash warning stays out of the way of
    // every test that is about something else.
    apiMock.plexTrash.mockResolvedValue({
      configured: true,
      trashed: 0,
      sections_unreadable: 0,
      empties_after_scan: false,
    });
    apiMock.reapBreakdown.mockResolvedValue({ has_snapshot: false });
    apiMock.latestSnapshot.mockResolvedValue({ ...snapshot, id: run.snapshot_id });
  });

  const report = (patch: Record<string, unknown> = {}) => ({
    run_id: run.id,
    dry_run: true,
    state: "completed",
    aborted_reason: null,
    would_delete_items: 0,
    deleted_bytes: 0,
    deleted_unmeasured: 0,
    skipped: 0,
    outcomes: [
      { media_key: "a", kind: "movie", state: "verified", detail: "one", title: "", checks: [] },
      { media_key: "b", kind: "movie", state: "verified", detail: "two", title: "", checks: [] },
    ],
    ...patch,
  });

  it("says what was walked, and never leads with a zero it fixes by construction", async () => {
    // The old summary led with "0 souls were actually reaped", which is zero for every
    // practice run there has ever been and so proves nothing, then called the per-item
    // outcomes "steps" -- a count that disagreed with the journalled steps below it (I-1).
    apiMock.dryRun.mockResolvedValue(report());
    const { person } = await buildPlan();

    await person.click(screen.getByRole("button", { name: "Practice run" }));

    const line = await screen.findByText(/Practice run complete/);
    expect(line.textContent).toContain("2 souls were walked end to end");
    expect(line.textContent).toContain("nothing was sent");
    expect(line.textContent).not.toContain("steps");
    expect(line.textContent).not.toContain("0 souls");
  });

  it("names the ones a check would hold back rather than counting them as walked", async () => {
    apiMock.dryRun.mockResolvedValue(report({ skipped: 1 }));
    const { person } = await buildPlan();

    await person.click(screen.getByRole("button", { name: "Practice run" }));

    const line = await screen.findByText(/Practice run complete/);
    expect(line.textContent).toContain("2 souls were walked");
    expect(line.textContent).toContain("1 of them would be skipped");
    expect(line.textContent).not.toContain("end to end");
  });
});
