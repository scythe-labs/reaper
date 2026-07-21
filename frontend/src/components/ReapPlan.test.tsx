// SPDX-License-Identifier: AGPL-3.0-or-later
// A plan is frozen against the scan it was built from. If a newer scan has landed since, the
// plan can list titles that scan would now protect, so the warning has to sit in the summary
// that carries Execute, next to the button that rebuilds it. These pin that it is there, and
// that "we could not check" is said out loud rather than rendering nothing.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Run, Snapshot } from "../api";
import { ReapPlan } from "./ReapPlan";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    safety: vi.fn(),
    createRun: vi.fn(),
    dryRun: vi.fn(),
    runs: vi.fn(),
    latestSnapshot: vi.fn(),
    reapBreakdown: vi.fn(),
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
    apiMock.runs.mockResolvedValue([run]);
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
