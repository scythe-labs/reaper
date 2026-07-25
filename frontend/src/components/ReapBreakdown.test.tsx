// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The reap breakdown: the ledger (policy verdict, hand changes, the net), the by-reason
// bars, the empty/error/no-scan states, and the two pointers off the page.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReapBreakdown as Breakdown } from "../api";
import { ReapBreakdown } from "./ReapBreakdown";

const { apiMock } = vi.hoisted(() => ({ apiMock: { reapBreakdown: vi.fn(), profile: vi.fn() } }));
vi.mock("../api", () => ({ api: apiMock }));

const GB = 1024 ** 3;

// The component consults the profile (via useHoldsBackUnmeasured) to know whether the planner
// holds unmeasured items back. Default: allowance 0, so it does (the common case).
function profileWith(maxUnmeasured: number) {
  return {
    max_items_per_run: 10,
    max_bytes_per_run: 1,
    max_items_per_30d: 100,
    max_bytes_per_30d: 1,
    caps_enabled: true,
    grace_days: 14,
    max_unmeasured_per_run: maxUnmeasured,
  };
}

function full(overrides: Partial<Breakdown> = {}): Breakdown {
  return {
    has_snapshot: true,
    policy_condemned: 543,
    policy_condemned_bytes: 4400 * GB,
    policy_condemned_unknown: 0,
    hand_spared: 12,
    hand_reaped: 38,
    hand_reaped_bytes: 300 * GB,
    hand_reaped_unknown: 0,
    hand_reaped_held: 0,
    will_reap: 569,
    will_reap_bytes: 4500 * GB,
    will_reap_unknown: 0,
    movies: 402,
    movies_unknown: 0,
    seasons: 167,
    seasons_unknown: 0,
    condemned_by: [
      { id: "unwatched", count: 521, bytes: 0, unknown_size: 0 },
      { id: "low_rating", count: 201, bytes: 0, unknown_size: 0 },
    ],
    ...overrides,
  };
}

function renderBreakdown(onPlex = () => {}, onReview = () => {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReapBreakdown onGoToPlexSettings={onPlex} onGoToReview={onReview} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  apiMock.reapBreakdown.mockResolvedValue(full());
  apiMock.profile.mockResolvedValue(profileWith(0));
});

