// SPDX-License-Identifier: AGPL-3.0-or-later
// The bulk paths of the review queue, where one press writes the override that decides
// whether many files are reaped or spared. Three behaviors are load-bearing:
//   - a bulk override that partly fails refreshes anyway, keeps ONLY the failed items
//     picked, and says how many failed (a clean-up that cleared the whole selection would
//     silently drop them from the next plan);
//   - "select everything matching" that cannot reach the end of the list selects nothing
//     and says so, rather than quietly meaning "the first page";
//   - nothing else can be pressed while a bulk write is in flight.
// The compact dormancy span is pinned here too: it rewrites a string the server writes.
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Candidate, type Verdict } from "../api";
import { compactSpan, ReviewQueue, ShowStatusChip } from "./ReviewQueue";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    candidates: vi.fn(),
    group: vi.fn(),
    override: vi.fn(),
    clearOverride: vi.fn(),
    vocabularyValues: vi.fn(),
    grace: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api: apiMock }));

function movie(n: number, extra: Partial<Candidate> = {}): Candidate {
  return {
    id: n,
    media_key: `radarr:1:${n}`,
    title: `Example Movie ${n}`,
    media_type: "movie",
    size_bytes: 1024 ** 3,
    verdict: "condemn",
    score: 80,
    coverage_bp: 10_000,
    first_flagged_at: null,
    year: 2011,
    summary: null,
    poster_url: null,
    requested_by: null,
    group_key: null,
    group_title: null,
    group_condemned_count: null,
    group_condemned_bytes: null,
    group_unknown_size: null,
    video_resolution: null,
    dormant_for: null,
    reason: null,
    spared: false,
    override: null,
    override_effective: null,
    chip: null,
    show_status: null,
    season_number: null,
    group_seasons: null,
    ...extra,
  };
}

/** A season row of one show. Shared `group_key` folds them into a single show card. */
function season(n: number, verdict: Verdict, extra: Partial<Candidate> = {}): Candidate {
  return movie(n, {
    media_key: `sonarr:1:${n}`,
    media_type: "season",
    title: "Example Show",
    group_key: "sonarr:show:1",
    group_title: "Example Show",
    season_number: n,
    verdict,
    ...extra,
  });
}

/** One page of candidates, with `total` deciding whether another page is claimed to exist. */
function page(items: Candidate[], total = items.length, offset = 0) {
  return {
    items,
    total,
    totalBytes: items.reduce((sum, i) => sum + (i.size_bytes ?? 0), 0),
    unknownSize: items.reduce((n, i) => n + (i.size_bytes === null ? 1 : 0), 0),
    offset,
  };
}

/** Stands in for the grace panel: the countdown an override changes, mounted so the test
 *  can see whether saving one refreshes it. */
function GraceProbe() {
  useQuery({ queryKey: ["grace"], queryFn: api.grace });
  return null;
}

function renderQueue(verdict: Verdict = "condemn") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <GraceProbe />
      <ReviewQueue
        verdict={verdict}
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
      />
    </QueryClientProvider>,
  );
}

/** How many cards the bulk bar says are picked. Scoped to the bar: the queue header
 *  carries a count of its own, and the two are different numbers. */
function pickedCount(): string {
  const bar = screen.getByRole("region", { name: "Bulk actions" });
  return within(bar).getByText(/selected|Tap or drag/).textContent ?? "";
}

/** Enter Select mode (the bulk bar only exists there) and pick every drawn card. */
async function selectAllDrawn() {
  const { userEvent } = await import("@testing-library/user-event");
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Select" }));
  await user.click(await screen.findByRole("button", { name: "Select all" }));
  return user;
}

// The queue watches a sentinel to reveal more cards; jsdom has no observer, and the list
// under test is short enough that never firing is the honest behavior.
class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

beforeEach(() => {
  vi.stubGlobal("IntersectionObserver", NoopObserver);
  vi.clearAllMocks();
  apiMock.vocabularyValues.mockResolvedValue({ values: [] });
  apiMock.group.mockResolvedValue(null);
  apiMock.grace.mockResolvedValue({
    in_grace: [],
    in_grace_count: 0,
    ready_count: 0,
    total_bytes_in_grace: 0,
  });
});

