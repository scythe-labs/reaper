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
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Candidate, type GroupSeasonMark, type Verdict } from "../api";
import {
  compactSpan,
  KeptByShowNote,
  OverrideControls,
  ReviewQueue,
  ShowStatusChip,
} from "./ReviewQueue";

const { apiMock } = vi.hoisted(() => ({
  apiMock: {
    candidates: vi.fn(),
    group: vi.fn(),
    override: vi.fn(),
    clearOverride: vi.fn(),
    vocabularyValues: vi.fn(),
    reapBreakdown: vi.fn(),
    general: vi.fn(),
  },
}));

vi.mock("../api", () => ({ api: apiMock }));

function movie(n: number, extra: Partial<Candidate> = {}): Candidate {
  const c: Candidate = {
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
    library: null,
    dormant_for: null,
    reason: null,
    spared: false,
    override: null,
    override_own: null,
    show_override: null,
    override_effective: null,
    spare_expires_at: null,
    show_spare_expires_at: null,
    chip: null,
    show_status: null,
    season_number: null,
    group_seasons: null,
    ...extra,
  };
  // Default an item's own decision to its effective one unless a test sets them apart (to
  // exercise a season kept only by its show).
  if (extra.override_own === undefined) c.override_own = c.override;
  return c;
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

/** One page of candidates, with `total` deciding whether another page is claimed to exist and
 *  `snapshotId` naming which scan the page came from (so a refetch can land a newer one). */
function page(items: Candidate[], total = items.length, offset = 0, snapshotId = 1) {
  return {
    items,
    total,
    totalBytes: items.reduce((sum, i) => sum + (i.size_bytes ?? 0), 0),
    unknownSize: items.reduce((n, i) => n + (i.size_bytes === null ? 1 : 0), 0),
    offset,
    snapshotId,
  };
}

/** Stands in for the Reap page's breakdown: the net an override changes, mounted so the
 *  test can see whether saving one refreshes it. */
function BreakdownProbe() {
  useQuery({ queryKey: ["reap-breakdown"], queryFn: api.reapBreakdown });
  return null;
}

function renderQueue(verdict: Verdict = "condemn", latestScanSnapshotId: number | null = null) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <BreakdownProbe />
      <ReviewQueue
        verdict={verdict}
        onVerdictChange={() => {}}
        selectedId={null}
        selectedGroupKey={null}
        onSelect={() => {}}
        onSelectGroup={() => {}}
        latestScanSnapshotId={latestScanSnapshotId}
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
  apiMock.general.mockResolvedValue({
    application_name: "Reaper",
    application_url: null,
    accent_color: "#25c3ff",
    api_key_set: false,
    expand_seasons_default: false,
    proxy_trust_enabled: false,
    trusted_proxies: [],
  });
  apiMock.reapBreakdown.mockResolvedValue({
    has_snapshot: true,
    will_reap: 0,
    condemned_by: [],
  });
});

