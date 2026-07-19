// SPDX-License-Identifier: AGPL-3.0-or-later
// The all-seasons list under an expanded show card. Two behaviors are load-bearing:
// the open list must say "loading" and "failed" out loud (an open chevron over silence
// reads as broken), and rows from other lanes are visible for the whole-show picture
// but act only from their own tab -- no Spare/Reap buttons here.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Candidate, Chip, Group, ShowStatus, Verdict } from "../api";
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
  showStatus: ShowStatus | null = null,
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
    override_effective: null,
    chip,
    show_status: showStatus,
    season_number: n,
    group_seasons: [
      { id: 1, season: 1, verdict: "protect", override: null, override_effective: null, size_bytes: 1024 ** 3 },
      { id: 2, season: 2, verdict: "condemn", override: null, override_effective: null, size_bytes: 1024 ** 3 },
      { id: 3, season: 3, verdict: "abstain", override: null, override_effective: null, size_bytes: 1024 ** 3 },
    ],
  };
}

const limboSeason = season(3, 3, "abstain", 82, {
  tone: "look",
  text: "Needs a look · watched more than a season your rule keeps",
});

function renderQueue(
  overrides: {
    onSelect?: (id: number) => void;
    onSelectGroup?: (key: string) => void;
    items?: Candidate[];
  } = {},
) {
  const items = overrides.items ?? [limboSeason];
  apiMock.candidates.mockResolvedValue({
    items,
    total: items.length,
    totalBytes: items.reduce((sum, i) => sum + i.size_bytes, 0),
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
        onSelect={overrides.onSelect ?? (() => {})}
        onSelectGroup={overrides.onSelectGroup ?? (() => {})}
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
    expect(screen.getByTitle("Season 1: kept. Open for its full reasoning.")).toBeInTheDocument();
    expect(
      screen.getByTitle("Season 2: would be removed. Open for its full reasoning."),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle("Season 3: left alone. Open for its full reasoning."),
    ).toBeInTheDocument();
  });

  it("marks an ended show, says so when it couldn't check, and stays quiet otherwise", async () => {
    // Three shows, one per state. The card names only the two worth reading: no chip is
    // how "still going" reads, so a bare row must carry no status word at all.
    const ended = season(11, 1, "abstain", 80, null, "ended");
    const going = { ...season(12, 1, "abstain", 80, null, "continuing"), group_key: "sonarr:5:43" };
    const unread = { ...season(13, 1, "abstain", 80, null, "unknown"), group_key: "sonarr:5:44" };
    renderQueue({ items: [ended, going, unread] });

    expect(await screen.findByTitle("This show has ended")).toHaveTextContent("Ended");
    expect(
      screen.getByTitle("We couldn't check whether this show has ended"),
    ).toHaveTextContent("Status unknown");
    // The still-going show wears nothing: one card, one chip, and neither is its.
    expect(screen.queryByText("Still going")).not.toBeInTheDocument();
    expect(screen.getAllByText("Ended")).toHaveLength(1);
    expect(screen.getAllByText("Status unknown")).toHaveLength(1);
  });

  it("opens a season's own reasoning when its strip square is clicked", async () => {
    const onSelect = vi.fn();
    const onSelectGroup = vi.fn();
    renderQueue({ onSelect, onSelectGroup });
    const { userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    // Season 2's square carries the candidate id 2; clicking it opens that season, not
    // the show. The square sits inside the card head (which opens the show), so the click
    // must stop there: onSelect fires with the season, onSelectGroup never does.
    await user.click(
      await screen.findByRole("button", { name: "Open Season 2, would be removed" }),
    );
    expect(onSelect).toHaveBeenCalledWith(2);
    expect(onSelectGroup).not.toHaveBeenCalled();
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
      show_status: null,
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
    // Scope to the season list: the card's strip squares are also Season-named buttons.
    const list = screen.getByRole("list");
    const rows = within(list).getAllByRole("button", { name: /Season \d/ });
    expect(rows).toHaveLength(3);
    expect(screen.getAllByRole("button", { name: /^Spare$/ })).toHaveLength(2);
  });

  it("says a season's size is unknown rather than showing it as empty", async () => {
    // A season only becomes a candidate if it holds files, so a stored 0 means Sonarr
    // declined to report a size. "0 B" here would be a false statement sitting beside
    // Spare and Reap.
    const unsized = { ...season(2, 2, "condemn", 88, null), size_bytes: 0 };
    apiMock.group.mockResolvedValue({
      group_key: "sonarr:5:42",
      title: "Example Show",
      year: 2012,
      poster_url: null,
      summary: null,
      size_bytes: 1024 ** 3,
      reason: null,
      chip: limboSeason.chip,
      links: {} as Group["links"],
      show_status: null,
      seasons: [unsized, limboSeason],
    } as Group);
    renderQueue();
    await expandSeasons();

    const list = screen.getByRole("list");
    expect(await within(list).findByText("Size unknown")).toBeInTheDocument();
    expect(within(list).queryByText("0 B")).not.toBeInTheDocument();
    // The season that did report one still reads as a size.
    expect(within(list).getByText("1.0 GiB")).toBeInTheDocument();
  });
});