describe("a bulk override", () => {
  it("refreshes, keeps only the failed item picked, and says how many failed", async () => {
    const items = [movie(1), movie(2), movie(3)];
    apiMock.candidates.mockResolvedValue(page(items));
    apiMock.override.mockImplementation((key: string) =>
      key === "radarr:1:2" ? Promise.reject(new Error("boom")) : Promise.resolve(undefined),
    );
    renderQueue();
    const user = await selectAllDrawn();
    expect(pickedCount()).toContain("3");

    const listFetches = apiMock.candidates.mock.calls.length;
    await user.click(screen.getByRole("button", { name: "Spare" }));

    // All three were attempted: Promise.all would have skipped the two after the failure.
    await waitFor(() => expect(apiMock.override).toHaveBeenCalledTimes(3));
    expect(apiMock.override.mock.calls.map((c) => c[0]).sort()).toEqual([
      "radarr:1:1",
      "radarr:1:2",
      "radarr:1:3",
    ]);
    // The queue refreshed even though part of the write failed.
    await waitFor(() =>
      expect(apiMock.candidates.mock.calls.length).toBeGreaterThan(listFetches),
    );
    // The failed one, alone, is still picked, and the notice counts it.
    expect(
      await screen.findByText(/1 item could not be updated; it is still selected/),
    ).toBeInTheDocument();
    expect(pickedCount()).toContain("1");
    // And it is the one that failed, not just any one: a clean-up that cleared the whole
    // selection and left an unrelated card picked would still count to one.
    expect(screen.getByRole("button", { name: "Select Example Movie 2" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    for (const title of ["Example Movie 1", "Example Movie 3"]) {
      expect(screen.getByRole("button", { name: `Select ${title}` })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    }
  });

  it("refreshes the grace countdown, which the override changes too", async () => {
    // A spare drops an item out of the countdown and a hand reap puts one in, so the plan
    // view must not go on serving the list it fetched before the decision.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    apiMock.override.mockResolvedValue(undefined);
    renderQueue();
    const user = await selectAllDrawn();
    await waitFor(() => expect(apiMock.grace).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Spare" }));
    await waitFor(() => expect(apiMock.grace).toHaveBeenCalledTimes(2));
  });

  it("blocks the other actions while it is in flight", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)]));
    // Both writes hang until the test lets them go; one then fails, so a pick survives and
    // the buttons have something to be enabled for once the write is no longer in flight.
    const settle: (() => void)[] = [];
    apiMock.override.mockImplementation(
      (key: string) =>
        new Promise<void>((resolve, reject) => {
          settle.push(() =>
            key === "radarr:1:2" ? reject(new Error("boom")) : resolve(undefined),
          );
        }),
    );
    renderQueue();
    const user = await selectAllDrawn();
    await user.click(screen.getByRole("button", { name: "Spare" }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Reap now/ })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Clear override" })).toBeDisabled();
    await waitFor(() => expect(settle).toHaveLength(2));
    settle.forEach((s) => s());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Reap now/ })).not.toBeDisabled(),
    );
  });
});

describe("the per-card override buttons", () => {
  it("offers Spare but not Reap on the Condemned lane, since the item is already on the block", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderQueue();
    // The card's own Spare (rescue) is here; there is no per-card Reap on this lane -- it
    // would force onto a list the item is already on. The bulk bar's Reap is another surface.
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
  });

  it("offers both Spare and Reap off the Condemned lane, where forcing a reap means something", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { verdict: "protect" })]));
    renderQueue("protect");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
  });
});

describe("the whole-show override buttons", () => {
  it("keeps Reap on a part-condemned show on the Condemned lane, unlike a condemned movie", async () => {
    // The show is here because SOME season is condemned; a whole-show Reap still takes the
    // seasons the scan kept, so it is not the movie's no-op and both buttons stay.
    apiMock.candidates.mockResolvedValue(
      page([season(1, "condemn"), season(2, "condemn"), season(3, "protect")]),
    );
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
  });

  it("drops Reap once every season of the show is condemned", async () => {
    // Now a whole-show Reap would change nothing, so it falls away just as the movie's does.
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
  });
});

describe("the bulk Reap override", () => {
  it("is dropped on Condemned, where the real deletion (Reap now) stays instead", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    renderQueue();
    await selectAllDrawn();
    // The redundant bulk Reap override is gone on this lane; Reap now (the real delete) stays.
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Reap now/ })).toBeInTheDocument();
  });

  it("keeps the bulk Reap override off the Condemned lane", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { verdict: "protect" })]));
    renderQueue("protect");
    await selectAllDrawn();
    expect(screen.getByRole("button", { name: /^Reap$/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Reap now/ })).not.toBeInTheDocument();
  });
});