describe("keeping the list in step with the latest scan", () => {
  it("confirms a quiet refresh with a toast only once the swap has landed", async () => {
    // This view loads snapshot 1; the newest completed scan is snapshot 2, so the list is a scan
    // behind. Idle at the top (jsdom scrollY 0, nothing open or selected): it refreshes quietly,
    // the refetch lands snapshot 2, and only THEN does a toast say so -- never at issuance (PR-5).
    apiMock.candidates
      .mockResolvedValueOnce(page([movie(1), movie(2)], 2, 0, 1))
      .mockResolvedValue(page([movie(1), movie(2)], 2, 0, 2));
    renderQueue("condemn", 2);
    expect(await screen.findByText("Updated to the latest scan.")).toBeInTheDocument();
    // Quiet means quiet: no mid-review nudge, no "one scan behind" marker.
    expect(screen.queryByText("A newer scan just finished")).not.toBeInTheDocument();
    expect(screen.queryByText(/One scan behind/)).not.toBeInTheDocument();
  });

  it("does not claim a swap when the refetch fails to catch up; it nudges instead", async () => {
    // The list is a scan behind and the reviewer is idle, so a silent refresh is attempted -- but
    // the refetch errors, so the list never reaches snapshot 2. The toast must not lie that it
    // did; a nudge appears so the reviewer is not left silently stale (PR-5).
    apiMock.candidates
      .mockResolvedValueOnce(page([movie(1), movie(2)], 2, 0, 1))
      .mockRejectedValue(new Error("network blip"));
    renderQueue("condemn", 2);
    expect(await screen.findByText(/A newer scan just finished|One scan behind/)).toBeInTheDocument();
    expect(screen.queryByText("Updated to the latest scan.")).not.toBeInTheDocument();
  });

  it("Show latest closes an open why-panel, whose row belongs to the replaced scan", async () => {
    // A panel open is a busy condition, so a newer scan raises the nudge instead of swapping
    // quietly. Pressing Show latest must close the panel: its candidate id is from the old
    // snapshot, so keeping it open would leave the operator deciding from stale evidence (B-7).
    const onClearItemSelection = vi.fn();
    apiMock.candidates.mockResolvedValue(page([movie(1), movie(2)], 2, 0, 1));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ReviewQueue
          verdict="condemn"
          onVerdictChange={() => {}}
          selectedId={1}
          selectedGroupKey={null}
          onSelect={() => {}}
          onSelectGroup={() => {}}
          onClearItemSelection={onClearItemSelection}
          latestScanSnapshotId={2}
        />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Show latest" }));
    expect(onClearItemSelection).toHaveBeenCalledTimes(1);
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

  it("refreshes the reap breakdown, which the override changes too", async () => {
    // A spare drops an item out of the net and a hand reap adds one, so the Reap page's
    // breakdown must not go on serving the numbers it fetched before the decision.
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    apiMock.override.mockResolvedValue(undefined);
    renderQueue();
    const user = await selectAllDrawn();
    await waitFor(() => expect(apiMock.reapBreakdown).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Spare" }));
    await waitFor(() => expect(apiMock.reapBreakdown).toHaveBeenCalledTimes(2));
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

  it("keeps Reap when the kept seasons are on other lanes, absent from the Condemned page", async () => {
    // The real shape on the Condemned lane: every FETCHED row is condemned (the kept seasons
    // sit on other lanes and never load here), but `group_seasons` still carries the whole
    // show, including a kept one. A whole-show Reap takes that kept season, so Reap must stay
    // -- the card judges over `group_seasons`, not the tab-filtered page.
    const marks: GroupSeasonMark[] = [
      { id: 1, season: 1, verdict: "condemn", override: null, override_effective: null, size_bytes: 1024 ** 3 },
      { id: 2, season: 2, verdict: "condemn", override: null, override_effective: null, size_bytes: 1024 ** 3 },
      { id: 3, season: 3, verdict: "protect", override: null, override_effective: null, size_bytes: 1024 ** 3 },
    ];
    apiMock.candidates.mockResolvedValue(
      page([
        season(1, "condemn", { group_seasons: marks }),
        season(2, "condemn", { group_seasons: marks }),
      ]),
    );
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
  });

  it("does not light the whole-show Reap when only some seasons are reaped", async () => {
    // On the Condemned lane the fetched rows are the reaped/condemned seasons, which all agree
    // "reap". But across the whole show (group_seasons) the override is mixed -- other seasons
    // are untouched -- so the whole-show control must NOT read as "Reaping" (its active state).
    const marks: GroupSeasonMark[] = [
      { id: 5, season: 5, verdict: "abstain", override: "reap", override_effective: false, size_bytes: 1024 ** 3 },
      { id: 8, season: 8, verdict: "condemn", override: "reap", override_effective: true, size_bytes: 1024 ** 3 },
      { id: 10, season: 10, verdict: "protect", override: null, override_effective: null, size_bytes: 1024 ** 3 },
    ];
    apiMock.candidates.mockResolvedValue(
      page([season(8, "condemn", { override: "reap", override_effective: true, group_seasons: marks })]),
    );
    renderQueue("condemn");
    await screen.findByText("Example Show");
    // Both buttons present and inactive: the Reap button says "Reap", never the active "Reaping".
    expect(screen.getByRole("button", { name: "Reap" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reaping" })).not.toBeInTheDocument();
  });

  it("drops Reap once every season of the show is condemned", async () => {
    // Now a whole-show Reap would change nothing, so it falls away just as the movie's does.
    // Every season condemned in `group_seasons` too, so the whole-show view agrees.
    const marks: GroupSeasonMark[] = [
      { id: 1, season: 1, verdict: "condemn", override: null, override_effective: null, size_bytes: 1024 ** 3 },
      { id: 2, season: 2, verdict: "condemn", override: null, override_effective: null, size_bytes: 1024 ** 3 },
    ];
    apiMock.candidates.mockResolvedValue(
      page([
        season(1, "condemn", { group_seasons: marks }),
        season(2, "condemn", { group_seasons: marks }),
      ]),
    );
    renderQueue("condemn");
    expect(await screen.findByRole("button", { name: "Spare" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Reap$/ })).not.toBeInTheDocument();
  });
});

describe("the expand-seasons-by-default preference", () => {
  it("starts a show's season list collapsed when the preference is off", async () => {
    // The beforeEach default is off, so the card rests collapsed until the pill is clicked.
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    renderQueue("condemn");
    const expander = await screen.findByRole("button", { name: "2 seasons" });
    expect(expander).toHaveAttribute("aria-expanded", "false");
  });

  it("opens a show's season list by default when the preference is on", async () => {
    apiMock.general.mockResolvedValue({
      application_name: "Reaper",
      application_url: null,
      accent_color: "#25c3ff",
      api_key_set: false,
      expand_seasons_default: true,
      proxy_trust_enabled: false,
      trusted_proxies: [],
    });
    apiMock.candidates.mockResolvedValue(page([season(1, "condemn"), season(2, "condemn")]));
    renderQueue("condemn");
    // The preference resolves on its own query, so the card may mount collapsed and expand a
    // tick later; wait for the expanded state rather than asserting the first frame.
    const expander = await screen.findByRole("button", { name: "2 seasons" });
    await waitFor(() => expect(expander).toHaveAttribute("aria-expanded", "true"));
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
    // capitalized and carrying a middot). Both have to land in the same sentence.
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
    // The lane chip's own capitalized wording never lands mid-sentence.
    expect(
      screen.queryByText(/kept for now: Needs a look/),
    ).not.toBeInTheDocument();
  });
});

describe("the score badge's color follows the fate", () => {
  // The number cannot keep the scan verdict while the row says "will be removed": it wears
  // the fate a hand decision forces. Solid reap/spare when it takes effect, dashed red when
  // a reap is held (never amber -- amber means "left for you to decide").
  async function scoreClassFor(item: Candidate): Promise<string> {
    apiMock.candidates.mockResolvedValue(page([item]));
    const { container } = renderQueue();
    await screen.findByText("Example Movie 1");
    return container.querySelector(".score")!.className;
  }

  it("goes solid red when the item is reaped by hand", async () => {
    // A protect item (green by the scan) that a hand reap will actually remove.
    const cls = await scoreClassFor(
      movie(1, { verdict: "protect", override: "reap", override_effective: true }),
    );
    expect(cls).toContain("score-reap");
    expect(cls).not.toContain("score-protect");
  });

  it("goes dashed red when the reap is held for now", async () => {
    const cls = await scoreClassFor(
      movie(1, { verdict: "abstain", override: "reap", override_effective: false }),
    );
    expect(cls).toContain("score-refused");
    expect(cls).not.toContain("score-abstain");
  });

  it("goes solid green when the item is spared by hand", async () => {
    const cls = await scoreClassFor(
      movie(1, { verdict: "condemn", override: "spare" }),
    );
    expect(cls).toContain("score-spare");
    expect(cls).not.toContain("score-condemn");
  });

  it("keeps the scan verdict's color when nothing was overridden", async () => {
    const cls = await scoreClassFor(movie(1, { verdict: "condemn" }));
    expect(cls).toContain("score-condemn");
  });
});

describe("the season strip's colors follow the fate", () => {
  // A show card's strip draws one square per season from `group_seasons`. Each square must
  // agree with its row: solid for an effective hand decision, dashed red (with a scythe
  // mark) for a reap the engine can't honor yet, the scan verdict otherwise. Amber is never
  // used here -- it means only "left for you to decide".
  function mark(id: number, verdict: Verdict, extra: Partial<GroupSeasonMark> = {}): GroupSeasonMark {
    return {
      id,
      season: id,
      verdict,
      override: null,
      override_effective: null,
      size_bytes: 1024 ** 3,
      ...extra,
    };
  }

  async function stripRender(marks: GroupSeasonMark[]) {
    const rows = marks.map((m) =>
      season(m.id, m.verdict, {
        override: m.override,
        override_effective: m.override_effective,
        group_seasons: marks,
      }),
    );
    apiMock.candidates.mockResolvedValue(page(rows));
    const { container } = renderQueue();
    await screen.findByText("Example Show");
    const squares = Array.from(container.querySelectorAll(".strip-sq"));
    return { squares, classes: squares.map((el) => el.className) };
  }

  it("paints a held reap dashed red, not solid and not its plain scan color", async () => {
    const { classes } = await stripRender([
      mark(14, "condemn"),
      mark(19, "abstain", { override: "reap", override_effective: false }),
      mark(20, "abstain", { override: "reap", override_effective: true }),
    ]);
    // 14 condemned, 19 reap-held (dashed red), 20 reap-effective (solid).
    expect(classes[0]).toContain("strip-condemn");
    expect(classes[1]).toContain("strip-ov-reap-refused");
    expect(classes[1]).not.toContain("strip-ov-reap ");
    expect(classes[2]).toContain("strip-ov-reap");
    expect(classes[2]).not.toContain("strip-ov-reap-refused");
  });

  it("marks the held reap with a scythe so it never reads as the plain condemned square", async () => {
    const { squares } = await stripRender([
      mark(14, "condemn"),
      mark(19, "abstain", { override: "reap", override_effective: false }),
    ]);
    // Only the held reap carries the mark; the plain condemned square must not.
    expect(squares[0]!.querySelector(".strip-mark")).toBeNull();
    expect(squares[1]!.querySelector(".strip-mark")).not.toBeNull();
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

// The note that keeps a season's Spare/Reap honest when a WHOLE-SHOW decision is what really
// governs it: the control toggles the season's own decision, and this note says what the show
// is doing so the operator never fights a toggle that cannot reach the show-level choice. Its
// wording turns on the relationship between the season's own decision and its show's.
describe("the kept-by-the-whole-show note", () => {
  const render1 = (
    own: "spare" | "reap" | null,
    show: "spare" | "reap" | null,
    effective: boolean | null = null,
  ) => render(<KeptByShowNote own={own} showOverride={show} effective={effective} />);

  it("says nothing when no whole-show decision covers the season", () => {
    const { container } = render1(null, null);
    expect(container).toBeEmptyDOMElement();
    // A season with only its own decision (a movie's shape) shows no note either.
    render1("spare", null);
    expect(screen.queryByText(/whole show/i)).not.toBeInTheDocument();
  });

  it("explains an inherited spare and points to the show", () => {
    render1(null, "spare");
    expect(screen.getByText(/this season is kept/i)).toBeInTheDocument();
    expect(screen.getByText(/Undo it on the show/i)).toBeInTheDocument();
  });

  it("warns that clearing an own spare won't help while the show also spares it", () => {
    render1("spare", "spare");
    expect(screen.getByText(/clearing this one won't remove it/i)).toBeInTheDocument();
  });

  it("says a season reaped against a spared show will still be removed", () => {
    render1("reap", "spare");
    expect(screen.getByText(/will be removed/i)).toBeInTheDocument();
    expect(screen.getByText(/even though the whole show is spared/i)).toBeInTheDocument();
  });

  it("says a season spared against a reaped show stays", () => {
    render1("spare", "reap");
    expect(screen.getByText(/stays/i)).toBeInTheDocument();
    expect(screen.getByText(/even though the whole show is set to reap/i)).toBeInTheDocument();
  });

  it("never promises removal for a reap the engine is holding (U-1)", () => {
    // A season reaped against a spared show, but the engine can't honor the reap yet
    // (streaming now): the note must say "kept for now", not "will be removed".
    render1("reap", "spare", false);
    expect(screen.getByText(/kept for now/i)).toBeInTheDocument();
    expect(screen.queryByText(/will be removed/i)).not.toBeInTheDocument();
  });

  it("says an inherited reap the engine can't honor yet is kept for now (U-1)", () => {
    render1(null, "reap", false);
    expect(screen.getByText(/kept for now/i)).toBeInTheDocument();
    expect(screen.queryByText(/will be removed/i)).not.toBeInTheDocument();
  });
});

// The row and card that hold OverrideControls also handle Enter/Space themselves (to open the
// why-panel). Keydown from the buttons must not bubble into that handler, whose preventDefault
// would cancel the button's own activation and open the panel instead of saving the override.
describe("keyboard activation of a revealed Spare/Reap button", () => {
  it("saves the override and does not bubble to the row's key handler (B-7)", async () => {
    const onSet = vi.fn();
    const rowKeyDown = vi.fn();
    // OverrideControls reads the default spare length from the general-settings query, so it
    // needs a client even in isolation; unresolved, the default reads as 0 (forever).
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <div onKeyDown={rowKeyDown}>
          <OverrideControls override={null} onSet={onSet} onClear={vi.fn()} pending={false} />
        </div>
      </QueryClientProvider>,
    );
    screen.getByRole("button", { name: "Spare" }).focus();
    await userEvent.keyboard("{Enter}");

    // A plain Spare press carries the operator's default length; unknown settings read as 0
    // (forever), the safe default.
    expect(onSet).toHaveBeenCalledWith("spare", 0);
    // The key stopped at the control, so the row handler (which would preventDefault and open
    // the panel) never ran.
    expect(rowKeyDown).not.toHaveBeenCalled();
  });
});

// The Spare chevron opens a length menu: quick day-presets, Forever, and a Custom entry. Each
// pick spares at that length, so the menu is the action, not a form.
describe("the Spare length menu", () => {
  function renderControls(onSet = vi.fn()) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <OverrideControls override={null} onSet={onSet} onClear={vi.fn()} pending={false} />
      </QueryClientProvider>,
    );
    return onSet;
  }

  it("spares for a chosen preset length", async () => {
    const user = userEvent.setup();
    const onSet = renderControls();
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    await user.click(screen.getByRole("menuitem", { name: /90 days/ }));
    expect(onSet).toHaveBeenCalledWith("spare", 90);
  });

  it("spares forever when Forever is picked", async () => {
    const user = userEvent.setup();
    const onSet = renderControls();
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    await user.click(screen.getByRole("menuitem", { name: /Forever/ }));
    expect(onSet).toHaveBeenCalledWith("spare", 0);
  });

  it("spares for a custom number of days", async () => {
    const user = userEvent.setup();
    const onSet = renderControls();
    await user.click(screen.getByRole("button", { name: "Choose how long to keep it" }));
    await user.click(screen.getByRole("menuitem", { name: /Custom length/ }));
    const box = screen.getByLabelText("Custom spare length in days");
    await user.clear(box);
    await user.type(box, "45");
    // Scope to the menu: the row's own split Spare button is also named "Spare".
    await user.click(within(screen.getByRole("menu")).getByRole("button", { name: "Spare" }));
    expect(onSet).toHaveBeenCalledWith("spare", 45);
  });
});

// One ＋ Filter control replaces the old row of fixed dropdowns: any filter is added from a
// menu, shows as an editable chip, and is removed from the chip. A new filter is a registry
// entry, so this one flow covers them all.
describe("the unified filter bar", () => {
  it("adds a filter from the ＋ Filter menu, edits its value, then removes it", async () => {
    apiMock.vocabularyValues.mockImplementation((field: string) =>
      Promise.resolve({ field, values: field === "library" ? ["Movies", "4K Movies"] : [] }),
    );
    apiMock.candidates.mockResolvedValue(page([movie(1)]));
    const user = userEvent.setup();
    renderQueue();
    await screen.findByText("Example Movie 1");

    // Add the Library filter. Its options come from the scan's vocabulary, so the menu entry
    // appears once that query resolves.
    await user.click(screen.getByRole("button", { name: "Filter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Library" }));

    // The candidates query now carries the library, and the filter is a removable chip.
    await waitFor(() =>
      expect(apiMock.candidates).toHaveBeenCalledWith(
        "condemn",
        expect.objectContaining({ library: "Movies" }),
        expect.anything(),
        expect.anything(),
      ),
    );
    expect(screen.getByRole("button", { name: "Remove the Library filter" })).toBeInTheDocument();

    // Clicking the chip opens its value picker; choosing another value re-filters in place.
    await user.click(screen.getByRole("button", { name: "Movies" }));
    await user.click(await screen.findByRole("option", { name: "4K Movies" }));
    await waitFor(() =>
      expect(apiMock.candidates).toHaveBeenCalledWith(
        "condemn",
        expect.objectContaining({ library: "4K Movies" }),
        expect.anything(),
        expect.anything(),
      ),
    );

    // Removing the chip drops the filter entirely.
    await user.click(screen.getByRole("button", { name: "Remove the Library filter" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Remove the Library filter" }),
      ).not.toBeInTheDocument(),
    );
    expect(apiMock.candidates).toHaveBeenLastCalledWith(
      "condemn",
      expect.objectContaining({ library: "" }),
      expect.anything(),
      expect.anything(),
    );
  });
});
