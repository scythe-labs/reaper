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

const { apiMock } = vi.hoisted(() => ({ apiMock: { reapBreakdown: vi.fn() } }));
vi.mock("../api", () => ({ api: apiMock }));

const GB = 1024 ** 3;

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
    will_reap: 569,
    will_reap_bytes: 4500 * GB,
    will_reap_unknown: 0,
    movies: 402,
    seasons: 167,
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
    apiMock.reapBreakdown.mockResolvedValue(full({ will_reap_unknown: 4 }));
    renderBreakdown();
    expect(await screen.findByText(/4 titles can't be measured/)).toBeInTheDocument();
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
