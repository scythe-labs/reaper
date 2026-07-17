// SPDX-License-Identifier: AGPL-3.0-or-later
// The all-seasons list under an expanded show card. Two behaviors are load-bearing:
// the open list must say "loading" and "failed" out loud (an open chevron over silence
// reads as broken), and rows from other lanes are visible for the whole-show picture
// but act only from their own tab -- no Spare/Reap buttons here.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Candidate, Chip, Group, Verdict } from "../api";
import { ReviewQueue } from "./ReviewQueue";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    candidates: vi.fn(),
    group: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api: apiMock }));

function season(
  id: number,
  n: number,
  verdict: Verdict,
  score: number,
  chip: Chip | null,
): Candidate {
  return {
    id,
    media_key: `sonarr:5:42:${n}`,
    title: `Example Show · Season ${n}`,
    media_type: "season",
    size_bytes: 1024 ** 3,
    verdict,
    score,
    coverage_bp: 10_000,
    first_flagged_at: null,
    year: 2012,
    summary: null,
    poster_url: null,
    requested_by: null,
    group_key: "sonarr:5:42",
    group_title: "Example Show",
    group_condemned_count: null,
    group_condemned_bytes: null,
    video_resolution: null,
    dormant_for: null,
    reason: null,
    spared: false,
    override: null,
    chip,
    season_number: n,
    group_seasons: [
      { season: 1, verdict: "protect", override: null, size_bytes: 1024 ** 3 },
      { season: 2, verdict: "condemn", override: null, size_bytes: 1024 ** 3 },
      { season: 3, verdict: "abstain", override: null, size_bytes: 1024 ** 3 },
    ],
  };
}

const limboSeason = season(3, 3, "abstain", 82, {
  tone: "look",
  text: "Needs a look · watched more than a season your rule keeps",
});

function renderQueue() {
  apiMock.candidates.mockResolvedValue({
    items: [limboSeason],
    total: 1,
    totalBytes: limboSeason.size_bytes,
    offset: 0,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ReviewQueue
        verdict="abstain"
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
      />
    </QueryClientProvider>,
  );
}

async function expandSeasons() {
  const { userEvent } = await import("@testing-library/user-event");
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /3 seasons/ }));
  return user;
}

describe("the show card", () => {
  it("wears its one status chip and the whole-show strip", async () => {
    renderQueue();
    expect(
      await screen.findByText("Needs a look · watched more than a season your rule keeps"),
    ).toBeInTheDocument();
    // The strip marks every season across every lane, not just this tab's.
    expect(screen.getByTitle("Season 1: kept")).toBeInTheDocument();
    expect(screen.getByTitle("Season 2: would be removed")).toBeInTheDocument();
    expect(screen.getByTitle("Season 3: left alone")).toBeInTheDocument();
  });
});

describe("the all-seasons list", () => {
  it("says it failed rather than sitting silent under an open chevron", async () => {
    apiMock.group.mockRejectedValue(new Error("boom"));
    renderQueue();
    await expandSeasons();
    expect(await screen.findByText(/Couldn't load the seasons/)).toBeInTheDocument();
  });

  it("lists every lane, with actions only on this tab's rows", async () => {
    const group: Group = {
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 3 * 1024 ** 3,
      reason: null,
      chip: limboSeason.chip,
      links: {} as Group["links"],
      seasons: [
        season(1, 1, "protect", 34, { tone: "kept", text: "Kept · someone is partway through" }),
        season(2, 2, "condemn", 88, null),
        limboSeason,
      ],
    };
    apiMock.group.mockResolvedValue(group);
    renderQueue();
    await expandSeasons();

    // Every lane is present: the kept season's chip, the condemned row's constant
    // mark, and the limbo row (whose chip also sits on the card head).
    expect(await screen.findByText("Kept · someone is partway through")).toBeInTheDocument();
    expect(screen.getByText("Would be removed")).toBeInTheDocument();

    // Only the row from THIS tab (Limbo) carries Spare/Reap; the other two act from
    // their own tabs. Exactly two Spare buttons exist: the card head's and the limbo row's.
    const rows = screen.getAllByRole("button", { name: /Season \d/ });
    expect(rows).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: /^Spare$/ })).toHaveLength(2);
  });
});
