// SPDX-License-Identifier: AGPL-3.0-or-later
// The policy page's two dead ends, and its control grammar.
//
// Both dead ends were states the operator could not get out of from the page that exists
// to fix them: a policy that could not be read showed no way to replace it, and a preset
// click left the removal lane over budget with Save disabled. Each test here fails if
// either fix is reverted.
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CustomCondemn, Policy, PolicyBody, ProfileSettings } from "../api";
import { PolicyEditor } from "./PolicyEditor";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    policy: vi.fn(),
    profile: vi.fn(),
    safety: vi.fn(),
    scanStatus: vi.fn(),
    seasonShape: vi.fn(),
    simulate: vi.fn(),
    validatePolicy: vi.fn(),
    vocabulary: vi.fn(),
    vocabularyValues: vi.fn(),
    savePolicy: vi.fn(),
    saveProfile: vi.fn(),
    setDeletion: vi.fn(),
    startScan: vi.fn(),
  },
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, api: apiMock };
});

function body(custom: CustomCondemn[] = []): PolicyBody {
  // A saved body is always on budget: the built-ins plus the operator's own rules total
  // exactly 100, which is what the server enforces.
  const builtIn = 100 - custom.reduce((sum, c) => sum + c.weight, 0);
  return {
    name: "default",
    media_type: "movie",
    condemn_at: 70,
    coverage_floor_bp: 5000,
    keep_last_seasons: 1,
    keep_first_season: false,
    keep_last_scope: "all",
    season_lookahead: 0,
    keep_in_progress: true,
    in_progress_hold_days: 30,
    keep_specials: true,
    flag_keep_conflicts: false,
    gates: [],
    signals: [
      { signal: "unwatched", weight: Math.round(builtIn * 0.7), saturate_at: 365, floor: 0 },
      { signal: "few_watchers", weight: Math.round(builtIn * 0.2), saturate_at: 3, floor: 0 },
      {
        signal: "low_rating",
        weight: builtIn - Math.round(builtIn * 0.7) - Math.round(builtIn * 0.2),
        saturate_at: 70,
        floor: 0,
      },
    ],
    protect_conditions: [],
    custom_condemn: custom,
    graded_keeps: [],
    keep_tags: [],
    keep_tags_match: "any",
    keep_rating_rules: [],
    keep_rating_match: "any",
  };
}

const pace: ProfileSettings = {
  max_items_per_run: 10,
  max_bytes_per_run: 500_000_000_000,
  max_items_per_30d: 100,
  max_bytes_per_30d: 2_000_000_000_000,
  grace_days: 14,
  require_approval: true,
  max_unmeasured_per_run: 0,
};

function renderEditor(policy: Partial<Policy> & { body: PolicyBody }) {
  apiMock.policy.mockResolvedValue({
    policy_hash: "hash",
    name: "default",
    warnings: [],
    ...policy,
  });
  apiMock.profile.mockResolvedValue(pace);
  apiMock.safety.mockResolvedValue({
    destructive_enabled: false,
    has_password: true,
    note: null,
  });
  apiMock.scanStatus.mockResolvedValue({
    running: false,
    phase: "idle",
    done: 0,
    total: 0,
    percent: 0,
    detail: "",
    error: null,
    snapshot_id: null,
    followup_queued: false,
  });
  apiMock.seasonShape.mockResolvedValue({ total_shows: 0, season_counts: {} });
  apiMock.vocabulary.mockResolvedValue({ lane: "condemn", fields: [] });
  apiMock.vocabularyValues.mockResolvedValue({ field: "", values: [] });
  apiMock.validatePolicy.mockResolvedValue({
    policy_hash: "hash",
    name: "default",
    body: policy.body,
    warnings: [],
  });
  apiMock.simulate.mockResolvedValue({
    exact: true,
    stale_reason: null,
    condemned: 0,
    protected: 0,
    abstained: 0,
    reclaimable_bytes: 0,
    newly_condemned: 0,
    no_longer_condemned: 0,
    histogram: [],
    examples_newly_condemned: [],
    protected_by: [],
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <PolicyEditor />
    </QueryClientProvider>,
  );
}

describe("a policy that couldn't be read", () => {
  it("says so on the load it happened, with nothing else dirty", async () => {
    // fell_back and needs_save are mutually exclusive on the server: a body that could
    // not be read at all never carries needs_save. The notice used to live inside the
    // savebar, which only renders when something is dirty, so it was invisible in
    // exactly the state it explains.
    const { container } = renderEditor({ body: body(), needs_save: false, fell_back: true });

    expect(
      await screen.findByText(/Your saved policy couldn't be read/),
    ).toBeInTheDocument();
    // And the way out is offered: the savebar renders, so the fallback can be replaced.
    const savebar = container.querySelector(".savebar");
    expect(savebar).not.toBeNull();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    // The notice itself is not inside that savebar: it hangs off the response flag alone,
    // so no dirty gate can hide it.
    expect(savebar?.textContent ?? "").not.toContain("couldn't be read");
  });

  it("stays quiet on an ordinary load", async () => {
    renderEditor({ body: body() });

    await screen.findByText("Policy");
    expect(screen.queryByText(/Your saved policy couldn't be read/)).not.toBeInTheDocument();
  });
});

describe("a preset", () => {
  it("fits the operator's own rules into the 100 points instead of overshooting", async () => {
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    const mine: CustomCondemn = {
      kind: "boolean",
      name: "My rule",
      field: "requested",
      op: "eq",
      value: false,
      weight: 15,
    };
    const { container } = renderEditor({ body: body([mine]) });

    await user.click(await screen.findByRole("button", { name: "Cautious" }));

    await waitFor(() =>
      expect(container.querySelector(".budget-line")?.textContent).toContain(
        "100 of 100 removal points used",
      ),
    );
    // The rule survives, scaled, rather than being dropped to make room.
    expect(container.querySelector(".budget-line")?.textContent).toContain("yours");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    expect(screen.queryByText(/before saving/)).not.toBeInTheDocument();
  });
});