describe("select everything matching", () => {
  it("selects nothing and says so when the rest of the list won't load", async () => {
    const first = [movie(1), movie(2)];
    apiMock.candidates.mockImplementation((_verdict, _filters, _limit, offset: number) =>
      offset === 0 ? Promise.resolve(page(first, 4)) : Promise.reject(new Error("boom")),
    );
    renderQueue();
    const user = await selectAllDrawn();
    // Two drawn and picked, four matching: the button that reaches past the drawn cards.
    await user.click(
      await screen.findByRole("button", { name: /Select everything matching/ }),
    );

    expect(
      await screen.findByText(/Couldn't load the rest of the list, so nothing was selected/),
    ).toBeInTheDocument();
    // The picks are exactly what they were: the two drawn cards, not the four claimed.
    expect(pickedCount()).toContain("2");
  });
});

describe("a reap the engine refused", () => {
  it("reads as one sentence whichever lane kept the item", async () => {
    // Two lanes refuse a hand reap: a safety stop that fired (its chip is "Kept · ...")
    // and a protection that could not be checked (its chip is that lane's own wording,
    // capitalised and carrying a middot). Both have to land in the same sentence.
    const streaming = movie(1, {
      override: "reap",
      override_effective: false,
      chip: { tone: "kept", text: "Kept · playing right now" },
    });
    const unchecked = movie(2, {
      override: "reap",
      override_effective: false,
      chip: { tone: "look", text: "Needs a look · left for you to decide" },
    });
    apiMock.candidates.mockResolvedValue(page([streaming, unchecked]));
    renderQueue();

    expect(
      await screen.findByText("Reap requested · kept for now: playing right now"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Reap requested · kept for now: a check on it couldn't be settled"),
    ).toBeInTheDocument();
    // The lane chip's own capitalised wording never lands mid-sentence.
    expect(
      screen.queryByText(/kept for now: Needs a look/),
    ).not.toBeInTheDocument();
  });
});

describe("the show-status chip", () => {
  it("announces the long form, not the bare label", async () => {
    // The visible words on their own are ambiguous ("Ended" reads as a fact about the
    // file, "Status unknown" says nothing), so the chip carries a sentence. A name only
    // reaches a screen reader on an element whose role can take one.
    render(<ShowStatusChip status="unknown" />);
    expect(
      screen.getByRole("img", { name: "We couldn't check whether this show has ended" }),
    ).toBeInTheDocument();
  });
});

describe("the dormancy span", () => {
  it("shortens a unit that follows a number and leaves a wordy span alone", () => {
    expect(compactSpan("5 years, 9 months")).toBe("5y 9m");
    expect(compactSpan("1 day")).toBe("1d");
    // The server's sub-day phrase carries no number, so nothing in it is a unit to shorten.
    expect(compactSpan("less than a day")).toBe("less than a day");
  });

  it("renders the sub-day phrase whole on the card", async () => {
    apiMock.candidates.mockResolvedValue(page([movie(1, { dormant_for: "less than a day" })]));
    renderQueue();
    expect(await screen.findByText(/Not watched in less than a day/)).toBeInTheDocument();
  });

  it("says on the card when an item will not be reaped for want of a size", async () => {
    // No plan will include it, which outranks every other reason the card could show:
    // an owner reading "Not watched in 5 years" would reasonably expect it to go.
    apiMock.candidates.mockResolvedValue(
      page([movie(1, { size_bytes: null, dormant_for: "5 years" })]),
    );
    renderQueue();

    expect(await screen.findByText("Held back: size unknown")).toBeInTheDocument();
    expect(screen.getByText("Size unknown")).toBeInTheDocument();
  });

  it("shows the ordinary reason when the size is known", async () => {
    // The hold-back line must not become permanent furniture: it appears only for the
    // items it is about.
    apiMock.candidates.mockResolvedValue(page([movie(1, { dormant_for: "less than a day" })]));
    renderQueue();

    await screen.findByText(/Not watched in less than a day/);
    expect(screen.queryByText("Held back: size unknown")).not.toBeInTheDocument();
  });
});