describe("the ledger", () => {
  it("shows the policy verdict, the hand changes, and the net", async () => {
    renderBreakdown();
    expect(await screen.findByText("Condemned by your policy")).toBeInTheDocument();
    expect(screen.getByText("You spared by hand")).toBeInTheDocument();
    expect(screen.getByText("You marked to reap by hand")).toBeInTheDocument();
    expect(screen.getByText("Will be reaped")).toBeInTheDocument();
    expect(screen.getByText(/402 movies · 167 TV seasons/)).toBeInTheDocument();
  });

  it("collapses to just the net when there are no hand changes", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ hand_spared: 0, hand_reaped: 0, will_reap: 543, policy_condemned: 543 }),
    );
    renderBreakdown();
    expect(await screen.findByText("Will be reaped")).toBeInTheDocument();
    // The funnel rows only appear once a hand change has moved the number.
    expect(screen.queryByText("Condemned by your policy")).not.toBeInTheDocument();
    expect(screen.queryByText("You spared by hand")).not.toBeInTheDocument();
  });

  it("names how many can't be measured and won't be removed", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    renderBreakdown();
    expect(await screen.findByText(/4 titles can't be measured, so Reaper won't remove/)).toBeInTheDocument();
    // With the allowance off the planner drops those 4, so the headline and total count only
    // 565, and the raw 569 never appears (B-8).
    expect(screen.getAllByText("565").length).toBeGreaterThan(0);
    expect(screen.queryByText("569")).not.toBeInTheDocument();
    // And the split subtracts the same four, so the page states ONE number for one reap:
    // 399 + 166 = 565, never the raw 402 + 167 (B-5, rule 30).
    expect(screen.getByText(/399 movies · 166 TV seasons/)).toBeInTheDocument();
  });

  it("rewords the unmeasured line when the allowance admits them", async () => {
    apiMock.profile.mockResolvedValue(profileWith(10)); // allowance on
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    renderBreakdown();
    // Allowance on: the planner admits the unmeasured, so the line must not promise they are
    // kept, and the count is not reduced (B-8).
    expect(
      await screen.findByText(/only removed within your unknown-size allowance/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/won't remove them/)).not.toBeInTheDocument();
    // Not reduced: the unmeasured stay in the count when the allowance admits them -- and in
    // the split, which follows the same set.
    expect(screen.getAllByText("569").length).toBeGreaterThan(0);
    expect(screen.getByText(/402 movies · 167 TV seasons/)).toBeInTheDocument();
  });

  it("says a reap removes nothing when every condemned title is unmeasured", async () => {
    // The old empty-state test was `will_reap === 0`, which renders a full ledger totaling
    // zero when the whole list is held back for want of a size (B-5).
    apiMock.reapBreakdown.mockResolvedValue(
      full({
        will_reap: 4,
        will_reap_unknown: 4,
        movies: 3,
        movies_unknown: 3,
        seasons: 1,
        seasons_unknown: 1,
      }),
    );
    renderBreakdown();
    expect(await screen.findByText(/would remove nothing right now/)).toBeInTheDocument();
    expect(screen.getByText(/couldn't measure any of the titles/)).toBeInTheDocument();
    expect(screen.queryByText("Will be reaped")).not.toBeInTheDocument();
  });

  it("says it couldn't check the allowance rather than printing an adjusted count", async () => {
    apiMock.profile.mockRejectedValue(new Error("boom"));
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 569, will_reap_unknown: 4, movies_unknown: 3, seasons_unknown: 1 }),
    );
    renderBreakdown();
    const notice = await screen.findByText(/couldn't check your unknown-size allowance/);
    expect(notice).toHaveClass("notice-warn");
    // Neither answer may be stated as fact: not the held-back 565, not the raw 569 (B-16).
    expect(screen.queryByText("565")).not.toBeInTheDocument();
    expect(screen.queryByText("569")).not.toBeInTheDocument();
    expect(screen.queryByText("Will be reaped")).not.toBeInTheDocument();
  });

  it("reports held hand reaps rather than dropping them", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ hand_reaped_held: 2 }));
    renderBreakdown();
    // The operator marked reaps the engine won't honor yet: say so (PR-2), and name the
    // operation that is actually holding them -- this reap, never "a scan" (U-10).
    expect(await screen.findByText(/2 reaps you marked are on hold/)).toBeInTheDocument();
    expect(screen.queryByText(/a scan won't remove/)).not.toBeInTheDocument();
  });
});

describe("the by-reason bars", () => {
  it("name each signal in plain words and carry the overlap note", async () => {
    renderBreakdown();
    expect(await screen.findByText("Gone unwatched too long")).toBeInTheDocument();
    expect(screen.getByText("Low rating")).toBeInTheDocument();
    expect(screen.getByText(/usually trip more than one, so these overlap/)).toBeInTheDocument();
  });

  it("shows a custom rule under its own name", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ condemned_by: [{ id: "My weekend rule", count: 9, bytes: 0, unknown_size: 0 }] }),
    );
    renderBreakdown();
    expect(await screen.findByText("My weekend rule")).toBeInTheDocument();
  });
});

describe("the states that are not a full ledger", () => {
  it("says nothing would be reaped when the net is empty", async () => {
    apiMock.reapBreakdown.mockResolvedValue(
      full({ will_reap: 0, policy_condemned: 0, hand_spared: 0, hand_reaped: 0 }),
    );
    renderBreakdown();
    expect(await screen.findByText(/would remove nothing right now/)).toBeInTheDocument();
  });

  it("prompts a scan before the first one", async () => {
    apiMock.reapBreakdown.mockResolvedValue(full({ has_snapshot: false }));
    renderBreakdown();
    expect(await screen.findByText(/No scan yet/)).toBeInTheDocument();
  });

  it("says it couldn't look, in the amber tone, on a failed load", async () => {
    apiMock.reapBreakdown.mockRejectedValue(new Error("boom"));
    renderBreakdown();
    const notice = await screen.findByText(/Couldn't load what a reap would remove/);
    expect(notice).toHaveClass("notice-warn");
  });
});

describe("the pointers off the page", () => {
  it("routes to Plex settings and to the review queue", async () => {
    const onPlex = vi.fn();
    const onReview = vi.fn();
    renderBreakdown(onPlex, onReview);
    const person = userEvent.setup();
    await person.click(await screen.findByRole("button", { name: /Settings → Plex/ }));
    await waitFor(() => expect(onPlex).toHaveBeenCalledTimes(1));
    await person.click(screen.getByRole("button", { name: /Review queue/ }));
    await waitFor(() => expect(onReview).toHaveBeenCalledTimes(1));
  });
});
